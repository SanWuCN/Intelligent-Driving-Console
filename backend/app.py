#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Set, Tuple
from urllib.parse import parse_qs, urlparse

from controller import VERSION, BigCarController, ControllerError, decode_bridge_frame


ROOT = Path(__file__).resolve().parents[1]
# WS opcodes
WS_CONTINUATION, WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 0, 1, 2, 8, 9, 10
# Live message types sent to the browser: [type u8][payload]
LIVE_JSON, LIVE_MAP, LIVE_CLOUD = 1, 2, 3


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
            if parsed.path == "/api/live":
                self._live_stream(parse_qs(parsed.query).get("ticket", [""])[0])
                return
            if parsed.path == "/api/rviz.mjpeg":
                self._rviz_stream()
                return
            if parsed.path == "/api/screen":
                ticket = parse_qs(parsed.query).get("ticket", [""])[0]
                target = self.controller.consume_screen_ticket(ticket)
                if target is None:
                    self.send_error(HTTPStatus.FORBIDDEN, "Screen ticket expired")
                    return
                self._screen_proxy(*target)
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

    # ------------------------------------------------------------- live stream
    def _live_stream(self, ticket: str) -> None:
        """Push the live scene to one browser over WebSocket.

        Binary frames are ``[type u8][payload]``: JSON for pose/battery/route
        bookkeeping, raw float32 xyz for the map and the lidar cloud.
        """
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(HTTPStatus.BAD_REQUEST, "WebSocket upgrade required")
            return
        if not self.controller.consume_live_ticket(ticket):
            self.send_error(HTTPStatus.FORBIDDEN, "Live ticket expired")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing Sec-WebSocket-Key")
            return
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        send_lock = threading.Lock()
        stopped = threading.Event()
        cloud_wanted = [True]
        map_name = {"value": str(self.controller.config.get("selected_map", ""))}

        def ws_send(payload: bytes, opcode: int = WS_BINARY) -> None:
            with send_lock:
                self._send_ws_frame(self.connection, payload, opcode=opcode)

        def send_json(data: Dict[str, Any]) -> None:
            ws_send(bytes([LIVE_JSON]) + json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        def send_cloud(kind: int, name: str, frame: Dict[str, Any]) -> None:
            header = dict(frame["header"])
            header["name"] = name
            blob = frame["blob"]
            encoded = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ws_send(bytes([kind]) + struct.pack("<I", len(encoded)) + encoded + blob)

        def deliver(frame: Dict[str, Any]) -> None:
            name = frame["type"]
            if name == "cloud" and cloud_wanted[0]:
                send_cloud(LIVE_CLOUD, "points_raw", frame)
            elif name == "map":
                send_cloud(LIVE_MAP, frame["header"].get("name", map_name["value"]), frame)
            elif name == "pose":
                send_json({"type": "pose", **frame["data"]})
            elif name == "battery":
                send_json({"type": "battery", **frame["data"]})
            elif name == "waypoints":
                send_json({"type": "waypoints", **frame["data"]})
            elif name == "trace":
                send_json({"type": "trace", **frame["data"]})
            elif name == "status":
                send_json({"type": "bridge", **frame["data"]})
            elif name == "error":
                send_json({"type": "bridge_error", **frame["data"]})

        token = self.controller.live.subscribe(deliver)
        try:
            state = self.controller.snapshot()
            send_json({
                "type": "welcome",
                "version": state["version"],
                "simulated": state["simulated"],
                "current_stage": state["current_stage"],
                "selected_map": state["selected_map"],
                "selected_route": state["selected_route"],
                "battery": state["battery"],
                "live": state["live"],
                "map": map_name["value"],
                "localized": "/current_pose" in state["live_topics"],
                "timestamp": state["timestamp"],
            })
            try:
                pending = self.controller.route_data(state["selected_route"]) if state["selected_route"] else None
            except ControllerError:
                pending = None
            if pending:
                send_json({"type": "route", "name": pending["name"],
                           "length_m": pending["length_m"], "count": len(pending["points"]),
                           "points": [value for point in pending["points"] for value in (point["x"], point["y"])]})
            # 先登记订阅类型，再要地图：否则地图帧可能比 wants 先到而被丢掉。
            self.controller.live.set_wants(token, ["cloud", "pose", "battery", "waypoints", "trace", "map"])
            if state["selected_map"]:
                self.controller.live.request_map(state["selected_map"], fresh=True)

            while not stopped.is_set():
                opcode, payload = self._read_ws_frame()
                if opcode == WS_CLOSE:
                    break
                if opcode == WS_PING:
                    ws_send(payload, opcode=WS_PONG)
                    continue
                if opcode not in (WS_TEXT, WS_CONTINUATION):
                    continue
                try:
                    command = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(command, dict):
                    continue
                action = command.get("cmd")
                if action == "map":
                    map_name["value"] = str(command.get("map", ""))
                    self.controller.live.request_map(map_name["value"])
                elif action == "cloud":
                    cloud_wanted[0] = bool(command.get("enabled", True))
                    self.controller.live.set_wants(token, ["cloud", "pose", "battery", "waypoints", "trace", "map"])
                elif action == "ping":
                    send_json({"type": "pong", "time": time.time()})
        except (OSError, ConnectionError, ValueError):
            pass
        except Exception as exc:  # noqa: BLE001 - 实时流不能静默死掉
            self.controller.log("ERROR", "live", f"实时推流异常：{exc!r}")
        finally:
            stopped.set()
            self.controller.live.unsubscribe(token)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

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

    @staticmethod
    def _recv_exact(stream: socket.socket, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = stream.recv(remaining)
            if not chunk:
                raise ConnectionError("connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _send_ws_frame(stream: socket.socket, payload: bytes, opcode: int = 2) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length <= 65535:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        stream.sendall(bytes(header) + payload)

    def _read_ws_frame(self) -> Tuple[int, bytes]:
        first, second = self._recv_exact(self.connection, 2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(self.connection, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(self.connection, 8))[0]
        mask = self._recv_exact(self.connection, 4) if masked else b""
        payload = self._recv_exact(self.connection, length) if length else b""
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload

    def _screen_proxy(self, host: str, port: int) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self.send_error(HTTPStatus.BAD_REQUEST, "WebSocket upgrade required")
            return
        try:
            vnc = socket.create_connection((host, port), timeout=5)
            vnc.settimeout(None)
        except OSError as exc:
            self.controller.log("ERROR", "screen", f"VNC 连接失败：{exc}")
            self.send_error(HTTPStatus.BAD_GATEWAY, "VNC service unavailable")
            return
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        send_lock = threading.Lock()
        stopped = threading.Event()

        def vnc_to_browser() -> None:
            try:
                while not stopped.is_set():
                    payload = vnc.recv(65536)
                    if not payload:
                        break
                    with send_lock:
                        self._send_ws_frame(self.connection, payload)
            except (OSError, ConnectionError):
                pass
            finally:
                stopped.set()
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=vnc_to_browser, name="vnc-to-browser", daemon=True).start()
        try:
            while not stopped.is_set():
                opcode, payload = self._read_ws_frame()
                if opcode == 8:
                    break
                if opcode == 9:
                    with send_lock:
                        self._send_ws_frame(self.connection, payload, opcode=10)
                elif opcode in (0, 1, 2):
                    vnc.sendall(payload)
        except (OSError, ConnectionError):
            pass
        finally:
            stopped.set()
            try:
                vnc.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            vnc.close()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/live-ticket":
            # 实时画面是只读遥测（地图/雷达/位姿/电量），不要求解锁控制：
            # 打开页面就能看到车况，危险动作仍然由 /api/action 的令牌把守。
            self._read_json()
            self._json(self.controller.issue_live_ticket())
            return
        if parsed.path == "/api/screen-ticket":
            if not self._authorized():
                self._json({"error": "控制令牌无效，无法连接车载屏幕"}, HTTPStatus.UNAUTHORIZED)
                return
            self._read_json()
            self._json(self.controller.issue_screen_ticket())
            return
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
    server = ThreadingHTTPServer((host, port), handler)
    server.controller = controller  # type: ignore[attr-defined]
    return server


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
        # 别把 ros_bridge 留在容器里占着 /points_raw。
        controller = getattr(server, "controller", None)
        if controller is not None:
            controller.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
