#!/usr/bin/env bash
# Purpose: Stage config and redeploy the app container through the platform compose stack.
# Usage: ./deploy.sh
# Sample: ./deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/app-stack.sh"

app_before_clean() (
    local env_file
    env_file="$(require_stack_env_file)"
    set -a
    source "$APP_ROOT_DIR/$env_file"
    set +a
    "$(require_python_bin)" - <<'PY'
import os
from pathlib import Path

base = Path(os.environ.get("PERSISTENT_APP_ROOT") or "/apps/nas-apps") / "auto-backup"
source = Path(os.environ.get("AUTO_BACKUP_SOURCE_ROOT") or base / "source").resolve(strict=True)
archive = Path(os.environ.get("AUTO_BACKUP_DESTINATION_ROOT") or base / "archive").resolve(strict=True)
if source.is_relative_to(archive) or archive.is_relative_to(source):
    raise SystemExit("Auto Backup source and archive must be separate, non-overlapping directories.")
if not source.is_dir() or not os.access(source, os.R_OK | os.X_OK):
    raise SystemExit(f"Auto Backup cannot read source directory {source}")
if not archive.is_dir() or not os.access(archive, os.W_OK | os.X_OK):
    raise SystemExit(f"Auto Backup cannot write archive directory {archive}")
PY
)

app_stage_runtime_config() {
    default_stage_runtime_config warm 0
}

standard_deploy_main
