from __future__ import annotations

import ast
import csv
import json
import math
import os
import secrets
import shlex
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import psutil
except ImportError:  # pragma: no cover - Jetson image normally provides psutil
    psutil = None


VERSION = "1.3.0"
ROS_SETUP = "source /opt/ros/melodic/setup.bash; source /root/autoware_1.14.0/install/setup.bash"

DEFAULT_PARAMETERS = {
    # 循迹速度（m/s）。前端「设置速度」滑杆即时下发，范围见 SPEED_MIN_MPS/SPEED_MAX_MPS。
    "speed_limit_mps": 0.2,
    # 下面两项保留在后端但不再暴露到界面：前视距离固定 2.0 m、
    # 障碍停车预留距离固定 0.05 m（激光雷达避障本身已由
    # LIDAR_OBSTACLE_AVOIDANCE_ENABLED 关闭，见 避障关闭记录.md）。
    "lookahead_distance_m": 2.0,
    "obstacle_stop_distance_m": 0.05,
    "auto_loop": True,
}

SPEED_MIN_MPS = 0.2
SPEED_MAX_MPS = 2.0

# 激光雷达避障（velocity_set 点云停车/减速）总开关。
# False：检测半径归零 + 点数阈值取 int32 上限，点云永远达不到判定条件，
#        velocity_set 只做航点速度整形，不再因为点云把目标速度压到 0。
# True ：恢复 Autoware.AI 原厂阈值（检测半径 1.3 m、点数阈值 10）。
LIDAR_OBSTACLE_AVOIDANCE_ENABLED = False
LIDAR_DETECTION_RANGE_M = 1.3 if LIDAR_OBSTACLE_AVOIDANCE_ENABLED else 0.0
LIDAR_POINTS_THRESHOLD = 10 if LIDAR_OBSTACLE_AVOIDANCE_ENABLED else 2000000000


class ControllerError(RuntimeError):
    pass


BRIDGE_MAGIC = b"BC"
BRIDGE_HELLO, BRIDGE_STATUS, BRIDGE_BATTERY, BRIDGE_POSE = 1, 2, 3, 4
BRIDGE_WAYPOINTS, BRIDGE_TRACE, BRIDGE_CLOUD, BRIDGE_MAP, BRIDGE_ERROR = 5, 6, 7, 8, 9
BRIDGE_TYPES = {
    BRIDGE_HELLO: "hello", BRIDGE_STATUS: "status", BRIDGE_BATTERY: "battery",
    BRIDGE_POSE: "pose", BRIDGE_WAYPOINTS: "waypoints", BRIDGE_TRACE: "trace",
    BRIDGE_CLOUD: "cloud", BRIDGE_MAP: "map", BRIDGE_ERROR: "error",
}
# 注意：`struct` 的默认「原生对齐」会把 <BI 补成 7 字节，必须用标准尺寸 <B I
# 才能和 ros_bridge.py 写出的 5 字节帧头（magic 之后）对齐。
BRIDGE_HEADER = struct.Struct("<B I")
BRIDGE_CLOUD_HEADER = struct.Struct("<I")


def decode_bridge_frame(kind: int, payload: bytes) -> Dict[str, Any]:
    """Turn one bridge frame into a JSON-ready dict.

    Cloud frames carry ``u32 header length | json header | float32 xyz``; the
    float blob stays bytes so it can go straight to a WebSocket binary frame.
    """
    name = BRIDGE_TYPES.get(kind)
    if name is None:
        raise ControllerError(f"未知的桥接帧类型：{kind}")
    if name in ("cloud", "map"):
        if len(payload) < BRIDGE_CLOUD_HEADER.size:
            raise ControllerError("点云帧过短")
        (size,) = BRIDGE_CLOUD_HEADER.unpack_from(payload, 0)
        start = BRIDGE_CLOUD_HEADER.size
        header = json.loads(payload[start:start + size].decode("utf-8"))
        blob = payload[start + size:]
        header["points"] = len(blob) // 12
        return {"type": name, "header": header, "blob": blob}
    return {"type": name, "data": json.loads(payload.decode("utf-8"))}


