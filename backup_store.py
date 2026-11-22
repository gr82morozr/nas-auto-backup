"""Persistent queue and audit trail shared by the HTTP service and worker."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path


DATA = Path(os.getenv("COLD_DIR", "/persistent"))
DB = DATA / "auto-backup.sqlite3"
SOURCE = Path(os.getenv("BACKUP_SOURCE_DIR", "/media/source"))
ARCHIVE = Path(os.getenv("BACKUP_ARCHIVE_DIR", "/media/archive"))
ACTIVE = ("queued", "hashing", "validating", "copying", "verifying", "auditing", "repairing")


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
    if kind == "backup":
        folder = source_folder(folder).relative_to(SOURCE.resolve()).as_posix()
    elif kind == "audit":
        folder = "."
    else:
        raise ValueError("Unknown job type.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT id FROM jobs WHERE kind=? AND folder=? AND finished IS NULL",
                              (kind, folder)).fetchone()
        if existing:
            return existing["id"]
        job = db.execute("INSERT INTO jobs(kind,folder) VALUES(?,?)", (kind, folder)).lastrowid
        event(db, "requested", folder, job)
        return job


def stage(db, item, state, detail="", digest=None, destination=None):
    db.execute("""UPDATE files SET state=?, detail=?, progress='', hash=coalesce(?,hash),
                  destination=coalesce(?,destination) WHERE id=?""",
               (state, detail, digest, destination, item["id"]))
    event(db, state, detail, item["job"], item["id"])
