# Auto Backup

Open `/auto-backup/output/` from NAS Gateway. In **Settings**, edit `app.source_folders` (a list of NAS host directories) and `app.backup_folder` (one NAS host directory). Select folders inside their respective Docker mounts. Use `.` on the dashboard to scan every configured source, or enter a subfolder relative to the source mount. **Start backup** saves the folder selection with the job. A later settings change applies to new jobs. Existing verified files keep their recorded locations for deduplication, audits, and repairs. Closing the browser leaves the job running. **Check archive now** queues an additional integrity check. Times are UTC.

## Storage

| Container path | Purpose |
| --- | --- |
| `/media/source` | Read-only source and repair search tree |
| `/media/archive` | Verified original files under `media/<prefix>/<sha256>/<original name>` and incomplete transfers under `.auto-backup-staging/` |
| `/persistent/auto-backup.sqlite3` | Durable queue, original paths, trusted hashes, verification results and event log |
| `/persistent/auto-backup-db-backups/` | Most recent 48 hourly online database snapshots |
| `/tmp/output/index.html` | Dashboard served by Nginx |

The default host source is `/apps/nasdata/important` and the archive is `/apps/nasdata/important_backup`. Both directories must exist before deployment. The deployment user needs read access to the source and write access to the archive. To override either path, set `AUTO_BACKUP_SOURCE_ROOT` or `AUTO_BACKUP_DESTINATION_ROOT` in the parent `env.conf`. Keep the database on a local filesystem that supports SQLite WAL locking.

## Verification and recovery

The worker hashes each source file and selects a check using its detected MIME type and filename. FFmpeg decodes media streams, qpdf checks PDF structure, and xlrd reads legacy XLS workbooks. For ZIP files and ZIP-based Office documents such as XLSX, unzip tests the archive entries and their CRCs. Other binaries receive SHA-256 copy verification only. This confirms that the archive matches the source bytes. The job event log records the check used. Failed file rows store the phase and diagnostic output in SQLite (`files.detail`), also visible in the job report. Files identical to an existing verified backup are recorded as duplicates, with a link between the source path and the canonical archive path in SQLite. A successful decode cannot establish that a recording was complete before its first scan.

The worker copies new files with rsync, hashes the staged destination independently, then renames it into place. On restart it preserves completed results, rechecks the interrupted file and reuses partial transfers. Monthly audits re-read each due backup, including after downtime. On a mismatch, the worker checks known source paths, then searches the mounted source tree by size and SHA-256. A verified replacement keeps the original digest. Unresolved damage remains in the report.

While jobs are queued or running, a separate timer uses the SQLite online backup API every hour. Snapshots include committed queue and file activity during long decodes. To recover from loss of the live database, stop the app and restore a snapshot into a clean database directory without stale SQLite sidecars. Keep the previous database files together for investigation.

## Management

After publishing the app and platform changes, deploy from the platform checkout with `./deploy-one.sh auto-backup`. Use `./start-one.sh auto-backup`, `./stop-one.sh auto-backup`, and `./showlog-one.sh auto-backup` for routine management. The control service also exposes the standard health, status, settings and message-hub endpoints.
