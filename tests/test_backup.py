import json
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
        self.assertEqual(sorted(states.values()), ["duplicate","failed","passed","passed","passed","passed"])
        self.assertEqual(states["broken.wav"], "failed")
        self.assertEqual(states["document.txt"], "passed")
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

    def test_flac_uses_probed_container_and_reports_decode_errors(self):
        cover = self.media("art.png", "-f", "lavfi", "-i", "color=size=32x32", "-frames:v", "1")
        audio = self.media("cover.flac", "-f", "lavfi", "-i", "sine=duration=1",
                           "-i", str(cover), "-c:a", "flac", "-c:v", "copy",
                           "-disposition:v", "attached_pic")
        cover.unlink()
        renamed = audio.rename(self.source / "wrong.wav")
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as launch:
            detail = worker.media_check(renamed, lambda message: None)
        decode = next(call.args[0] for call in launch.call_args_list if call.args[0][0] == "ffmpeg")
        self.assertEqual(decode[decode.index("-i")-2:decode.index("-i")], ["-f", "flac"])
        self.assertIn("audio flac", detail)
        self.assertIn("video png", detail)
        renamed.write_bytes(renamed.read_bytes()[:-53])
        job = self.job("backup")
        row = self.db.execute("SELECT state,detail FROM files WHERE job=?", (job,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertIn("ffprobe format=flac, FFmpeg input=flac", row["detail"])
        self.assertIn("FFmpeg decode failed", row["detail"])

    def test_flac_fallback_requires_matching_embedded_checksum(self):
        path = self.media("audio.flac", "-f", "lavfi", "-i", "sine=duration=1")
        worker.verify_flac_checksum(path)
        # Native FLAC STREAMINFO starts at offset 8, with the MD5 at offset 26.
        with path.open("r+b") as stream:
            stream.seek(26)
            stream.write(bytes.fromhex("11" * 16))
        with self.assertRaisesRegex(ValueError, "differs from the embedded"):
            worker.verify_flac_checksum(path)

    def test_non_media_routing_and_failure_log(self):
        for name, mime, tool in [
                ("renamed.bin", "application/pdf", "qpdf"),
                ("sheet.XLS", "application/octet-stream", sys.executable),
                ("sheet.xlsx", "application/zip", "unzip"),
                ("data.bin", "application/octet-stream", None)]:
            with self.subTest(name=name), patch.object(worker, "checked_output",
                    side_effect=[mime, ""]) as check, patch.object(worker, "media_check") as media:
                worker.validate_file(self.source / name, lambda message: None)
                commands = [call.args[0][0] for call in check.call_args_list]
                self.assertEqual(commands, ["file", tool] if tool else ["file"])
                media.assert_not_called()

        (self.source / "damaged.pdf").write_bytes(b"%PDF")
        diagnostic = "first warning\n" + "x" * 2500 + "\nlast error"
        with patch.object(subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, "application/pdf", ""),
                subprocess.CompletedProcess([], 2, "checking file", diagnostic)]):
            job = self.job("backup")
        row = self.db.execute("SELECT state,detail FROM files WHERE job=?", (job,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertIn("validating: qpdf exited with status 2", row["detail"])
        self.assertIn("checking file\n" + diagnostic, row["detail"])

    def test_success_history_survives_restart_rescan_and_audit(self):
        source = self.source / "original.wav"
        self.media(source.name, "-f", "lavfi", "-i", "sine", "-t", "0.1")
        first_job = self.job("backup")
        def first_result():
            return self.db.execute("SELECT * FROM files WHERE job=? AND path=?",
                                   (first_job, source.name)).fetchone()

        original = dict(first_result())
        self.assertEqual(original["state"], "passed")
        self.db.close()
        store.initialize()
        self.db = store.connect()
        self.addCleanup(self.db.close)
        worker.recover(self.db)

        renamed = source.rename(self.source / "renamed.wav")
        self.assertEqual(self.states(self.job("backup")), {renamed.name: "duplicate"})
        renamed.unlink()
        self.assertEqual(self.states(self.job("audit")), {original["destination"]: "verified"})
        self.assertEqual(dict(first_result()), original)
        archive = self.db.execute("SELECT hash,destination FROM archive").fetchall()
        self.assertEqual([tuple(row) for row in archive], [(original["hash"], original["destination"])])

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

    def test_configured_folders_are_saved_with_job(self):
        config = store.default_config()
        app = config["app"]
        source = app["source_folders"][0]
        archive = app["backup_folder"]
        for folder in ("first", "second", "excluded"):
            (self.source / folder).mkdir()
            self.media(folder + "/clip.wav", "-f", "lavfi", "-i", "sine", "-t", "0.1")
        app.update(source_folders=[source+"/first", source+"/second"], backup_folder=archive+"/family")
        settings = store.DATA / "config.json"
        settings.write_text(json.dumps(config))
        job = store.enqueue(self.db, "backup")
        with self.assertRaises(ValueError):
            store.enqueue(self.db, "backup", "excluded")
        settings.write_text(json.dumps(store.default_config()))
        worker.run_job(self.db, self.db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())
        self.assertEqual(set(self.states(job)), {"first/clip.wav", "second/clip.wav"})
        self.assertEqual(sorted(self.states(job).values()), ["duplicate", "passed"])
        self.assertEqual(len(list((self.archive / "family").rglob("*.wav"))), 1)
        self.assertIn("verified", self.states(self.job("audit")).values())

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
