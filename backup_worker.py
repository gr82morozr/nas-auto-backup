"""Single durable file worker plus an independent SQLite snapshot timer."""
import calendar
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
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


def checked_output(command):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, errors="replace")
    if result.returncode:
        diagnostics = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        raise ValueError(f"{Path(command[0]).name} exited with status {result.returncode}\n{diagnostics}")
    return result.stdout


def validate_file(path, progress):
    mime = checked_output(["file", "--brief", "--mime-type", "--", str(path)]).strip()
    suffix = path.suffix.lower()
    if suffix == ".pdf" or mime == "application/pdf":
        checked_output(["qpdf", "--check", str(path)])
        return "PDF structure checked by qpdf"
    if suffix == ".xls" or mime == "application/vnd.ms-excel":
        checked_output([sys.executable, "-c",
                        "import sys, xlrd; xlrd.open_workbook(sys.argv[1])", str(path)])
        return "XLS workbook read by xlrd"
    if suffix in {".zip", ".xlsx", ".xlsm", ".docx", ".pptx", ".ods", ".odt"} or mime == "application/zip":
        checked_output(["unzip", "-tq", str(path)])
        return "ZIP entries and CRCs checked by unzip"
    media_types = ("image/", "audio/", "video/")
    named_type = mimetypes.guess_type(path.name)[0] or ""
    ambiguous = mime in {"application/octet-stream", "text/plain", "inode/x-empty", "application/x-empty"}
    if (mime.startswith(media_types) or mime in {"application/ogg", "application/vnd.rn-realmedia"}
            or (ambiguous and named_type.startswith(media_types))):
        return media_check(path, progress)
    return f"SHA-256 copy verification only ({mime})"


def media_check(path, progress):
    output = checked_output(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                             "-show_entries", "stream=index,codec_type,codec_name:format=format_name",
                             "-of", "json", str(path)])
    info = json.loads(output)
    streams = [s for s in info.get("streams", []) if s["codec_type"] in ("audio", "video")]
    formats = (info.get("format", {}).get("format_name") or "").split(",")
    # ffprobe lists aliases of the same demuxer, e.g. "matroska,webm".
    demuxer = formats[0]
    if not demuxer or not streams or {"tty", "bin", "data", "hls", "dash", "concat", "sdp"}.intersection(formats):
        raise ValueError(f"No supported standalone media in ffprobe result: {output.strip()}")
    stream_info = ", ".join(f"#{s['index']} {s['codec_type']} {s.get('codec_name', 'unknown')}" for s in streams)
    description = f"ffprobe format={','.join(formats)}, FFmpeg input={demuxer}, streams={stream_info}"
    # Explicit stream maps keep secondary audio tracks and additional video streams.
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-xerror",
               "-err_detect", "crccheck+bitstream+buffer+explode", "-abort_on",
               "empty_output_stream", "-threads", "1", "-protocol_whitelist", "file,pipe",
               "-f", demuxer]
    if demuxer == "image2":
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
        markers = (b"corrupt", b"truncat", b"eoi missing", b"concealing", b"invalid data",
                   b"decode error", b"ended prematurely", b"partial file")
        if result or any(any(marker in line.lower() for marker in markers) for line in log):
            log.seek(0)
            raise ValueError(f"{description}\nFFmpeg decode failed (exit {result})\n{log.read().decode(errors='replace')}")
    return f"Media streams decoded. {description}"


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
        roots = json.loads(job["sources"]) if job["sources"] else [job["folder"]]
        def record_error(exc):
            with db:
                db.execute("INSERT OR IGNORE INTO files(job,path,state,detail) VALUES(?,?,'failed',?)",
                           (job["id"], str(exc.filename), str(exc)))
                store.event(db, "discovery error", str(exc), job["id"])
        last_commit = time.monotonic()
        paths = (path for folder in roots for path in walk(store.source_folder(folder), record_error))
        for path in paths:
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
        discovered = db.execute("SELECT count(*) FROM files WHERE job=?", (job["id"],)).fetchone()[0]
        store.event(db, "discovery complete", job=job["id"], files=discovered)


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
        raise OSError(f"rsync exited with status {result.returncode}\n{result.stderr.strip()}")
    with db:
        store.stage(db, item, "verifying", relative)
    actual = sha256(partial, progress_writer(db, item))
    if actual != digest:
        partial.unlink()
        raise ValueError(f"Destination SHA-256 mismatch; expected {digest}, read {actual}")
    with partial.open("rb+") as stream:
        os.fsync(stream.fileno())
    partial.replace(target)
    if os.name == "posix":
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def backup(db, item, archive_prefix):
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
        method = validate_file(source, progress_writer(db, item))
        with db:
            store.event(db, "source check", method, item["job"], item["id"])
        # The digest directory keeps distinct versions with the same name independent.
        relative = (Path(archive_prefix) / "media" / digest[:2] / digest / source.name).as_posix()
    publish(db, item, source, digest, relative)
    with db:
        db.execute("""INSERT INTO archive VALUES(?,?,?,1,?) ON CONFLICT(hash)
                      DO UPDATE SET healthy=1,due=excluded.due""",
                   (digest, relative, size, next_month()))
        store.stage(db, item, "repaired" if known else "passed", relative)


