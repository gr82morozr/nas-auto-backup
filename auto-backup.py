"""Render the Auto Backup overview for the platform reverse proxy."""
import json
import os
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile

from apps_common.platform_runtime_config import backup_invalid_json, copy_file_if_changed, write_json
from apps_common.platform_runtime_status import resolve_runtime_dir
from backup_store import configured_folders, default_config


APP_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_CONFIG_PATH = resolve_runtime_dir() / "config.json"
DEFAULT_PERSISTENT_CONFIG_PATH = Path(os.getenv("COLD_DIR", "/persistent")) / "config.json"
OUTPUT_PATH = Path(os.getenv("OUTPUT_DIR", "/tmp/output")) / "index.html"
PUBLIC_OUTPUT_PATH = "/auto-backup/output/"


def ensure_runtime_config_path():
    defaults = default_config()
    path = DEFAULT_PERSISTENT_CONFIG_PATH
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    except json.JSONDecodeError:
        backup_invalid_json(path)
        config = {}
    # A saved source list is authoritative, including removal of the original root.
    app = config.get("app", {})
    merged = {**config, "app": {**defaults["app"], **app}}
    if merged != config:
        write_json(path, merged)
    copy_file_if_changed(path, DEFAULT_RUNTIME_CONFIG_PATH)
    return DEFAULT_RUNTIME_CONFIG_PATH


def load_config(path=None):
    config = json.loads((path or ensure_runtime_config_path()).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("app"), dict):
        raise ValueError("Expected an app configuration object.")
    title = config["app"].get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("The app title must be a nonempty string.")
    configured_folders(config)
    return config


def write_output(config):
    page = (APP_DIR / "overview.html").read_text(encoding="utf-8")
    page = page.replace("{{title}}", escape(config["app"]["title"]))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=OUTPUT_PATH.parent, delete=False) as staged:
        path = Path(staged.name)
    try:
        path.write_text(page, encoding="utf-8")
        path.chmod(0o644)
        path.replace(OUTPUT_PATH)
    finally:
        path.unlink(missing_ok=True)
    return OUTPUT_PATH
