"""Single durable file worker plus an independent SQLite snapshot timer."""
import calendar
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import backup_store as store


def next_month():
    now = datetime.now(timezone.utc)
    year, month = now.year + (now.month == 12), now.month % 12 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day).strftime("%Y-%m-%d %H:%M:%S")


def sha256(path, progress=None):
    with path.open("rb") as stream:
        if progress is None:
            return hashlib.file_digest(stream, "sha256").hexdigest()
        digest = hashlib.sha256()
        size = os.fstat(stream.fileno()).st_size
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
            progress(f"{stream.tell() * 100 // max(1, size)}% read")
        return digest.hexdigest()


def progress_writer(db, item):
    last = 0.0
    def update(message):
        nonlocal last
        now = time.monotonic()
        if now - last >= 2:
            with db:
                db.execute("UPDATE files SET progress=? WHERE id=?", (message, item["id"]))
            last = now
    return update


def media_check(path, progress):
    probe = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                            "stream=index,codec_type:format=format_name", "-of", "json",
                            str(path)], capture_output=True, text=True, errors="replace")
    if probe.returncode:
        raise ValueError(probe.stderr.strip()[-2000:] or "Unrecognized or unreadable media")
    info = json.loads(probe.stdout)
    streams = [s for s in info.get("streams", []) if s["codec_type"] in ("audio", "video")]
    media_format = info.get("format", {}).get("format_name")
    if not streams or media_format in {"tty", "bin", "data", "hls", "dash", "concat", "sdp"}:
        raise ValueError("No supported standalone audio, photo, or video")
    # Explicit stream maps keep secondary audio tracks and additional video streams.
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-xerror",
               "-err_detect", "crccheck+bitstream+buffer+explode", "-abort_on",
               "empty_output_stream", "-threads", "1", "-protocol_whitelist", "file,pipe"]
    if media_format == "image2":
        command.extend(["-pattern_type", "none"])
    command.extend(["-i", str(path)])
    for stream in streams:
        command.extend(["-map", f"0:{stream['index']}"])
    command.extend(["-threads", "1", "-progress", "pipe:1", "-nostats", "-f", "null", "-"])
    with tempfile.TemporaryFile() as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True) as process:
            for update in process.stdout:
                if update.startswith("out_time="):
                    progress("Decoded through " + update.split("=", 1)[1].strip())
            result = process.wait()
        log.seek(0)
        last = ""
        for raw in log:
            line = raw.decode(errors="replace").strip()
            last = line[-1500:] or last
            if any(marker in line.lower() for marker in
                   ("corrupt", "truncat", "eoi missing", "concealing", "invalid data",
                    "decode error", "ended prematurely", "partial file")):
                raise ValueError(last)
        if result:
            raise ValueError(last or f"FFmpeg exited with status {result}")


def walk(root, on_error):
    # Do not follow links, cross into the archive bind mount, or ingest special files.
    archive_stat = store.ARCHIVE.stat()
    for directory, children, names in os.walk(root, onerror=on_error):
        parent = Path(directory)
        if parent.samefile(store.ARCHIVE):
            children.clear()
            continue
        for name in children[:]:
            child = parent / name
            try:
                if child.is_symlink() or os.path.samestat(child.stat(), archive_stat):
                    children.remove(name)
            except OSError as exc:
                children.remove(name)
                on_error(exc)
        for name in names:
            path = parent / name
            try:
                if not path.is_symlink() and path.is_file():
                    yield path
            except OSError as exc:
                on_error(exc)


def discover(db, job):
    with db:
        db.execute("UPDATE jobs SET state='discovering' WHERE id=?", (job["id"],))
        store.event(db, "discovering", job["folder"], job["id"])
    if job["kind"] == "backup":
        root = store.source_folder(job["folder"])
        def record_error(exc):
            with db:
                db.execute("INSERT OR IGNORE INTO files(job,path,state,detail) VALUES(?,?,'failed',?)",
                           (job["id"], str(exc.filename), str(exc)))
                store.event(db, "discovery error", str(exc), job["id"])
        last_commit = time.monotonic()
        for path in walk(root, record_error):
            db.execute("INSERT OR IGNORE INTO files(job,path) VALUES(?,?)",
                       (job["id"], path.relative_to(store.SOURCE.resolve()).as_posix()))
            if db.total_changes % 250 == 0 or time.monotonic() - last_commit >= 1:
                db.commit()
                last_commit = time.monotonic()
    else:
        due = "WHERE due <= CURRENT_TIMESTAMP" if job["kind"] == "monthly" else ""
        db.execute(f"""INSERT OR IGNORE INTO files(job,path,hash,destination)
                       SELECT ?,destination,hash,destination FROM archive {due}""", (job["id"],))
    with db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
        store.event(db, "discovery complete", job=job["id"])


