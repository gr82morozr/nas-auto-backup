import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_report
import backup_store as store
import backup_worker as worker


class BackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source, self.archive = root / "source", root / "archive"
        self.source.mkdir()
        self.archive.mkdir()
        locations = patch.multiple(store, SOURCE=self.source, ARCHIVE=self.archive,
                                   DATA=root, DB=root / "jobs.sqlite3")
        locations.start()
        self.addCleanup(locations.stop)
        store.initialize()
        self.db = store.connect()
        self.addCleanup(self.db.close)

    def job(self, kind):
        job = store.enqueue(self.db, kind)
        worker.run_job(self.db, self.db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())
        return job

    def states(self, job):
        return dict(self.db.execute("SELECT path,state FROM files WHERE job=?", (job,)))

    def media(self, name, *args):
        path = self.source / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(path)], check=True)
        return path

    def test_full_decode_copy_duplicate_audit_repair_and_snapshot(self):
        original = self.media("clip.mkv", "-f", "lavfi", "-i", "color=size=32x32:rate=2",
                              "-f", "lavfi", "-i", "sine", "-map", "0:v", "-map", "1:a",
                              "-map", "1:a", "-t", "1", "-c:v", "ffv1", "-c:a", "pcm_s16le")
        nested = self.source / "album"
        nested.mkdir()
        duplicate = nested / "renamed.mkv"
        shutil.copyfile(original, duplicate)
        self.media("still.png", "-f", "lavfi", "-i", "color=size=16x16", "-frames:v", "1")
        audio = self.media("audio.wav", "-f", "lavfi", "-i", "sine", "-t", "1")
        (self.source / "broken.wav").write_bytes(audio.read_bytes()[:-503])
        (self.source / "document.txt").write_text("this is not media")
        job = self.job("backup")
        states = self.states(job)
        self.assertEqual(sorted(states.values()), ["duplicate","failed","failed","passed","passed","passed"])
        self.assertEqual(states["broken.wav"], "failed")
        self.assertIn("duplicate", backup_report.report(self.db, job, "", 0))
        self.assertEqual(len(backup_report.dashboard(self.db, 0)["jobs"]), 1)
        digest = worker.sha256(original)
        dest = self.archive / self.db.execute("SELECT destination FROM archive WHERE hash=?", (digest,)).fetchone()[0]
        self.assertEqual(worker.sha256(dest), digest)
        original.unlink()
        moved = nested / "another-name.bin"
        duplicate.rename(moved)
        dest.write_bytes(b"damaged")
        self.assertIn("repaired", self.states(self.job("audit")).values())
        self.assertEqual(worker.sha256(dest), digest)
        moved.unlink()
        dest.write_bytes(b"damaged again")
        self.assertIn("unrepaired", self.states(self.job("audit")).values())
        self.assertEqual(dest.read_bytes(), b"damaged again")
        worker.snapshot(self.db)
        snapshot = next((store.DATA / "auto-backup-db-backups").glob("*.sqlite3"))
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT healthy FROM archive WHERE hash=?", (digest,)).fetchone()[0], 0)

    def test_resume_replays_only_unfinished_files(self):
        job = store.enqueue(self.db, "backup")
        self.assertEqual(store.enqueue(self.db, "backup"), job)
        with self.db:
            self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
            self.db.executemany("INSERT INTO files(job,path,state) VALUES(?,?,?)",
                                [(job,"done","passed"),(job,"interrupted","copying")])
        worker.recover(self.db)
        self.assertEqual(self.states(job), {"done":"passed","interrupted":"queued"})
        with self.assertRaises((ValueError, OSError)):
            store.enqueue(self.db, "backup", "..")

    def test_hourly_snapshots_and_overdue_monthly_audit(self):
        store.enqueue(self.db, "backup")
        with self.db:
            self.db.execute("INSERT INTO archive VALUES('baseline','media/one',20,1,'2020-01-01')")
        worker.maintenance_tick(self.db)
        worker.maintenance_tick(self.db)
        def count(action):
            return self.db.execute("SELECT count(*) FROM events WHERE action=?", (action,)).fetchone()[0]
        self.assertEqual(count("monthly audit requested"), 1)
        self.assertEqual(count("snapshot saved"), 1)
        with self.db:
            self.db.execute("UPDATE events SET at=datetime('now','-61 minutes') WHERE action='snapshot saved'")
        worker.maintenance_tick(self.db)
        self.assertEqual(count("snapshot saved"), 2)

    def test_destination_hash_mismatch_never_publishes(self):
        audio = self.media("audio.wav", "-f", "lavfi", "-i", "sine", "-t", "0.1")
        real_hash = worker.sha256
        def bad_transfer(path, progress=None):
            if path.suffix == ".partial":
                path.write_bytes(b"bad transfer")
            return real_hash(path, progress)
        with patch.object(worker, "sha256", side_effect=bad_transfer):
            job = self.job("backup")
        self.assertEqual(self.states(job)[audio.name], "failed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM archive").fetchone()[0], 0)
        self.assertFalse(list(self.archive.rglob("*.wav")))
