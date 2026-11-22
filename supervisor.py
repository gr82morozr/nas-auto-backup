import runpy
import sys
from pathlib import Path

from apps_common.platform_runtime_status import write_runtime_state
from apps_common.platform_supervisor import MultiProcessSupervisor, ProcessSpec
from backup_store import initialize


APP_DIR = Path(__file__).resolve().parent


def bootstrap():
    initialize()
    app = runpy.run_path(str(APP_DIR / "auto-backup.py"))
    app["write_output"](app["load_config"]())
    write_runtime_state("auto-backup")


if __name__ == "__main__":
    raise SystemExit(MultiProcessSupervisor(
        app_name="auto-backup",
        app_dir=APP_DIR,
        startup_hook=bootstrap,
        processes=[
            ProcessSpec("control-service", [sys.executable, "control-service.py"]),
            ProcessSpec("backup-worker", [sys.executable, "backup_worker.py"]),
        ],
    ).run())