def publish(db, item, source, digest, relative):
    target = store.ARCHIVE / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = store.ARCHIVE / ".auto-backup-staging"
    staging.mkdir(exist_ok=True)
    partial = staging / f"{digest}.partial"
    with db:
        store.stage(db, item, "copying", str(source), digest, relative)
    # Delta transfer reuses an interrupted partial; the final destination stays untouched.
    result = subprocess.run([shutil.which("rsync") or "rsync", "--inplace", "--no-whole-file", "--ignore-times",
                             "--perms", "--chmod=Fu=rw,Fgo=r",
                             "--", str(source), str(partial)], capture_output=True,
                            text=True, errors="replace")
    if result.returncode:
        raise OSError(result.stderr.strip()[-2000:] or "rsync failed")
    with db:
        store.stage(db, item, "verifying", relative)
    if sha256(partial, progress_writer(db, item)) != digest:
        partial.unlink()
        raise ValueError("Destination SHA-256 mismatch")
    with partial.open("rb+") as stream:
        os.fsync(stream.fileno())
    partial.replace(target)
    if os.name == "posix":
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def backup(db, item):
    source = (store.SOURCE / item["path"]).resolve(strict=True)
    if not source.is_relative_to(store.SOURCE.resolve()):
        raise ValueError("Source outside configured root")
    with db:
        store.stage(db, item, "hashing")
    digest = sha256(source, progress_writer(db, item))
    known = db.execute("SELECT * FROM archive WHERE hash=?", (digest,)).fetchone()
    size = source.stat().st_size
    if known:
        relative = known["destination"]
        target = store.ARCHIVE / relative
        if known["healthy"] and target.is_file() and target.stat().st_size == size:
            with db:
                store.stage(db, item, "duplicate", "Already backed up", digest, relative)
            return
        with db:
            db.execute("UPDATE archive SET healthy=0 WHERE hash=?", (digest,))
            store.stage(db, item, "repairing", "Source matches trusted digest", digest, relative)
    else:
        with db:
            store.stage(db, item, "validating", digest=digest)
        media_check(source, progress_writer(db, item))
        with db:
            store.event(db, "decode passed", item["path"], item["job"], item["id"])
        # The digest directory keeps distinct versions with the same name independent.
        relative = (Path("media") / digest[:2] / digest / source.name).as_posix()
    publish(db, item, source, digest, relative)
    with db:
        db.execute("""INSERT INTO archive VALUES(?,?,?,1,?) ON CONFLICT(hash)
                      DO UPDATE SET healthy=1,due=excluded.due""",
                   (digest, relative, size, next_month()))
        store.stage(db, item, "repaired" if known else "passed", relative)


def audit(db, item):
    digest = item["hash"]
    row = db.execute("SELECT * FROM archive WHERE hash=?", (digest,)).fetchone()
    path = store.ARCHIVE / row["destination"]
    with db:
        store.stage(db, item, "auditing", row["destination"])
    try:
        healthy = sha256(path, progress_writer(db, item)) == digest
    except OSError as exc:
        healthy = False
        with db:
            store.event(db, "archive read error", str(exc), item["job"], item["id"])
    if healthy:
        with db:
            db.execute("UPDATE archive SET healthy=1,due=? WHERE hash=?", (next_month(), digest))
            store.stage(db, item, "verified", "SHA-256 matches baseline")
        return
    with db:
        db.execute("UPDATE archive SET healthy=0,due=? WHERE hash=?", (next_month(), digest))
        store.stage(db, item, "repairing", "Missing or damaged backup; searching source tree")

    def record_error(exc):
        with db:
            store.event(db, "candidate read error", str(exc), item["job"], item["id"])
    # Previously backed-up paths and reported duplicates usually resolve repairs
    # without rereading an entire NAS. Fall back to walking for moved/renamed files.
    def candidates():
        for match in db.execute("""SELECT DISTINCT f.path FROM files f JOIN jobs j ON j.id=f.job
                                   WHERE f.hash=? AND j.kind='backup'""", (digest,)):
            candidate = store.SOURCE / match["path"]
            if candidate.resolve().is_relative_to(store.SOURCE.resolve()) and candidate.is_file():
                yield candidate
        yield from walk(store.SOURCE, record_error)

    for candidate in candidates():
        try:
            if candidate.stat().st_size != row["size"] or sha256(candidate) != digest:
                continue
        except OSError as exc:
            record_error(exc)
            continue
        publish(db, item, candidate, digest, row["destination"])
        with db:
            db.execute("UPDATE archive SET healthy=1 WHERE hash=?", (digest,))
            store.stage(db, item, "repaired", str(candidate))
        return
    with db:
        store.stage(db, item, "unrepaired", "No readable candidate matched the original SHA-256")