class LiveBridge:
    """Owns the long-lived ``ros_bridge.py`` process and fans its frames out.

    The bridge is spawned lazily, only while at least one browser is watching,
    and its stdout is parsed on a daemon thread.  The newest frame of every type
    is cached, so ``/api/state`` can read battery and pose without shelling out
    to ``docker exec`` again.
    """

    def __init__(self, controller: "BigCarController"):
        self.controller = controller
        self.process: Optional[subprocess.Popen] = None
        self.lock = threading.RLock()
        self.cache: Dict[str, Any] = {}
        self.clients: Dict[str, Dict[str, Any]] = {}
        self.ready = False
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.frames = 0
        self.last_frame_at: Optional[float] = None
        self._last_map: Optional[str] = None
        self._map_token: str = uuid.uuid4().hex
        self._stop = threading.Event()

    # ------------------------------------------------------------- process I/O
    def command(self, payload: Dict[str, Any]) -> bool:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            return False
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _bridge_command(self) -> List[str]:
        override = os.environ.get("BIGCAR_BRIDGE_COMMAND")
        if override:
            return shlex.split(override)
        script = str(self.controller.config.get("bridge_script", "/from_host/bigcar-console/backend/ros_bridge.py"))
        # 容器默认的 `python` 是 Python 3 且没 source ROS，必须走 bash -lc + python2，
        # 否则一启动就是 "No module named rospy"。
        inner = f"exec python2 {shlex.quote(script)}"
        if os.environ.get("BIGCAR_BRIDGE_MOCK"):
            inner = f"exec python2 {shlex.quote(script)} --mock"
        return ["docker", "exec", "-i", self.controller.container, "bash", "-lc", f"{ROS_SETUP}; {inner}"]

    def ensure(self) -> bool:
        """Start the bridge process if it is not running. Never raises."""
        if self.controller.simulate:
            self.last_error = "演示模式没有实时数据"
            return False
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return True
            try:
                self.process = subprocess.Popen(
                    self._bridge_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0,
                )
            except OSError as exc:
                self.process = None
                self.last_error = f"实时桥接启动失败：{exc}"
                self.controller.log("ERROR", "live", self.last_error)
                return False
            self.ready = False
            self.started_at = time.time()
            self.last_error = None
            self.controller.log("INFO", "live", "实时数据桥接已启动（ros_bridge）")
            threading.Thread(target=self._read_frames, name="live-frames", daemon=True).start()
            threading.Thread(target=self._read_errors, args=(self.process,), name="live-errors", daemon=True).start()
            threading.Thread(target=self._watch, name="live-watch", daemon=True).start()
            return True

    def _read_errors(self, process) -> None:
        if process.stderr is None:
            return
        with (self.controller.logs_dir / "ros_bridge.log").open("ab") as handle:
            for line in iter(process.stderr.readline, b""):
                handle.write(line)
                handle.flush()

    def _watch(self) -> None:
        process = self.process
        if process is None:
            return
        code = process.wait()
        self.controller.log("WARN", "live", f"实时数据桥接已退出（code={code}）")
        with self.lock:
            if self.process is process:
                self.process = None
                self.ready = False
        self._stop.set()
        time.sleep(1.0)
        self._stop.clear()
        if self.clients:
            threading.Timer(1.0, self.ensure).start()

    def _read_frames(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        buffer = b""
        header_size = BRIDGE_HEADER.size          # 不含 2 字节 magic
        frame_size = 2 + header_size
        while True:
            try:
                chunk = stream.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buffer += chunk
            while True:
                if len(buffer) < frame_size:
                    break
                if buffer[:2] != BRIDGE_MAGIC:
                    # 丢掉噪声，重新对齐到下一个帧头。
                    index = buffer.find(BRIDGE_MAGIC)
                    buffer = buffer[index:] if index >= 0 else b""
                    if len(buffer) < frame_size:
                        break
                kind, size = BRIDGE_HEADER.unpack_from(buffer, 2)
                if len(buffer) < frame_size + size:
                    break
                payload = buffer[frame_size:frame_size + size]
                buffer = buffer[frame_size + size:]
                try:
                    frame = decode_bridge_frame(kind, payload)
                except (ControllerError, ValueError, UnicodeDecodeError) as exc:
                    self.last_error = f"实时帧解析失败：{exc}"
                    continue
                self._publish(frame)

    def _publish(self, frame: Dict[str, Any]) -> None:
        name = frame["type"]
        self.frames += 1
        self.last_frame_at = time.time()
        with self.lock:
            self.cache[name] = frame
            if name == "hello":
                self.ready = True
            elif name == "error":
                self.last_error = frame["data"].get("message") or "实时桥接报错"
            clients = list(self.clients.values())
        for client in clients:
            if name in client["wants"]:
                self._offer(client, frame)

    # --------------------------------------------------------------- clients
    def subscribe(self, subscriber) -> str:
        """Register a watching browser and trigger the bridge if needed."""
        token = uuid.uuid4().hex
        with self.lock:
            self.clients[token] = {
                "subscriber": subscriber, "wants": set(), "queue": deque(maxlen=6),
                "wake": threading.Event(), "sent": 0,
            }
        threading.Thread(target=self._write_client, args=(token,), name=f"live-send-{token[:6]}", daemon=True).start()
        if not self.ensure():
            self.push(token, {"type": "error", "data": {
                "scope": "bridge", "message": self.last_error or "实时数据不可用", "time": time.time()}})
            return token
        self.command({"cmd": "start", "cloud_hz": 5, "pose_hz": 10})
        return token

    def unsubscribe(self, token: str) -> None:
        if not token:
            return
        with self.lock:
            self.clients.pop(token, None)
            remaining = len(self.clients)
        if remaining == 0 and self.process is not None:
            self.command({"cmd": "stop"})

    def set_wants(self, token: str, wants: Iterable[str]) -> None:
        if not token:
            return
        with self.lock:
            client = self.clients.get(token)
            if client is not None:
                client["wants"] = set(wants)
        self.command({"cmd": "start", "cloud_hz": 5, "pose_hz": 10})

    def request_map(self, name: str, fresh: bool = False) -> None:
        """Ask the bridge for a map. ``fresh`` 让桥接无条件重发（新开页面/重连）。"""
        if not name:
            return
        self._last_map = name
        if fresh:
            self._map_token = uuid.uuid4().hex
        self.command({"cmd": "map", "map": name, "token": self._map_token})

    def push(self, token: str, frame: Dict[str, Any]) -> None:
        with self.lock:
            client = self.clients.get(token)
        if client is not None:
            self._offer(client, frame)

    def broadcast(self, frame: Dict[str, Any]) -> None:
        with self.lock:
            clients = list(self.clients.values())
        for client in clients:
            self._offer(client, frame)

    @staticmethod
    def _offer(client: Dict[str, Any], frame: Dict[str, Any]) -> None:
        queue = client["queue"]
        try:
            queue.append(frame)          # drop the oldest when a client lags
        except Exception:
            pass
        wake = client.get("wake")
        if wake is not None:
            wake.set()

    def _write_client(self, token: str) -> None:
        with self.lock:
            client = self.clients.get(token)
        if client is None:
            return
        client.setdefault("wake", threading.Event())
        while True:
            client["wake"].wait(1.0)
            client["wake"].clear()
            while True:
                try:
                    frame = client["queue"].popleft()
                except IndexError:
                    break
                with self.lock:
                    alive = token in self.clients
                if not alive:
                    return
                try:
                    client["subscriber"](frame)
                except Exception as exc:
                    self.controller.log("ERROR", "live", f"实时推送失败：{exc!r}")
                    with self.lock:
                        self.clients.pop(token, None)
                    return
                with self.lock:
                    client["sent"] = client.get("sent", 0) + 1

    # ------------------------------------------------------------ read access
    def snapshot(self, name: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.cache.get(name)

    def latest_battery(self) -> Optional[Dict[str, Any]]:
        frame = self.snapshot("battery")
        return dict(frame["data"]) if frame else None

    def latest_pose(self) -> Optional[Dict[str, Any]]:
        frame = self.snapshot("pose")
        return dict(frame["data"]) if frame else None

    def status(self) -> Dict[str, Any]:
        with self.lock:
            clients = len(self.clients)
            running = self.process is not None and self.process.poll() is None
        return {
            "running": running, "ready": self.ready, "clients": clients,
            "frames": self.frames, "last_frame_at": self.last_frame_at,
            "error": self.last_error, "map": self._last_map,
        }

    def close(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()


class BigCarController:
    def __init__(self, root: Path, simulate: bool = False):
        self.root = root.resolve()
        self.runtime = self.root / "runtime"
        self.logs_dir = self.runtime / "logs"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.simulate = simulate
        self.config_path = self.runtime / "config.json"
        self.config = self._load_config()
        self.container = str(self.config.get("container", "autoware_ai_orin"))
        self.data_dir = Path(str(self.config.get("data_dir", "/home/nvidia/Desktop"))).resolve()
        self._logs: deque[Dict[str, str]] = deque(maxlen=300)
        self._lock = threading.RLock()
        self._busy: Optional[str] = None
        self._last_error: Optional[str] = None
        self._emergency = False
        self._sim_stage = 1
        self._ros_cache: Tuple[float, Set[str], Set[str]] = (0.0, set(), set())
        self._live_cache: Tuple[float, Set[str]] = (0.0, set())
        self._screen_tickets: Dict[str, float] = {}
        self._live_tickets: Dict[str, float] = {}
        self._applied_speed_cache: Optional[Tuple[float, float]] = None
        self._battery_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        self._probe_lock = threading.RLock()
        self._speed_pending: Optional[Dict[str, Any]] = None
        self._speed_wake = threading.Event()
        self._closing = threading.Event()
        threading.Thread(target=self._speed_worker, name="speed-dispatch", daemon=True).start()
        self.live = LiveBridge(self)
        self.log("INFO", "system", "智能驾驶控制台后端已启动")
        if self.simulate:
            self.log("WARN", "system", "当前为演示模式，不会执行车端命令")

    def _load_config(self) -> Dict[str, Any]:
        if self.config_path.exists():
            with self.config_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        config = {
            "container": "autoware_ai_orin",
            "data_dir": "/home/nvidia/Desktop",
            "control_token": "801801801",
            "listen": "0.0.0.0",
            "port": 8765,
            "display": ":0",
            "xauthority": "/run/user/1000/gdm/Xauthority",
            "screen_size": "1920x1080",
            "vnc_host": "127.0.0.1",
            "vnc_port": 5900,
            "vnc_password": "",
            "selected_map": "",
            "selected_route": "",
            "parameters": DEFAULT_PARAMETERS.copy(),
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass
        return config

    def _save_config(self) -> None:
        temporary = self.config_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass

    def parameters(self) -> Dict[str, Any]:
        configured = self.config.get("parameters", {})
        return {**DEFAULT_PARAMETERS, **configured} if isinstance(configured, dict) else DEFAULT_PARAMETERS.copy()

    def issue_screen_ticket(self) -> Dict[str, Any]:
        ticket = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._screen_tickets = {key: expiry for key, expiry in self._screen_tickets.items() if expiry > now}
            self._screen_tickets[ticket] = now + 30.0
        return {"ticket": ticket, "password": str(self.config.get("vnc_password", ""))}

    def consume_screen_ticket(self, ticket: str) -> Optional[Tuple[str, int]]:
        now = time.monotonic()
        with self._lock:
            expiry = self._screen_tickets.pop(ticket, 0.0)
        if expiry <= now:
            return None
        return str(self.config.get("vnc_host", "127.0.0.1")), int(self.config.get("vnc_port", 5900))

    def issue_live_ticket(self) -> Dict[str, Any]:
        """一次性凭据：浏览器的 WebSocket 握手不能带自定义请求头。"""
        ticket = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._live_tickets = {key: expiry for key, expiry in self._live_tickets.items() if expiry > now}
            self._live_tickets[ticket] = now + 60.0
        return {"ticket": ticket, "ttl": 60}

    def consume_live_ticket(self, ticket: str) -> bool:
        now = time.monotonic()
        with self._lock:
            expiry = self._live_tickets.pop(ticket, 0.0)
        return expiry > now

    @property
    def control_token(self) -> str:
        return str(self.config.get("control_token", ""))

    def log(self, level: str, module: str, message: str) -> None:
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "module": module, "message": message}
        with self._lock:
            self._logs.append(entry)
        log_path = self.logs_dir / "console.log"
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:<5} {module:<14} {message}\n")
        except OSError:
            pass

    @staticmethod
    def _run(args: List[str], timeout: float = 8.0, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, env=env, check=False)
        except FileNotFoundError:
            # 例如开发机上没有 docker CLI：当作「命令不可用」而不是抛异常，
            # 否则 /api/live 会在 WebSocket 握手之后直接断开。
            return subprocess.CompletedProcess(args, 127, stdout=f"命令不存在：{args[0]}")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, stdout=f"命令超时：{' '.join(args[:3])}")

    def _docker(self, args: List[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
        return self._run(["docker", *args], timeout=timeout)

    def _ros(self, command: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
        return self._docker(["exec", self.container, "bash", "-lc", f"{ROS_SETUP}; {command}"], timeout=timeout)

    def _container_running(self) -> bool:
        if self.simulate:
            return True
        try:
            result = self._docker(["inspect", "-f", "{{.State.Running}}", self.container], timeout=4)
            return result.returncode == 0 and result.stdout.strip() == "true"
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _ros_snapshot(self, force: bool = False) -> Tuple[Set[str], Set[str]]:
        # Concurrent browser polls must not spawn competing short-lived ROS nodes.
        with self._probe_lock:
            return self._ros_snapshot_locked(force)

    def _ros_snapshot_locked(self, force: bool = False) -> Tuple[Set[str], Set[str]]:
        if self.simulate:
            stage_nodes = [
                ["/rosout"],
                ["/yhs_can_control_qt_node", "/hesai_lidar"],
                ["/base_link_to_localizer", "/world_to_map", "/robot_state_publisher", "/voxel_grid_filter", "/ring_ground_filter"],
                ["/points_map_loader", "/ndt_matching", "/pose_relay", "/vel_relay"],
                ["/waypoint_loader", "/lane_rule", "/lane_stop", "/lane_select", "/astar_avoid", "/velocity_set"],
                ["/pure_pursuit", "/twist_filter", "/twist_gate"],
            ]
            nodes = set(stage_nodes[0])
            for index in range(1, min(self._sim_stage, 6)):
                nodes.update(stage_nodes[index])
            topics = {"/rosout", "/rosout_agg"}
            if self._sim_stage >= 2:
                topics.update({"/points_raw", "/ctrl_fb", "/odo_fb"})
            if self._sim_stage >= 4:
                topics.update({"/points_map", "/ndt_pose", "/current_pose", "/current_velocity"})
            if self._sim_stage >= 5:
                topics.update({"/base_waypoints", "/final_waypoints"})
            if self._sim_stage >= 6:
                topics.update({"/ctrl_raw", "/ctrl_cmd"})
            return nodes, topics
        now = time.monotonic()
        cached_at, nodes, topics = self._ros_cache
        live_cached_at, _live = self._live_cache
        if not force and now - cached_at < 5.0 and now - live_cached_at < 5.0:
            return nodes, topics
        if not self._container_running():
            self._ros_cache = (now, set(), set())
            self._live_cache = (now, set())
            return set(), set()
        try:
            watched = ("/points_raw", "/points_no_ground", "/current_pose", "/final_waypoints", "/ctrl_cmd", "/ctrl_fb")
            probes = " ".join(shlex.quote(topic) for topic in watched)
            result = self._ros(f"timeout -k 1 10 python2 /from_host/bigcar-console/backend/ros_probe.py {probes}", timeout=13)
            if result.returncode != 0 or "__BATTERY__" not in result.stdout:
                raise OSError("ROS status probe incomplete")
            section = None
            nodes, topics, live = set(), set(), set()
            battery: Dict[str, Any] = {}
            for raw in result.stdout.splitlines():
                line = raw.strip()
                if line == "__NODES__":
                    section = "nodes"
                elif line == "__TOPICS__":
                    section = "topics"
                elif line == "__LIVE__":
                    section = "live"
                elif line == "__BATTERY__":
                    section = "battery"
                elif section == "nodes" and line.startswith("/"):
                    nodes.add(line)
                elif section == "topics" and line.startswith("/"):
                    topics.add(line)
                elif section == "live" and line in watched:
                    live.add(line)
                elif section == "battery" and " " in line:
                    name, _, value = line.partition(" ")
                    try:
                        battery[name] = ast.literal_eval(value)
                    except (ValueError, SyntaxError):
                        continue
            completed_at = time.monotonic()
            self._ros_cache = (completed_at, nodes, topics)
            self._live_cache = (completed_at, live)
            if battery:
                self._battery_cache = (completed_at, self._normalise_battery(battery))
            return nodes, topics
        except (OSError, subprocess.TimeoutExpired):
            completed_at = time.monotonic()
            if not force and completed_at - cached_at < 15.0:
                # Only display polls may retain a recent successful sample.
                # Forced checks before starting motion still fail closed.
                return nodes, topics
            self._ros_cache = (completed_at, set(), set())
            self._live_cache = (completed_at, set())
            return set(), set()

    @staticmethod
    def _contains(values: Iterable[str], *names: str) -> bool:
        return all(any(name in value for value in values) for name in names)

    def _live_topics(self, force: bool = False) -> Set[str]:
        if self.simulate:
            live: Set[str] = set()
            if self._sim_stage >= 2:
                live.update({"/points_raw", "/points_no_ground", "/ctrl_fb"})
            if self._sim_stage >= 4:
                live.add("/current_pose")
            if self._sim_stage >= 5:
                live.add("/final_waypoints")
            if self._sim_stage >= 6:
                live.add("/ctrl_cmd")
            return live
        now = time.monotonic()
        cached_at, live = self._live_cache
        if force or now - cached_at >= 5.0:
            self._ros_snapshot(force=force)
            _cached_at, live = self._live_cache
        return live

    def _stage(self, nodes: Set[str], live_topics: Set[str]) -> int:
        if self.simulate:
            return self._sim_stage
        if not self._container_running():
            return 0
        stage = 1
        if self._contains(nodes, "yhs_can_control_qt_node", "hesai_lidar") and {"/points_raw", "/ctrl_fb"}.issubset(live_topics):
            stage = 2
        else:
            return stage
        if self._contains(nodes, "base_link_to_localizer", "robot_state_publisher", "voxel_grid_filter", "ring_ground_filter"):
            stage = 3
        else:
            return stage
        if self._contains(nodes, "points_map_loader", "ndt_matching", "pose_relay", "vel_relay") and "/current_pose" in live_topics:
            stage = 4
        else:
            return stage
        if self._contains(nodes, "waypoint_loader", "lane_rule", "lane_stop", "lane_select", "astar_avoid", "velocity_set") and "/final_waypoints" in live_topics:
            stage = 5
        else:
            return stage
        if self._contains(nodes, "pure_pursuit", "twist_filter", "twist_gate") and "/ctrl_cmd" in live_topics:
            stage = 6
        return stage

    def list_files(self, suffix: str) -> List[Dict[str, Any]]:
        try:
            entries = []
            for path in self.data_dir.iterdir():
                if path.is_file() and path.suffix.lower() == suffix:
                    stat = path.stat()
                    entries.append({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
            return sorted(entries, key=lambda item: item["name"].lower())
        except OSError:
            return []

    def resolve_data_file(self, name: str, suffix: str) -> Path:
        if not name or Path(name).name != name or not name.lower().endswith(suffix):
            raise ControllerError(f"无效的 {suffix} 文件名")
        path = (self.data_dir / name).resolve()
        if path.parent != self.data_dir or not path.is_file():
            raise ControllerError(f"文件不存在：{name}")
        return path

    def _save_selection(self, data: Dict[str, Any]) -> None:
        selected_map = str(data.get("map", ""))
        selected_route = str(data.get("route", ""))
        if selected_map:
            self.resolve_data_file(selected_map, ".pcd")
        if selected_route:
            self.resolve_data_file(selected_route, ".csv")
        with self._lock:
            self.config["selected_map"] = selected_map
            self.config["selected_route"] = selected_route
            self._save_config()
        self.log("INFO", "files", f"已保存默认文件：{selected_map or '未选地图'} / {selected_route or '未选路径'}")

    @staticmethod
    def _bounded_float(data: Dict[str, Any], name: str, minimum: float, maximum: float) -> float:
        try:
            value = float(data[name])
        except (KeyError, TypeError, ValueError):
            raise ControllerError(f"参数 {name} 不是有效数值")
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ControllerError(f"参数 {name} 必须在 {minimum} 至 {maximum} 之间")
        return value

    def _publish_live_parameters(self, parameters: Dict[str, Any]) -> bool:
        """Push the live tuning config to Autoware. Returns True when it went out."""
        if self.simulate or not self._container_running():
            return False
        speed_mps = float(parameters["speed_limit_mps"])
        lookahead_m = float(parameters["lookahead_distance_m"])
        stop_distance_m = float(parameters["obstacle_stop_distance_m"])
        # replanner 的 velocity_max/min 单位是 km/h，follower 的 velocity 也是 km/h。
        velocity_kph = speed_mps * 3.6
        # velocity_min 只影响弯道下限，不能等于上限，否则低速段被抬高。
        velocity_min_kph = min(speed_mps, 0.3) * 3.6
        follower = (
            "{header: {stamp: now}, param_flag: 0, "
            f"velocity: {velocity_kph:.4f}, lookahead_distance: {lookahead_m:.4f}, "
            f"lookahead_ratio: 2.0, minimum_lookahead_distance: {lookahead_m:.4f}, "
            "displacement_threshold: 0.0, relative_angle_threshold: 0.0}"
        )
        replanner = (
            "{multi_lane_csv: '', replanning_mode: true, use_decision_maker: false, "
            f"velocity_max: {velocity_kph:.4f}, velocity_min: {velocity_min_kph:.4f}, "
            "accel_limit: 0.5, decel_limit: 0.3, radius_thresh: 20.0, radius_min: 6.0, "
            "resample_mode: false, resample_interval: 1.0, velocity_offset: 4, end_point_offset: 1, "
            "braking_distance: 5, replan_curve_mode: false, replan_endpoint_mode: false, "
            "overwrite_vmax_mode: false, realtime_tuning_mode: true}"
        )
        velocity_set = (
            "{header: {stamp: now}, "
            f"stop_distance_obstacle: {stop_distance_m:.4f}, stop_distance_stopline: 5.0, "
            f"detection_range: {LIDAR_DETECTION_RANGE_M:.1f}, threshold_points: {LIDAR_POINTS_THRESHOLD}, "
            "detection_height_top: 0.2, "
            "detection_height_bottom: -1.7, deceleration_obstacle: 0.8, "
            "deceleration_stopline: 0.6, velocity_change_limit: 9.972, "
            "deceleration_range: 0.0, temporal_waypoints_size: 100.0}"
        )
        commands = [
            ("/config/waypoint_follower", "autoware_config_msgs/ConfigWaypointFollower", follower),
            ("/config/waypoint_replanner", "autoware_config_msgs/ConfigWaypointReplanner", replanner),
            ("/config/velocity_set", "autoware_config_msgs/ConfigVelocitySet", velocity_set),
        ]
        for topic, message_type, payload in commands:
            # rostopic --once keeps its latched publisher alive for delivery.
            # Bound the process inside Docker too, so a host timeout cannot orphan it.
            try:
                result = self._ros(
                    f"timeout -k 2 12 rostopic pub -1 {topic} {message_type} {shlex.quote(payload)}",
                    timeout=17,
                )
            except subprocess.TimeoutExpired:
                raise ControllerError(f"实时参数下发超时（{topic}），请检查 ROS 节点负载") from None
            if result.returncode in (124, 137):
                raise ControllerError(f"实时参数下发超时（{topic}），请检查 ROS 节点负载")
            if result.returncode != 0:
                raise ControllerError(f"实时参数下发失败（{topic}）：{result.stdout.strip()}")
        self._applied_speed_cache = (time.monotonic(), velocity_kph)
        return True

    def _coerce_parameters(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Merge an incoming parameter payload onto the stored values.

        Only ``speed_limit_mps`` and ``auto_loop`` are exposed to the UI, so only
        those are accepted from a request: 前视距离 / 障碍停车 are backend-fixed
        values now.  A payload without them keeps the stored value.
        """
        current = self.parameters()
        merged = {
            "speed_limit_mps": current["speed_limit_mps"],
            "lookahead_distance_m": float(current["lookahead_distance_m"]),
            "obstacle_stop_distance_m": float(current["obstacle_stop_distance_m"]),
            "auto_loop": bool(current.get("auto_loop", True)),
        }
        if "speed_limit_mps" in data:
            merged["speed_limit_mps"] = self._bounded_float(data, "speed_limit_mps", SPEED_MIN_MPS, SPEED_MAX_MPS)
        if "auto_loop" in data:
            merged["auto_loop"] = bool(data["auto_loop"])
        return merged

    def _update_parameters(self, data: Dict[str, Any]) -> None:
        parameters = self._coerce_parameters(data)
        self.apply_speed(parameters["speed_limit_mps"], parameters=parameters, action="update_parameters")

    def apply_speed(self, speed_mps: float, parameters: Optional[Dict[str, Any]] = None,
                    action: str = "set_speed") -> Dict[str, Any]:
        """同时保存并即时下发循迹速度。

        写三处，缺一处都会出现「界面改了但车没变」：
        1. `config.json`：下一次启动、下一次循环路线生成都用它；
        2. `/config/waypoint_replanner`：velocity_max（km/h）+ realtime_tuning_mode，
           replanner 会立刻按新上限重发 `/final_waypoints`；
        3. `/config/waypoint_follower`：pure_pursuit 的速度与航点速度的一致性。
        """
        value = float(speed_mps)
        if not math.isfinite(value) or not SPEED_MIN_MPS - 1e-9 <= value <= SPEED_MAX_MPS + 1e-9:
            raise ControllerError(f"速度必须在 {SPEED_MIN_MPS} 至 {SPEED_MAX_MPS} m/s 之间")
        merged = dict(parameters or self.parameters())
        merged["speed_limit_mps"] = value
        with self._lock:
            self.config["parameters"] = merged
            self._save_config()
        try:
            self._refresh_loop_route(value)
        except (ControllerError, OSError) as exc:
            self.log("WARN", "loop", f"循环路径速度同步失败：{exc}")
        # 下发放到后台线程：拖滑杆时前端每次都秒回，中间值被合并成最后一次。
        queued = self.simulate or self._container_running()
        if queued:
            with self._lock:
                self._speed_pending = merged
            self._speed_wake.set()
        self.log(
            "INFO",
            "tuning",
            f"循迹速度已设为 {value:.2f} m/s（{value * 3.6:.2f} km/h）"
            + ("，正在下发到车端" if queued else "；车端未运行，已保存待下发"),
        )
        return {"speed_limit_mps": value, "published": queued, "action": action}

    def _speed_worker(self) -> None:
        """把连续的速度调整合并成一次下发。"""
        while not self._closing.is_set():
            self._speed_wake.wait(1.0)
            self._speed_wake.clear()
            if self._closing.is_set():
                return
            # 合并窗口：连着拖滑杆时等最后一次，避免一次拖动下发十几遍。
            time.sleep(0.25)
            with self._lock:
                pending = self._speed_pending
                self._speed_pending = None
            if not pending:
                continue
            try:
                self._publish_live_parameters(pending)
                self.log("INFO", "tuning", f"循迹速度 {pending['speed_limit_mps']:.2f} m/s 已下发到车端")
            except Exception as exc:  # 车端没起来时不能让线程死掉
                self.log("ERROR", "tuning", f"速度下发失败：{exc}")

    def _speed_dispatch_state(self, stage: int, live_topics: Set[str]) -> Dict[str, Any]:
        """What the UI needs in order to explain whether a speed change bites now."""
        parameters = self.parameters()
        stored = float(parameters["speed_limit_mps"])
        try:
            ceiling = max((point["velocity"] for point in self.route_data(str(self.config.get("selected_route", "")))["points"]), default=0.0)
        except (ControllerError, ValueError):
            ceiling = 0.0
        # 只有 waypoint_replanner 在运行时 `/config/waypoint_replanner` 才有订阅者；
        # 此时改动立刻反映到 `/final_waypoints`，也就是「马上生效」。
        live = bool(self._container_running()) and stage >= 5
        return {
            "min_mps": SPEED_MIN_MPS,
            "max_mps": SPEED_MAX_MPS,
            "value_mps": round(stored, 3),
            "route_ceiling_mps": round(ceiling, 3),
            "route_limited": bool(ceiling) and stored > ceiling + 0.02,
            "live": live,
            "detail": "已下发到车端，改变立即生效" if live
            else "已保存；车端规划节点启动后（第 5 步）自动按此速度运行",
        }

    def _set_all_waypoint_velocities(self, source: Path, speed_mps: float) -> bool:
        """把循环路径文件里的航点速度改成新的目标速度（不重排航点）。"""
        try:
            with source.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
                rows = list(csv.reader(handle))
        except OSError:
            return False
        if not rows:
            return False
        velocity_kph = speed_mps * 3.6
        changed = False
        for row in rows:
            if len(row) < 5:
                continue
            try:
                float(row[0]), float(row[1])
            except (TypeError, ValueError):
                continue
            row[4] = f"{velocity_kph:.4f}"
            changed = True
        if not changed:
            return False
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerows(rows)
        return True

    def _refresh_loop_route(self, speed_mps: float) -> None:
        """速度变了就同步已经在跑的循环路径，避免下次加载又回到旧速度。

        `_prepare_loop_route` 已经把多圈路径展开好了，这里只改航点速度列，
        保持圈数、航点顺序和闭环结构完全不变。
        """
        if not bool(self.parameters().get("auto_loop", True)):
            return
        if not str(self.config.get("selected_route", "")):
            return
        loop_path = self.runtime / "loop_route.csv"
        if not loop_path.exists():
            return
        if self._set_all_waypoint_velocities(loop_path, speed_mps):
            self.log("INFO", "loop", f"循环路径航点速度已同步为 {speed_mps * 3.6:.2f} km/h")

    def _prepare_loop_route(self, source: Path, speed_mps: float, laps: int = 200) -> Path:
        with source.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            raise ControllerError("路径文件为空")
        numeric: List[Tuple[int, float, float]] = []
        for index, row in enumerate(rows):
            if len(row) < 2:
                continue
            try:
                numeric.append((index, float(row[0]), float(row[1])))
            except (TypeError, ValueError):
                continue
        if len(numeric) < 3:
            raise ControllerError("路径航点太少，无法循环")
        gap = math.hypot(numeric[-1][1] - numeric[0][1], numeric[-1][2] - numeric[0][2])
        if gap > 2.0:
            raise ControllerError(f"路径未闭环：终点距起点 {gap:.2f} m，为防止横穿地图已禁止自动循环")
        header_rows = rows[:numeric[0][0]]
        waypoint_rows = [list(rows[index]) for index, _x, _y in numeric]
        velocity_kph = speed_mps * 3.6
        for row in waypoint_rows:
            if len(row) >= 5:
                row[4] = f"{velocity_kph:.4f}"
        target = self.runtime / "loop_route.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            # Autoware's CSV parser does not strip CR from the last header field.
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerows(header_rows)
            for _ in range(laps):
                writer.writerows(waypoint_rows)
        self.log("INFO", "loop", f"已生成 {laps} 圈连续路径，闭环误差 {gap:.2f} m")
        return target

    def route_data(self, name: str) -> Dict[str, Any]:
        path = self.resolve_data_file(name, ".csv")
        points: List[Dict[str, float]] = []
        with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
            for row in csv.reader(handle):
                if len(row) < 2:
                    continue
                try:
                    values = [float(value.strip()) for value in row[:5]]
                except (ValueError, TypeError):
                    continue
                while len(values) < 5:
                    values.append(0.0)
                if not all(math.isfinite(value) for value in values):
                    continue
                points.append({"x": values[0], "y": values[1], "z": values[2], "yaw": values[3], "velocity": values[4]})
        if not points:
            raise ControllerError("路径文件没有可用航点")
        length = 0.0
        for first, second in zip(points, points[1:]):
            length += math.hypot(second["x"] - first["x"], second["y"] - first["y"])
        xs = [point["x"] for point in points]
        ys = [point["y"] for point in points]
        return {"name": name, "points": points, "length_m": length, "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}

    def _temperature(self) -> Optional[float]:
        values = []
        for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                value = float(path.read_text().strip()) / 1000.0
                if -20 < value < 150:
                    values.append(value)
            except (OSError, ValueError):
                continue
        return max(values) if values else None

    @staticmethod
    def _can_state() -> str:
        try:
            value = Path("/sys/class/net/can0/operstate").read_text().strip().upper()
            return value if value else "未知"
        except OSError:
            return "未检测"

    @staticmethod
    def _module(key: str, label: str, state: str, detail: str) -> Dict[str, str]:
        return {"key": key, "label": label, "state": state, "detail": detail}

    def snapshot(self) -> Dict[str, Any]:
        nodes, topics = self._ros_snapshot()
        live_topics = self._live_topics()
        stage = self._stage(nodes, live_topics)
        container_ok = self._container_running()
        map_ok = "/points_map" in topics or self._contains(nodes, "points_map_loader")
        pose_ok = "/current_pose" in live_topics
        route_ok = "/final_waypoints" in live_topics
        chassis_node = self._contains(nodes, "yhs_can_control_qt_node")
        chassis_feedback = "/ctrl_fb" in live_topics
        lidar_node = self._contains(nodes, "hesai_lidar")
        tf_ok = self._contains(nodes, "base_link_to_localizer", "robot_state_publisher")
        ndt_nodes = self._contains(nodes, "points_map_loader", "ndt_matching", "pose_relay", "vel_relay")
        planning_nodes = self._contains(nodes, "waypoint_loader", "lane_select")
        control_nodes = self._contains(nodes, "pure_pursuit", "twist_filter", "twist_gate")
        control_output = "/ctrl_cmd" in live_topics
        localization_bootstrap = self._contains(nodes, "localization_bootstrap")
        rviz_running = stage >= 3 and any("rviz" in node.lower() for node in nodes)
        modules = [
            self._module("docker", "Docker 容器", "ok" if container_ok else "error", f"{self.container} 运行中" if container_ok else "容器未运行"),
            self._module("chassis", "底盘（CAN）", "ok" if chassis_feedback else "error" if chassis_node else "idle", f"CAN0 {self._can_state()} · /ctrl_fb 实时" if chassis_feedback else "底盘无实时反馈，请检查进程和 CAN 通信" if chassis_node else "底盘节点未启动"),
            self._module("lidar", "Hesai 激光雷达", "ok" if "/points_raw" in live_topics else "warn" if lidar_node else "idle", "PandarXT-16 · /points_raw 实时" if "/points_raw" in live_topics else "驱动已启动，但 /points_raw 暂无实时数据" if lidar_node else "雷达节点未启动"),
            self._module("tf", "Autoware / TF", "ok" if tf_ok else "idle", "Runtime Manager · base_link → velodyne" if tf_ok else "Autoware 尚未启动"),
            self._module("ndt", "NDT 定位", "ok" if pose_ok else "warn" if (ndt_nodes or localization_bootstrap) and map_ok else "idle", "/current_pose 实时" if pose_ok else "地图和雷达已显示，请设置 2D Pose" if localization_bootstrap and map_ok else "定位计算中，等待 /current_pose" if ndt_nodes and map_ok else "定位节点未启动"),
            self._module(
                "planning",
                "路径规划/跟踪",
                "ok" if control_output and chassis_feedback else "warn" if control_output or control_nodes or route_ok else "idle",
                "/ctrl_cmd 与底盘反馈实时，车辆运动状态请以现场为准"
                if control_output and chassis_feedback
                else "/ctrl_cmd 有输出，但底盘无实时反馈"
                if control_output
                else "控制节点已启动，但 /ctrl_cmd 无实时数据"
                if control_nodes
                else "/final_waypoints 实时，等待启动循迹"
                if route_ok
                else "规划节点已启动，等待最终航点"
                if planning_nodes
                else "请先完成地图标定",
            ),
        ]
        titles = [
            ("环境检查", "系统环境、硬件连接、资源检查"),
            ("底盘与雷达", "CAN 通信、底盘状态、Hesai 雷达"),
            ("Autoware", "启动 TF、车辆模型与点云滤波"),
            ("地图与标定", "加载点云地图、NDT 定位初始化"),
            ("路径配置", "加载 CSV 航迹并启动规划节点"),
            ("循迹运行", "启动控制链路，进入循迹模式"),
        ]
        with self._lock:
            busy = self._busy
            logs = list(self._logs)
            last_error = self._last_error
            emergency = self._emergency
        workflow = []
        action_labels = ["重新检查环境", "启动底盘与雷达", "打开 Autoware 与终端", "打开 RViz 并开始标定", "加载路径与规划", "开始循迹"]
        for index, (title, description) in enumerate(titles, 1):
            if index <= stage:
                state, detail = "done", "状态检查通过"
            elif index == stage + 1:
                state, detail = ("running", f"正在执行：{busy}") if busy else ("current", "可以启动此步骤")
            else:
                state, detail = "waiting", "需先完成上一步"
            if emergency and index == 6:
                state, detail = "blocked", "已触发紧急停止，重新检查后方可运行"
            if index == 2 and lidar_node and "/points_raw" not in live_topics:
                state, detail = "current", "雷达驱动已启动，但 /points_raw 暂无实时点云"
            if index == 4 and ndt_nodes and not pose_ok:
                state, detail = "current", "请在 RViz 使用 2D Pose Estimate 设置车辆位置和朝向"
            if index == 4 and localization_bootstrap and not pose_ok:
                state, detail = "current", "地图与实时雷达已显示，请使用 2D Pose Estimate 完成人工标定"
            if index == 5 and planning_nodes and not route_ok:
                state, detail = "current", "规划节点已启动，但 /final_waypoints 暂无实时数据"
            if index == 6 and control_nodes and not control_output:
                state, detail = "current", "控制节点已启动，但 /ctrl_cmd 没有实时输出"
            if index == 2 and chassis_node and not chassis_feedback:
                state, detail = "current", "底盘无实时反馈，请检查进程和 CAN 通信"
            if index == 6 and control_output and not chassis_feedback:
                state, detail = "blocked", "底盘无实时反馈，不能判定为循迹运行"
            workflow.append({"id": index, "title": title, "description": description, "state": state, "detail": detail, "action_label": action_labels[index - 1]})
        cpu = psutil.cpu_percent(interval=None) if psutil else None
        memory = psutil.virtual_memory().percent if psutil else None
        return {
            "version": VERSION,
            "simulated": self.simulate,
            "connected": container_ok,
            "container": self.container,
            "current_stage": stage,
            "busy": busy,
            "emergency": emergency,
            "rviz_running": rviz_running,
            "live_topics": sorted(live_topics),
            "modules": modules,
            "workflow": workflow,
            "telemetry": {"cpu_percent": cpu, "memory_percent": memory, "temperature_c": self._temperature(), "can0": self._can_state(), "node_count": len(nodes), "topic_count": len(topics)},
            "battery": self.battery_state(),
            "live": self.live.status(),
            "speed": self._speed_dispatch_state(stage, live_topics),
            "maps": self.list_files(".pcd"),
            "routes": self.list_files(".csv"),
            "selected_map": str(self.config.get("selected_map", "")),
            "selected_route": str(self.config.get("selected_route", "")),
            "parameters": self.parameters(),
            "logs": logs,
            "timestamp": time.time(),
            "last_error": last_error,
        }

    @staticmethod
    def _normalise_battery(values: Dict[str, Any]) -> Dict[str, Any]:
        """把 BMS 原始字段整理成前端要的形状（含是否低压、是否充电）。"""
        soc = values.get("soc")
        return {
            "soc": None if soc is None else float(soc),
            "voltage": None if values.get("voltage") is None else float(values["voltage"]),
            "current": None if values.get("current") is None else float(values["current"]),
            "remaining_ah": None if values.get("remaining_ah") is None else float(values["remaining_ah"]),
            "temp_high": None if values.get("temp_high") is None else float(values["temp_high"]),
            "temp_low": None if values.get("temp_low") is None else float(values["temp_low"]),
            "low": bool(values.get("under") or values.get("single_uv")),
            "over": bool(values.get("over") or values.get("single_ov")),
            "charge": bool(values.get("charge")),
            "stamp": time.time(),
        }

    def battery_state(self) -> Optional[Dict[str, Any]]:
        """剩余电量：优先实时桥接，其次 5 秒一次的话题探针。"""
        battery = self.live.latest_battery()
        if battery is not None:
            chassis = self.live.snapshot("chassis")
            if chassis:
                battery.setdefault("speed", chassis["data"].get("speed"))
            battery["source"] = "live"
            return battery
        if self.simulate or not self._container_running():
            return None
        # 探针在 _ros_snapshot 里顺带读 BMS，这里直接复用它的结果（最长 60 秒）。
        self._ros_snapshot()
        cached = self._battery_cache
        if cached and time.monotonic() - cached[0] < 60.0:
            battery = dict(cached[1])
            battery["source"] = "probe"
            return battery
        return None

    def _start_process(self, name: str, command: str) -> None:
        pid_file = f"/from_host/bigcar-console/runtime/{name}.pid"
        log_file = f"/from_host/bigcar-console/runtime/logs/{name}.log"
        check = self._ros(f"p=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); test -n \"$p\" && kill -0 \"$p\" 2>/dev/null", timeout=4)
        if check.returncode == 0:
            self.log("INFO", name, "进程已在运行，跳过重复启动")
            return
        display = str(self.config.get("display", ":0"))
        xauth = str(self.config.get("xauthority", "/run/user/1000/gdm/Xauthority"))
        wrapped = f"export DISPLAY={shlex.quote(display)} XAUTHORITY={shlex.quote(xauth)}; echo $$ > {shlex.quote(pid_file)}; exec {command} >> {shlex.quote(log_file)} 2>&1"
        result = self._docker(["exec", "-d", self.container, "bash", "-lc", f"{ROS_SETUP}; {wrapped}"], timeout=7)
        if result.returncode != 0:
            raise ControllerError(f"{name} 启动失败：{result.stdout.strip()}")
        self.log("INFO", name, f"已提交启动：{command}")
        time.sleep(0.35)

    def _topic_has_message(self, topic: str) -> bool:
        if self.simulate:
            return True
        try:
            result = self._ros(
                f"timeout -k 1 10 python2 /from_host/bigcar-console/backend/ros_probe.py {shlex.quote(topic)}",
                timeout=13,
            )
            if result.returncode != 0 or "__LIVE__" not in result.stdout:
                return False
            live_section = result.stdout.split("__LIVE__", 1)[1].split("__BATTERY__", 1)[0]
            return topic in live_section.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _open_autoware_terminal(self) -> None:
        log_path = "/home/nvidia/Desktop/bigcar-console/runtime/logs/autoware_ui.log"
        check = self._run(["pgrep", "-u", "nvidia", "-f", f"tail -n 120 -F {log_path}"], timeout=3)
        if check.returncode == 0:
            self.log("INFO", "autoware_ui", "Autoware 终端已在桌面打开")
            return
        display = str(self.config.get("display", ":0"))
        xauth = str(self.config.get("xauthority", "/run/user/1000/gdm/Xauthority"))
        command = f"tail -n 120 -F {shlex.quote(log_path)}; exec bash"
        try:
            subprocess.Popen(
                [
                    "runuser", "-u", "nvidia", "--", "env", f"DISPLAY={display}", f"XAUTHORITY={xauth}",
                    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", "gnome-terminal", "--title=Autoware 控制终端", "--", "bash", "-lc", command,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.log("INFO", "autoware_ui", "已在车载桌面打开 Autoware 控制终端")
        except OSError as exc:
            self.log("WARN", "autoware_ui", f"终端窗口打开失败：{exc}")

    def _start_rviz(self) -> None:
        """Run RViz in the container mount/network namespaces with host IPC.

        Jetson's Qt/X11 stack can render the 3D view while dropping dock widgets
        when the container has a separate IPC namespace.  Sharing only IPC keeps
        the container filesystem and ROS environment while fixing Qt's X11 path.
        """
        pid_file = "/from_host/bigcar-console/runtime/rviz.pid"
        # Redirection is evaluated by the host shell before nsenter runs.
        log_file = str(self.logs_dir / "rviz.log")
        check = self._ros(f"p=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); test -n \"$p\" && kill -0 \"$p\" 2>/dev/null", timeout=4)
        if check.returncode == 0:
            self.log("INFO", "rviz", "进程已在运行，跳过重复启动")
            return
        display = str(self.config.get("display", ":0"))
        xauth = str(self.config.get("xauthority", "/run/user/1000/gdm/Xauthority"))
        command = (
            "container_pid=$(docker inspect -f '{{.State.Pid}}' " + shlex.quote(self.container) + "); "
            "test -n \"$container_pid\" -a \"$container_pid\" != 0 || exit 1; "
            "nohup nsenter --target \"$container_pid\" --mount --uts --net --pid bash -lc "
            + shlex.quote(
                f"source /opt/ros/melodic/setup.bash; source /root/autoware_1.14.0/install/setup.bash; "
                f"export DISPLAY={shlex.quote(display)} XAUTHORITY={shlex.quote(xauth)}; "
                f"echo $$ > {shlex.quote(pid_file)}; "
                "exec rosrun rviz rviz -d $(rospack find autoware_quickstart_examples)/launch/rosbag_demo/default.rviz"
            )
            + f" >> {shlex.quote(log_file)} 2>&1 </dev/null &"
        )
        result = self._run(["bash", "-lc", command], timeout=10)
        if result.returncode != 0:
            raise ControllerError(f"rviz 启动失败：{result.stdout.strip()}")
        self.log("INFO", "rviz", "已使用宿主机 IPC 启动 RViz，工具栏和面板可正常显示")
        time.sleep(0.5)

    def _launch_stage(self, action: str, data: Dict[str, Any]) -> None:
        if action == "environment_check":
            if not self._container_running():
                result = self._docker(["start", self.container], timeout=20)
                if result.returncode != 0:
                    raise ControllerError(f"容器启动失败：{result.stdout.strip()}")
            self.log("INFO", "environment", "环境检查完成，Autoware 容器可用")
            return
        if action == "start_hardware":
            self._start_process("chassis", "roslaunch yhs_can_control_qt yhs_can_control_qt.launch")
            self._start_process("lidar", "roslaunch hesai_lidar hesai_lidar.launch lidar_correction_file:=$(rospack find hesai_lidar)/config/PandarXT-16.csv")
            return
        if action == "start_autoware":
            if "/points_raw" not in self._live_topics(force=True):
                raise ControllerError("激光雷达没有实时点云，请先确认 /points_raw 后再启动 Autoware")
            self._start_process(
                "autoware_ui",
                "bash /from_host/bigcar-console/backend/run_managed_runtime_manager.sh",
            )
            self._open_autoware_terminal()
            self._start_process("setup_tf", "roslaunch runtime_manager setup_tf.launch x:=1.2 y:=0.0 z:=2.0 yaw:=0.0 pitch:=0.0 roll:=0.0 frame_id:=/base_link child_frame_id:=/velodyne")
            self._start_process("vehicle_model", "roslaunch vehicle_description vehicle_model.launch")
            self._start_process("world_to_map", "roslaunch autoware_quickstart_examples tf_local.launch")
            self._start_process("voxel_filter", "roslaunch points_downsampler points_downsample.launch node_name:=voxel_grid_filter points_topic:=/points_raw measurement_range:=200")
            self._start_process("ground_filter", "roslaunch points_preprocessor ring_ground_filter.launch point_topic:=/points_raw sensor_model:=16 sensor_height:=2.0")
            return
        if action == "start_localization":
            nodes, _ = self._ros_snapshot(force=True)
            if not self._contains(nodes, "base_link_to_localizer", "robot_state_publisher"):
                raise ControllerError("Autoware 基础节点尚未就绪，请先完成上一步")
            map_path = self.resolve_data_file(str(data.get("map", "")), ".pcd")
            self.config["selected_map"] = map_path.name
            self._save_config()
            container_path = f"/from_host/{map_path.name}"
            self._start_process("map_loader", f"roslaunch map_file points_map_loader.launch path_pcd:={shlex.quote(container_path)}")
            self._start_process("vel_pose", "roslaunch autoware_connector vel_pose_connect.launch topic_pose_stamped:=/ndt_pose topic_twist_stamped:=/estimate_twist")
            self._start_process("localization_bootstrap", "python2 /from_host/bigcar-console/backend/localization_bootstrap.py")
            self._start_rviz()
            self.log("WARN", "localization", "RViz 已打开：请使用 2D Pose Estimate 人工设置车辆位置和朝向")
            return
        if action == "load_route":
            if "/current_pose" not in self._live_topics(force=True):
                raise ControllerError("尚未完成 RViz 人工初始位姿标定，/current_pose 没有实时数据")
            route_path = self.resolve_data_file(str(data.get("route", "")), ".csv")
            self.route_data(route_path.name)
            parameters = self.parameters()
            speed_mps = float(parameters["speed_limit_mps"])
            if bool(parameters["auto_loop"]):
                loop_path = self._prepare_loop_route(route_path, speed_mps)
                container_path = f"/from_host/bigcar-console/runtime/{loop_path.name}"
            else:
                container_path = f"/from_host/{route_path.name}"
            self.config["selected_route"] = route_path.name
            self._save_config()
            self._start_process(
                "waypoint_loader",
                f"roslaunch waypoint_maker waypoint_loader.launch load_csv:=true "
                f"multi_lane_csv:={shlex.quote(container_path)} replanning_mode:=true "
                f"realtime_tuning_mode:=true velocity_max:={speed_mps * 3.6:.4f} "
                f"velocity_min:={min(speed_mps, 0.1) * 3.6:.4f} resample_mode:=false replan_endpoint_mode:=false",
            )
            self._start_process("lane_rule", "roslaunch lane_planner lane_rule_option.launch")
            self._start_process("lane_stop", "rosrun lane_planner lane_stop")
            self._start_process("lane_select", "roslaunch lane_planner lane_select.launch")
            # The route is expanded into repeated laps.  Astar's default 30-point
            # neighborhood can lock onto a different section at the lap seam;
            # search a wider local window while avoidance remains disabled.
            self._start_process("astar_avoid", "roslaunch waypoint_planner astar_avoid.launch robot_length:=2.0 robot_width:=1.0 robot_base2back:=0.5 minimum_turning_radius:=2.1 enable_avoidance:=false closest_search_size:=300")
            self._start_process(
                "velocity_set",
                "roslaunch waypoint_planner velocity_set.launch "
                "use_crosswalk_detection:=false enable_multiple_crosswalk_detection:=false "
                f"points_topic:=points_no_ground stop_distance_obstacle:={float(parameters['obstacle_stop_distance_m']):.4f} "
                f"detection_range:={LIDAR_DETECTION_RANGE_M:.1f} points_threshold:={LIDAR_POINTS_THRESHOLD}",
            )
            return
        if action == "start_tracking":
            if data.get("safety_confirmed") is not True:
                raise ControllerError("必须完成安全确认后才能启动循迹")
            current_live = self._live_topics(force=True)
            missing = [topic for topic in ("/points_raw", "/current_pose", "/final_waypoints", "/ctrl_fb") if topic not in current_live]
            if missing:
                reasons = {
                    "/points_raw": "激光雷达没有实时点云",
                    "/current_pose": "尚未在 RViz 完成人工初始位姿标定",
                    "/final_waypoints": "规划链路尚未生成最终航点",
                    "/ctrl_fb": "底盘没有实时反馈，请检查进程和 CAN 通信",
                }
                raise ControllerError("启动被阻止：" + "；".join(reasons[topic] for topic in missing))
            guarded = self.config.get("person_guard_enabled") is True
            if guarded:
                disarm = self._ros("rosservice call /person_guard/arm 'data: false'", timeout=5)
                if disarm.returncode != 0:
                    raise ControllerError("人体停车门控未运行，禁止启动循迹")
            tracking_launch = (
                "roslaunch /from_host/bigcar-console/backend/person_guard_tracking.launch"
                if guarded else "roslaunch twist_filter twist_filter.launch"
            )
            self._start_process("twist_filter", tracking_launch)
            # The YHS chassis consumes autoware_msgs/ControlCommandStamped on
            # /ctrl_cmd.  Pure Pursuit defaults to Twist-only output, which
            # leaves ctrl_raw/ctrl_cmd silent even though all control nodes are
            # running.  Enable steering-robot output to complete the chain:
            # pure_pursuit -> /ctrl_raw -> twist_filter -> /ctrl_cmd -> CAN.
            self._start_process(
                "pure_pursuit",
                "roslaunch pure_pursuit pure_pursuit.launch "
                "publishes_for_steering_robot:=true velocity_source:=0 "
                f"const_lookahead_distance:={float(self.parameters()['lookahead_distance_m']):.4f} "
                f"minimum_lookahead_distance:={float(self.parameters()['lookahead_distance_m']):.4f}",
            )
            self._publish_live_parameters(self.parameters())
            time.sleep(1.0)
            if not self._topic_has_message("/ctrl_cmd"):
                raise ControllerError("控制链路启动失败：/ctrl_cmd 没有实时数据，已禁止显示‘循迹运行中’")
            if guarded:
                armed = self._ros("rosservice call /person_guard/arm 'data: true'", timeout=5)
                if armed.returncode != 0 or "success: True" not in armed.stdout:
                    self._emergency_stop()
                    raise ControllerError("人体相机数据未就绪，已停止循迹启动")
            with self._lock:
                self._emergency = False
            self.log("WARN", "safety", "循迹控制已启动，请保持物理急停可用")
            return
        if action == "launch_rviz":
            self._start_rviz()
            return
        raise ControllerError(f"未知动作：{action}")

    def _emergency_stop(self) -> None:
        if not self.simulate and self._container_running():
            if self.config.get("person_guard_enabled") is True:
                self._ros("rosservice call /person_guard/arm 'data: false' 2>/dev/null || true", timeout=5)
            self._ros("rosnode kill /pure_pursuit /twist_filter /twist_gate 2>/dev/null || true", timeout=6)
            zero = "{header: {stamp: now}, cmd: {linear_velocity: 0.0, linear_acceleration: 0.0, steering_angle: 0.0}}"
            self._ros(f"timeout 2 rostopic pub -r 20 /ctrl_cmd autoware_msgs/ControlCommandStamped {shlex.quote(zero)} >/dev/null 2>&1 || true", timeout=5)
        if self.simulate:
            self._sim_stage = min(self._sim_stage, 5)
        with self._lock:
            self._emergency = True
        self._ros_cache = (0.0, set(), set())
        self._live_cache = (0.0, set())
        self.log("WARN", "safety", "紧急停止已执行：控制节点停止并下发零速指令")

    def _stop_all(self) -> None:
        self._emergency_stop()
        if not self.simulate and self._container_running():
            node_names = [
                "hesai_lidar", "yhs_can_control_qt_node", "base_link_to_localizer", "world_to_map", "joint_state_publisher", "robot_state_publisher", "localization_bootstrap",
                "voxel_grid_filter", "ring_ground_filter", "points_map_loader", "ndt_matching", "can_status_translator", "pose_relay", "vel_relay",
                "waypoint_loader", "waypoint_replanner", "waypoint_marker_publisher", "lane_rule", "lane_stop", "lane_select", "astar_avoid", "velocity_set",
            ]
            self._ros("rosnode kill " + " ".join(f"/{name}" for name in node_names) + " 2>/dev/null || true", timeout=12)
            self._ros(
                "for pid_file in /from_host/bigcar-console/runtime/*.pid; do "
                "[ -f \"$pid_file\" ] || continue; "
                "managed_pid=$(cat \"$pid_file\" 2>/dev/null || true); "
                "[ -n \"$managed_pid\" ] && kill -TERM \"$managed_pid\" 2>/dev/null || true; "
                "done; sleep 1; "
                "for pid_file in /from_host/bigcar-console/runtime/*.pid; do "
                "[ -f \"$pid_file\" ] || continue; "
                "managed_pid=$(cat \"$pid_file\" 2>/dev/null || true); "
                "[ -n \"$managed_pid\" ] && kill -KILL \"$managed_pid\" 2>/dev/null || true; "
                "rm -f \"$pid_file\"; done",
                timeout=8,
            )
            self._ros(
                "pkill -TERM -f '[r]untime_manager_dialog.py|[m]anaged_runtime_manager.py|[p]roc_manager.py|[r]viz|[y]hs_can_control_qt_node|[h]esai_lidar_node|[r]ostopic echo|[t]imeout .*rostopic|[f]or topic in /points_raw' 2>/dev/null || true; "
                "sleep 1; pkill -KILL -f '[r]untime_manager_dialog.py|[m]anaged_runtime_manager.py|[p]roc_manager.py|[r]viz|[y]hs_can_control_qt_node|[h]esai_lidar_node|[r]ostopic echo|[t]imeout .*rostopic|[f]or topic in /points_raw' 2>/dev/null || true; "
                "yes | rosnode cleanup >/dev/null 2>&1 || true",
                timeout=5,
            )
            self._run(["pkill", "-u", "nvidia", "-f", "tail -n 120 -F /home/nvidia/Desktop/bigcar-console/runtime/logs/autoware_ui.log"], timeout=3)
        if self.simulate:
            self._sim_stage = 1
        self._ros_cache = (0.0, set(), set())
        self._live_cache = (0.0, set())
        self.log("INFO", "system", "已停止控制台管理的全部 ROS 节点")

    def _restart_workflow(self) -> None:
        self._stop_all()
        with self._lock:
            self._emergency = False
            self._last_error = None
        self._ros_cache = (0.0, set(), set())
        self._live_cache = (0.0, set())
        self.log("INFO", "system", "操作流程已安全重置，可从底盘与雷达重新开始")

    def action(self, action: str, data: Dict[str, Any]) -> str:
        if action == "emergency_stop":
            self._emergency_stop()
            return "紧急停止已执行"
        if action == "stop_all":
            self._stop_all()
            return "全部节点已停止"
        allowed = {
            "environment_check", "start_hardware", "start_autoware", "start_localization",
            "load_route", "start_tracking", "launch_rviz", "restart_workflow",
            "save_selection", "update_parameters", "set_speed",
        }
        if action not in allowed:
            raise ControllerError("不允许的控制动作")
        if action == "set_speed":
            # 速度必须立即生效，所以不进 worker 队列：直接保存 + 下发，
            # 否则拖滑杆时会被 _busy 锁串行化成一次一个。
            result = self.apply_speed(self._bounded_float(data, "speed_limit_mps", SPEED_MIN_MPS, SPEED_MAX_MPS))
            return f"速度已设为 {result['speed_limit_mps']:.2f} m/s" + ("" if result["published"] else "（车端未运行，已保存）")
        with self._lock:
            if self._busy:
                raise ControllerError(f"正在执行：{self._busy}")
            self._busy = action
            self._last_error = None

        def worker() -> None:
            try:
                if action == "restart_workflow":
                    self._restart_workflow()
                elif action == "save_selection":
                    self._save_selection(data)
                elif action == "update_parameters":
                    self._update_parameters(data)
                elif self.simulate:
                    time.sleep(0.45)
                    stage_for_action = {name: index + 1 for index, name in enumerate(("environment_check", "start_hardware", "start_autoware", "start_localization", "load_route", "start_tracking"))}
                    self._sim_stage = max(self._sim_stage, stage_for_action.get(action, self._sim_stage))
                    self.log("INFO", action, "演示动作执行完成")
                else:
                    self._launch_stage(action, data)
                self._ros_cache = (0.0, set(), set())
                self._live_cache = (0.0, set())
            except Exception as exc:  # worker must always clear busy and expose the error
                message = str(exc)
                if action == "start_tracking":
                    try:
                        self._emergency_stop()
                    except Exception as stop_exc:
                        self.log("ERROR", "safety", f"启动失败后的停止操作异常：{stop_exc}")
                with self._lock:
                    self._last_error = message
                self.log("ERROR", action, message)
            finally:
                with self._lock:
                    self._busy = None

        threading.Thread(target=worker, name=f"action-{action}", daemon=True).start()
        messages = {
            "restart_workflow": "已开始重启操作流程",
            "save_selection": "正在保存默认文件",
            "update_parameters": "正在下发并保存运行参数",
        }
        return messages.get(action, f"已开始执行：{action}")

    def close(self) -> None:
        self._closing.set()
        self._speed_wake.set()
        self.live.close()