def audit(db, item, sources):
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
        mounted = store.SOURCE.resolve()
        roots = [root for folder in sources if (root := (mounted / folder).resolve()).is_relative_to(mounted)]
        for match in db.execute("""SELECT DISTINCT f.path FROM files f JOIN jobs j ON j.id=f.job
                                   WHERE f.hash=? AND j.kind='backup'""", (digest,)):
            candidate = store.SOURCE / match["path"]
            if any(candidate.resolve().is_relative_to(root) for root in roots) and candidate.is_file():
                yield candidate
        for root in roots:
            yield from walk(root, record_error)

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
    repair_sources = json.loads(job["sources"]) if job["sources"] else ["."]
    store.log_activity("processing job", job=job["id"], kind=job["kind"], state=job["state"],
                       sources=repair_sources, archive=job["archive_prefix"])
    if job["state"] != "running":
        discover(db, job)
    while True:
        item = db.execute("SELECT * FROM files WHERE job=? AND state='queued' ORDER BY id LIMIT 1",
                          (job["id"],)).fetchone()
        if not item:
            break
        try:
            if job["kind"] == "backup":
                backup(db, item, job["archive_prefix"])
            else:
                audit(db, item, repair_sources)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            phase = db.execute("SELECT state FROM files WHERE id=?", (item["id"],)).fetchone()[0]
            with db:
                store.stage(db, item, "failed", f"{phase}: {exc}")
    with db:
        counts = dict(db.execute("SELECT state,count(*) FROM files WHERE job=? GROUP BY state", (job["id"],)))
        failures = counts.get("failed", 0) + counts.get("unrepaired", 0)
        state = "completed with errors" if failures else "completed"
        db.execute("UPDATE jobs SET state=?,finished=CURRENT_TIMESTAMP WHERE id=?", (state, job["id"]))
        store.event(db, state, job=job["id"], counts=counts)


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
                sources, archive = store.saved_folders()
                job = db.execute("INSERT INTO jobs(kind,folder,sources,archive_prefix) VALUES('monthly','.',?,?)",
                                 (json.dumps(sources), archive)).lastrowid
                store.event(db, "monthly audit requested", job=job)


def log_heartbeat(db):
    job = db.execute("SELECT id,kind,state FROM jobs WHERE finished IS NULL ORDER BY id LIMIT 1").fetchone()
    if job is None:
        store.log_activity("idle", detail="Waiting for backup or audit requests")
        return
    counts = dict(db.execute("SELECT state,count(*) FROM files WHERE job=? GROUP BY state", (job["id"],)))
    marks = ",".join("?" for _ in store.ACTIVE[1:])
    file = db.execute(f"SELECT id,path,state,progress,hash FROM files WHERE job=? AND state IN ({marks}) LIMIT 1",
                      (job["id"], *store.ACTIVE[1:])).fetchone()
    current = dict(file) if file else {}
    digest = current.pop("hash", "")
    if current.get("state") == "copying" and digest:
        try:
            current["staged_bytes"] = (store.ARCHIVE / ".auto-backup-staging" / f"{digest}.partial").stat().st_size
        except FileNotFoundError:
            pass
    store.log_activity("heartbeat", job=job["id"], kind=job["kind"], state=job["state"],
                       counts=counts, current=current or None)


def maintenance():
    with closing(store.connect()) as db:
        next_heartbeat = 0
        while True:
            try:
                if time.monotonic() >= next_heartbeat:
                    log_heartbeat(db)
                    next_heartbeat = time.monotonic() + 60
                maintenance_tick(db)
            except (OSError, ValueError, sqlite3.Error) as exc:
                with db:
                    store.event(db, "maintenance error", str(exc))
            time.sleep(15)


def recover(db):
    with db:
        reset = db.execute("""UPDATE files SET state='queued' WHERE state IN
                      ('hashing','validating','copying','verifying','auditing','repairing')""")
        store.event(db, "worker started", "Interrupted files returned to queue", requeued=reset.rowcount)


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