def run_job(db, job):
    if job["state"] != "running":
        discover(db, job)
    while True:
        item = db.execute("SELECT * FROM files WHERE job=? AND state='queued' ORDER BY id LIMIT 1",
                          (job["id"],)).fetchone()
        if not item:
            break
        try:
            if job["kind"] == "backup":
                backup(db, item)
            else:
                audit(db, item)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            with db:
                store.stage(db, item, "failed", str(exc))
    with db:
        failures = db.execute("SELECT COUNT(*) FROM files WHERE job=? AND state IN ('failed','unrepaired')",
                              (job["id"],)).fetchone()[0]
        state = "completed with errors" if failures else "completed"
        db.execute("UPDATE jobs SET state=?,finished=CURRENT_TIMESTAMP WHERE id=?", (state, job["id"]))
        store.event(db, state, job=job["id"])


def snapshot(db):
    directory = store.DATA / "auto-backup-db-backups"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"auto-backup-{stamp}.sqlite3"
    temp = directory / "snapshot.partial"
    with closing(sqlite3.connect(temp)) as dest:
        db.backup(dest, pages=256, sleep=0.05)
        if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite snapshot failed integrity check")
    temp.replace(target)
    with db:
        store.event(db, "snapshot saved", target.name)
    for old in sorted(directory.glob("auto-backup-????????T??????Z.sqlite3"))[:-48]:
        old.unlink()


def maintenance_tick(db):
    busy = db.execute("SELECT 1 FROM jobs WHERE finished IS NULL LIMIT 1").fetchone()
    due = db.execute("""SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM events
                        WHERE action='snapshot saved' AND at>datetime('now','-1 hour'))""").fetchone()
    if busy and due:
        snapshot(db)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM archive WHERE due<=CURRENT_TIMESTAMP LIMIT 1").fetchone():
            if not db.execute("SELECT 1 FROM jobs WHERE kind IN ('monthly','audit') AND finished IS NULL").fetchone():
                job = db.execute("INSERT INTO jobs(kind,folder) VALUES('monthly','.')").lastrowid
                store.event(db, "monthly audit requested", job=job)


def maintenance():
    with closing(store.connect()) as db:
        while True:
            try:
                maintenance_tick(db)
            except (OSError, ValueError, sqlite3.Error) as exc:
                with db:
                    store.event(db, "maintenance error", str(exc))
            time.sleep(15)


def recover(db):
    with db:
        db.execute("""UPDATE files SET state='queued' WHERE state IN
                      ('hashing','validating','copying','verifying','auditing','repairing')""")
        store.event(db, "worker started", "Interrupted files returned to queue")


def main():
    with closing(store.connect()) as db:
        recover(db)
        threading.Thread(target=maintenance, daemon=True).start()
        while True:
            job = db.execute("SELECT * FROM jobs WHERE finished IS NULL ORDER BY id LIMIT 1").fetchone()
            if not job:
                time.sleep(1)
                continue
            try:
                run_job(db, job)
            except (ValueError, OSError) as exc:
                with db:
                    db.execute("UPDATE jobs SET state='failed',error=?,finished=CURRENT_TIMESTAMP WHERE id=?",
                               (str(exc), job["id"]))
                    store.event(db, "job failed", str(exc), job["id"])


if __name__ == "__main__":
    main()
