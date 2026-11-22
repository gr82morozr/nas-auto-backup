"""Persistent queue and audit trail shared by the HTTP service and worker."""
import os
import json
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath


DATA = Path(os.getenv("COLD_DIR", "/persistent"))
DB = DATA / "auto-backup.sqlite3"
SOURCE = Path(os.getenv("BACKUP_SOURCE_DIR", "/media/source"))
ARCHIVE = Path(os.getenv("BACKUP_ARCHIVE_DIR", "/media/archive"))
ACTIVE = ("queued", "hashing", "validating", "copying", "verifying", "auditing", "repairing")


def default_config():
    config = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
    app = config["app"]
    app["source_folders"] = [os.getenv("AUTO_BACKUP_SOURCE_ROOT") or app["source_folders"][0]]
    app["backup_folder"] = os.getenv("AUTO_BACKUP_DESTINATION_ROOT") or app["backup_folder"]
    return config


def configured_folders(config):
    defaults = default_config()["app"]
    app = config["app"]
    sources = app.get("source_folders")
    if not isinstance(sources, list) or not sources:
        raise ValueError("source_folders must be a nonempty list of host directories.")

    def relative(value, host_root, mounted_root):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Each folder must be an absolute NAS host path.")
        path = PurePosixPath(value)
        base = PurePosixPath(host_root)
        if not path.is_absolute() or not path.is_relative_to(base) or ".." in path.parts:
            raise ValueError(f"Choose a host directory inside {base}: {value}")
        rel = path.relative_to(base)
        local = mounted_root / rel.as_posix()
        if not local.resolve().is_relative_to(mounted_root.resolve()):
            raise ValueError(f"Folder resolves outside its mount: {value}")
        return rel.as_posix()

    roots = sorted({relative(s, defaults["source_folders"][0], SOURCE) for s in sources})
    return roots, relative(app.get("backup_folder"), defaults["backup_folder"], ARCHIVE)


def saved_folders():
    path = DATA / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default_config()
    return configured_folders(config)


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, folder TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'waiting', created TEXT DEFAULT CURRENT_TIMESTAMP,
                finished TEXT, error TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id),
                path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                hash TEXT DEFAULT '', destination TEXT DEFAULT '', detail TEXT DEFAULT '',
                UNIQUE(job,path)
            );
            CREATE INDEX IF NOT EXISTS file_queue ON files(job,state,id);
            CREATE INDEX IF NOT EXISTS file_digest ON files(hash,path);
            CREATE TABLE IF NOT EXISTS archive (
                hash TEXT PRIMARY KEY, destination TEXT UNIQUE NOT NULL,
                size INTEGER NOT NULL, healthy INTEGER NOT NULL, due TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_due ON archive(due);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP,
                job INTEGER, file INTEGER, action TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_job ON events(job,id);
            CREATE INDEX IF NOT EXISTS event_action ON events(action,at);
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
        if "progress" not in columns:
            db.execute("ALTER TABLE files ADD COLUMN progress TEXT DEFAULT ''")
            db.commit()
        columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        with db:
            if "sources" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN sources TEXT")
            if "archive_prefix" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN archive_prefix TEXT NOT NULL DEFAULT '.'")


def event(db, action, detail="", job=None, file=None):
    db.execute("INSERT INTO events(job,file,action,detail) VALUES(?,?,?,?)",
               (job, file, action, detail))


def source_folder(folder):
    root = SOURCE.resolve(strict=True)
    path = (root / folder).resolve(strict=True)
    if Path(folder).is_absolute() or not path.is_relative_to(root) or not path.is_dir():
        raise ValueError("Choose a folder relative to the source root.")
    return path


def enqueue(db, kind, folder="."):
    roots, archive = saved_folders()
    if kind == "backup":
        if folder != ".":
            folder = source_folder(folder).relative_to(SOURCE.resolve()).as_posix()
            if not any(Path(folder).is_relative_to(root) for root in roots):
                raise ValueError("Choose a subfolder inside a configured source folder.")
            roots = [folder]
        for root in roots:
            source_folder(root)
        folder = ", ".join(roots)
    elif kind == "audit":
        folder = "."
    else:
        raise ValueError("Unknown job type.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        sources = json.dumps(roots)
        existing = db.execute("""SELECT id FROM jobs WHERE kind=? AND folder=? AND sources=?
                                 AND archive_prefix=? AND finished IS NULL""",
                              (kind, folder, sources, archive)).fetchone()
        if existing:
            return existing["id"]
        job = db.execute("INSERT INTO jobs(kind,folder,sources,archive_prefix) VALUES(?,?,?,?)",
                         (kind, folder, sources, archive)).lastrowid
        event(db, "requested", folder, job)
        return job


def stage(db, item, state, detail="", digest=None, destination=None):
    db.execute("""UPDATE files SET state=?, detail=?, progress='', hash=coalesce(?,hash),
                  destination=coalesce(?,destination) WHERE id=?""",
               (state, detail, digest, destination, item["id"]))
    event(db, state, detail, item["job"], item["id"])
