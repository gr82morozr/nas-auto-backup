#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import closing
import backup_store
import backup_report
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from apps_common.platform_control_service import json_response
from apps_common.platform_runtime_status import runtime_status_payload
from apps_common.platform_status import utc_now
from apps_common.platform_settings_ui import read_pretty_json, render_settings_page, save_json_text


DEFAULT_PORT = 8000
APP_DIR = Path(__file__).resolve().parent
APP_MODULE_PATH = APP_DIR / "auto-backup.py"
LOG_PREFIX = "[auto-backup-control]"
SETTINGS_PATHS = {"/settings", "/auto-backup/control/settings"}
SETTINGS_SAVE_PATHS = {"/config/save", "/auto-backup/control/config/save"}
STATUS_PATHS = {"/healthz", "/auto-backup/control/healthz", "/api/status", "/auto-backup/control/api/status"}
MESSAGE_HUB_INBOUND_PATHS = {"/api/message-hub/inbound-message", "/auto-backup/control/api/message-hub/inbound-message"}


def load_auto_backup_module():
    spec = importlib.util.spec_from_file_location("auto_backup_module", APP_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Auto Backup module from {APP_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUTO_BACKUP = load_auto_backup_module()
CONTROL_SERVICE_STARTED_AT = utc_now()
CONFIG_LOCK = RLock()


def html_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, body_text: str) -> None:
    body = body_text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "AutoBackupControl/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{LOG_PREFIX} {self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        route = path.removeprefix("/auto-backup/control")
        if route in ("/progress", "/report"):
            query = parse_qs(urlparse(self.path).query)
            try:
                page = min(10**12, max(0, int(query.get("page", ["0"])[0])))
                with closing(backup_store.connect()) as db:
                    if route == "/progress":
                        json_response(self, HTTPStatus.OK, backup_report.dashboard(db, page))
                    else:
                        body = backup_report.report(db, int(query.get("job", ["0"])[0]),
                                                    query.get("state", [""])[0], page,
                                                    query.get("events", ["0"])[0] == "1")
                        html_response(self, HTTPStatus.OK, body)
            except (ValueError, OverflowError) as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path in SETTINGS_PATHS:
            with CONFIG_LOCK:
                AUTO_BACKUP.ensure_runtime_config_path()
            config_path = AUTO_BACKUP.DEFAULT_PERSISTENT_CONFIG_PATH
            html = render_settings_page(
                title="Auto Backup",
                config_path=config_path,
                config_text=read_pretty_json(config_path),
                save_path="/auto-backup/control/config/save",
                return_path=AUTO_BACKUP.PUBLIC_OUTPUT_PATH,
                return_label="Open Overview",
            )
            html_response(self, HTTPStatus.OK, html)
            return
        if path in STATUS_PATHS:
            json_response(
                self,
                HTTPStatus.OK,
                runtime_status_payload(
                    "auto-backup",
                    fallback_started_at=CONTROL_SERVICE_STARTED_AT,
                    runtime_dir=AUTO_BACKUP.resolve_runtime_dir(),
                    uptime_source="app-runtime",
                    extras={
                        "config_path": str(AUTO_BACKUP.DEFAULT_RUNTIME_CONFIG_PATH),
                        "output_path": str(AUTO_BACKUP.OUTPUT_PATH),
                    },
                ),
            )
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown route: {path}"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path.removeprefix("/auto-backup/control") == "/jobs":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.headers.get_content_type() != "application/json" or not 0 < size <= 8192:
                    raise ValueError("Expected a small JSON job request")
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict) or not isinstance(request.get("folder", "."), str):
                    raise ValueError("Invalid job request")
                with closing(backup_store.connect()) as db:
                    job = backup_store.enqueue(db, request.get("kind"), request.get("folder", "."))
                json_response(self, HTTPStatus.ACCEPTED, {"job": job})
            except (ValueError, OSError) as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path in SETTINGS_SAVE_PATHS:
            with CONFIG_LOCK:
                self._handle_settings_save()
            return
        if path not in MESSAGE_HUB_INBOUND_PATHS:
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown route: {path}"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            content_length = 0
        raw_body = self.rfile.read(max(0, content_length)).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Expected a JSON object"})
            return
        dispatch = payload.get("dispatch", {}) if isinstance(payload, dict) and isinstance(payload.get("dispatch"), dict) else {}
        message = payload.get("message", {}) if isinstance(payload, dict) and isinstance(payload.get("message"), dict) else {}
        action = str(dispatch.get("action", "") or payload.get("action", "") or message.get("command", "") or "status").strip().lower()

        if action == "status":
            with CONFIG_LOCK:
                config = AUTO_BACKUP.load_config()
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "auto-backup",
                    "action": action,
                    "title": str(config.get("app", {}).get("title", "Auto Backup")),
                },
            )
            return
        if action in {"refresh", "refresh-output"}:
            with CONFIG_LOCK:
                config = AUTO_BACKUP.load_config()
                output_path = AUTO_BACKUP.write_output(config)
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "auto-backup",
                    "action": action,
                    "message": f"Refreshed landing page at {output_path}.",
                },
            )
            return

        json_response(
            self,
            HTTPStatus.BAD_REQUEST,
            {"ok": False, "service": "auto-backup", "error": f"Unsupported action: {action}"},
        )

    def _handle_settings_save(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            content_length = 0
        raw_body = self.rfile.read(max(0, content_length)).decode("utf-8", errors="replace")
        fields = parse_qs(raw_body, keep_blank_values=True)
        config_text = (fields.get("config_text") or [""])[-1]
        AUTO_BACKUP.ensure_runtime_config_path()
        config_path = AUTO_BACKUP.DEFAULT_PERSISTENT_CONFIG_PATH
        try:
            save_json_text(
                config_path,
                config_text,
                validate=lambda path: AUTO_BACKUP.load_config(Path(path)),
            )
            AUTO_BACKUP.write_output(AUTO_BACKUP.load_config())
            html = render_settings_page(
                title="Auto Backup",
                config_path=config_path,
                config_text=read_pretty_json(config_path),
                save_path="/auto-backup/control/config/save",
                return_path=AUTO_BACKUP.PUBLIC_OUTPUT_PATH,
                return_label="Open Overview",
                message="Persistent config saved successfully.",
            )
            html_response(self, HTTPStatus.OK, html)
        except Exception as exc:
            html = render_settings_page(
                title="Auto Backup",
                config_path=config_path,
                config_text=config_text,
                save_path="/auto-backup/control/config/save",
                return_path=AUTO_BACKUP.PUBLIC_OUTPUT_PATH,
                return_label="Open Overview",
                error_text=str(exc),
            )
            html_response(self, HTTPStatus.BAD_REQUEST, html)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", DEFAULT_PORT), ControlHandler)
    print(f"{LOG_PREFIX} Listening on 0.0.0.0:{DEFAULT_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
