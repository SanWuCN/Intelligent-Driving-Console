#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from controller import VERSION, BigCarController, ControllerError


ROOT = Path(__file__).resolve().parents[1]


class ConsoleHandler(SimpleHTTPRequestHandler):
    controller: BigCarController
    static_root: Path

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(self.static_root), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/state"):
            return
        super().log_message(fmt, *args)

    def _json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
        except ValueError:
            raise ControllerError("无效的请求长度")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControllerError("请求 JSON 格式错误")
        if not isinstance(value, dict):
            raise ControllerError("请求体必须是 JSON 对象")
        return value

    def _authorized(self) -> bool:
        provided = self.headers.get("X-Control-Token", "")
        expected = self.controller.control_token
        return bool(expected) and hmac.compare_digest(provided, expected)

    def _security_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def end_headers(self) -> None:
        self._security_headers()
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._json({"ok": True, "version": self.controller.snapshot()["version"], "simulated": self.controller.simulate})
                return
            if parsed.path == "/api/state":
                self._json(self.controller.snapshot())
                return
            if parsed.path == "/api/route":
                name = parse_qs(parsed.query).get("file", [""])[0]
                self._json(self.controller.route_data(name))
                return
            if parsed.path == "/api/rviz.mjpeg":
                self._rviz_stream()
                return
            if parsed.path.startswith("/api/"):
                self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
                return
            path = (self.static_root / parsed.path.lstrip("/")).resolve()
            if parsed.path == "/" or not path.is_file():
                self.path = "/index.html"
            super().do_GET()
        except ControllerError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.controller.log("ERROR", "http", str(exc))
            self._json({"error": "服务器内部错误"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _rviz_stream(self) -> None:
        if self.controller.simulate:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Simulation has no desktop stream")
            return
        display = str(self.controller.config.get("display", ":0"))
        xauthority = str(self.controller.config.get("xauthority", "/run/user/1000/gdm/Xauthority"))
        env = os.environ.copy()
        env.update({"DISPLAY": display, "XAUTHORITY": xauthority})
        screen_size = str(self.controller.config.get("screen_size", "1920x1080"))
        command = [
            "/usr/bin/ffmpeg", "-loglevel", "error", "-f", "x11grab", "-draw_mouse", "0", "-framerate", "4",
            "-video_size", screen_size, "-i", display, "-vf", "scale=960:-2", "-q:v", "7", "-f", "mpjpeg", "-boundary_tag", "frame", "pipe:1",
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_json()
            action = str(body.get("action", ""))
            if action != "emergency_stop" and not self._authorized():
                self._json({"error": "控制令牌无效，请重新解锁"}, HTTPStatus.UNAUTHORIZED)
                return
            message = self.controller.action(action, body)
            self._json({"ok": True, "message": message}, HTTPStatus.ACCEPTED)
        except ControllerError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            self.controller.log("ERROR", "http", str(exc))
            self._json({"error": "服务器内部错误"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def build_server(host: str, port: int, simulate: bool = False) -> ThreadingHTTPServer:
    controller = BigCarController(ROOT, simulate=simulate)
    static_root = ROOT / "dist"
    if not static_root.exists():
        raise RuntimeError(f"前端构建不存在：{static_root}，请先运行 npm run build")
    handler = type("BoundConsoleHandler", (ConsoleHandler,), {"controller": controller, "static_root": static_root})
    return ThreadingHTTPServer((host, port), handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Autoware.AI big car web console")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    server = build_server(args.host, args.port, simulate=args.simulate)
    print(f"bigcar-console {VERSION} listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
