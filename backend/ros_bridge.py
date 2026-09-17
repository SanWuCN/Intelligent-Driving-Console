#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Live telemetry bridge: ROS topics -> framed binary stream on stdout.

Runs *inside* the Autoware container (Python 2.7 / ROS Melodic) as a long-lived
process started by the console backend:

    docker exec -i <container> python /from_host/bigcar-console/backend/ros_bridge.py

Everything it writes to stdout is a frame:

    magic 'BC' | type u8 | payload length u32 LE | payload

Cloud payloads are ``u32 LE json-header length | json header | float32 xyz``.
Control commands arrive on stdin, one JSON object per line:

    {"cmd": "start", "cloud_hz": 5, "pose_hz": 10}
    {"cmd": "map", "map": "map.pcd"}
    {"cmd": "stop"}

The bridge only streams what the console asks for, so a car nobody is watching
stays quiet.  ``--mock`` emits a synthetic scene instead of touching ROS, which
is how the host side is exercised on a developer machine.
"""

from __future__ import print_function

import array
import json
import math
import os
import select
import struct
import sys
import threading
import time

MAX_CLOUD_POINTS = 24000
MAX_MAP_POINTS = 260000
MAGIC = b"BC"

TYPE_HELLO = 1
TYPE_STATUS = 2
TYPE_BATTERY = 3
TYPE_POSE = 4
TYPE_WAYPOINTS = 5
TYPE_TRACE = 6
TYPE_CLOUD = 7
TYPE_MAP = 8
TYPE_ERROR = 9

_writer_lock = threading.Lock()
# `<B I`（带空格）表示标准尺寸：`<BI` 会被补成 7 字节，宿主机会解析错位。
_FRAME_HEADER = struct.Struct("<B I")


def _unbuffered(stream):
    """stdout 是管道时绝不能走块缓冲：一帧点云能到 1 MB，攒在缓冲里
    会让宿主机一直读不到数据。两种启动方式（python / python -u）都要能用。"""
    try:
        stream.reconfigure(write_through=True)   # Python 3
    except (AttributeError, ValueError, OSError):
        pass                                     # Python 2 本来就是无缓冲的
    return stream


# Python 2 的 sys.stdout 就是二进制；Python 3 需要 buffer 才能写字节。
# 车端是 Python 2.7，本地 mock 测试跑 Python 3，两边都要能用。
STDOUT = _unbuffered(getattr(sys.stdout, "buffer", sys.stdout))


def log(message):
    sys.stderr.write("ros_bridge: %s\n" % message)
    sys.stderr.flush()


# --------------------------------------------------------------------- output
def send_frame(kind, payload):
    frame = MAGIC + _FRAME_HEADER.pack(kind, len(payload)) + payload
    with _writer_lock:
        STDOUT.write(frame)
        STDOUT.flush()


def send_json(kind, data):
    send_frame(kind, json.dumps(data, separators=(",", ":")).encode("utf-8"))


def error_frame(message, scope="bridge"):
    try:
        send_json(TYPE_ERROR, {"scope": scope, "message": message, "time": time.time()})
    except Exception:
        log("failed to report: %s" % message)
    log("%s: %s" % (scope, message))


def _array_bytes(blob):
    """array.array -> bytes。Python 2.7 只有 tostring()，Python 3 只有 tobytes()。"""
    method = getattr(blob, "tobytes", None) or getattr(blob, "tostring")
    return method()


def cloud_frame(kind, stamp, frame_id, points, extra=None):
    """points: flat sequence of x, y, z numbers."""
    blob = array.array("f", points)
    if sys.byteorder != "little":
        blob.byteswap()
    meta = {"stamp": stamp, "frame_id": frame_id, "count": len(points) // 3}
    if extra:
        meta.update(extra)
    header = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    send_frame(kind, struct.pack("<I", len(header)) + header + _array_bytes(blob))


# ------------------------------------------------------------------ pointcloud
try:
    import numpy  # Jetson 镜像自带；Windows/精简环境没有时自动退回纯 Python
except ImportError:  # pragma: no cover
    numpy = None


def read_pointcloud2_fast(message):
    """numpy 版本：把 PointCloud2 直接当作结构化数组读出来。

    纯 Python 逐点 struct.unpack 在 Jetson 上要 1.3 秒一帧，
    会把订阅回调线程彻底堵死；numpy 版本是毫秒级。
    """
    fields = {field.name: (field.offset, field.datatype) for field in message.fields}
    if not all(name in fields for name in ("x", "y", "z")):
        return None
    # sensor_msgs/PointField: FLOAT32 = 7
    if any(fields[name][1] != 7 for name in ("x", "y", "z")):
        return None
    count = message.width * message.height
    if count <= 0:
        return None
    expected = count * message.point_step
    raw = message.data[:expected]
    if len(raw) < expected:
        return None
    names = ["x", "y", "z"]
    formats = ["<f4", "<f4", "<f4"]
    offsets = [fields["x"][0], fields["y"][0], fields["z"][0]]
    dtype = numpy.dtype({"names": names, "formats": formats, "offsets": offsets, "itemsize": message.point_step})
    points = numpy.frombuffer(raw, dtype=dtype)
    mask = numpy.isfinite(points["x"]) & numpy.isfinite(points["y"]) & numpy.isfinite(points["z"])
    return numpy.stack([points["x"][mask], points["y"][mask], points["z"][mask]], axis=-1)


def voxel_filter_fast(points, leaf, limit):
    """numpy 体素降采样：每个格子保留第一个点。"""
    if leaf <= 0:
        return points[:limit]
    keys = numpy.floor(points / leaf).astype(numpy.int64)
    # 把三列键压成一个整数，再用 unique(return_index=True) 取每组第一个。
    shifted = (keys[:, 0] + 524288) * 1048576 * 1048576 + (keys[:, 1] + 524288) * 1048576 + (keys[:, 2] + 524288)
    _unique, index = numpy.unique(shifted, return_index=True)
    index.sort()
    kept = points[index]
    return kept[:limit]


def transform_points_fast(points, rotation, translation):
    matrix = numpy.asarray(rotation, dtype=numpy.float64).reshape(3, 3)
    offset = numpy.asarray(translation, dtype=numpy.float64).reshape(3)
    return points.dot(matrix.T) + offset


def read_pointcloud2(message):
    """Return [(x, y, z)] from a sensor_msgs/PointCloud2 without numpy."""
    offsets = {}
    for field in message.fields:
        offsets[field.name] = field.offset
    if not all(name in offsets for name in ("x", "y", "z")):
        return []
    step = message.point_step
    data = message.data
    count = message.width * message.height
    xo, yo, zo = offsets["x"], offsets["y"], offsets["z"]
    unpack = struct.unpack_from
    points = []
    append = points.append
    for index in range(count):
        base = index * step
        try:
            x = unpack("<f", data, base + xo)[0]
            y = unpack("<f", data, base + yo)[0]
            z = unpack("<f", data, base + zo)[0]
        except struct.error:
            break
        if x != x or y != y or z != z:  # NaN
            continue
        append((x, y, z))
    return points


def voxel_filter(points, leaf, limit):
    """Cheap 3D voxel downsample, keeping the first point of every cell."""
    if leaf <= 0:
        return points[:limit]
    seen = set()
    kept = []
    add = kept.append
    for x, y, z in points:
        key = (int(math.floor(x / leaf)), int(math.floor(y / leaf)), int(math.floor(z / leaf)))
        if key in seen:
            continue
        seen.add(key)
        add((x, y, z))
        if len(kept) >= limit:
            break
    return kept


def transform_points(points, rotation, translation):
    """rotation: 3x3 row-major tuple; translation: (x, y, z)."""
    r00, r01, r02, r10, r11, r12, r20, r21, r22 = rotation
    tx, ty, tz = translation
    out = []
    append = out.append
    for x, y, z in points:
        append((
            r00 * x + r01 * y + r02 * z + tx,
            r10 * x + r11 * y + r12 * z + ty,
            r20 * x + r21 * y + r22 * z + tz,
        ))
    return out


def quaternion_matrix(q):
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy),
        2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx),
        2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy),
    )


def quaternion_yaw(q):
    x, y, z, w = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def matrix_apply(matrix, vector):
    return tuple(
        sum(matrix[row * 3 + k] * vector[k] for k in range(3))
        for row in range(3)
    )


# -------------------------------------------------------------------- map (PCD)
def parse_pcd(path):
    """Minimal PCD reader: ascii and binary, needs x/y/z fields."""
    with open(path, "rb") as handle:
        header = {}
        while True:
            line = handle.readline()
            if not line:
                raise ValueError("PCD 缺少 DATA 段")
            text = line.decode("ascii", "ignore").strip()
            if not text or text.startswith("#"):
                continue
            parts = text.split()
            keyword = parts[0].upper()
            header[keyword] = parts[1:]
            if keyword == "DATA":
                break
        fields = header.get("FIELDS", [])
        if not all(name in fields for name in ("x", "y", "z")):
            raise ValueError("PCD 缺少 x/y/z 字段")
        sizes = [int(value) for value in header.get("SIZE", ["4"] * len(fields))]
        types = header.get("TYPE", ["F"] * len(fields))
        counts = [int(value) for value in header.get("COUNT", ["1"] * len(fields))]
        total = int(header.get("POINTS", header.get("WIDTH", ["0"]))[0])
        mode = header.get("DATA", ["ascii"])[0].lower()
        index = {name: fields.index(name) for name in ("x", "y", "z")}
        if mode == "ascii":
            points = []
            append = points.append
            for raw in handle:
                parts = raw.split()
                if len(parts) < len(fields):
                    continue
                try:
                    append((float(parts[index["x"]]), float(parts[index["y"]]), float(parts[index["z"]])))
                except (ValueError, IndexError):
                    continue
                if len(points) >= total:
                    break
            return points
        if mode != "binary":
            raise ValueError("暂不支持 PCD 压缩格式（%s），请另存为 ascii 或 binary" % mode)
        offsets = []
        offset = 0
        for size, count in zip(sizes, counts):
            offsets.append(offset)
            offset += size * count
        point_step = offset
        formats = {
            ("F", 4): "f", ("F", 8): "d",
            ("I", 1): "B", ("I", 2): "H", ("I", 4): "I",
            ("U", 1): "B", ("U", 2): "H", ("U", 4): "I",
        }
        point_format = "<" + "".join(
            formats.get((types[position], sizes[position]), "f") * counts[position]
            for position in range(len(fields))
        )
        points = []
        append = points.append
        unpack_from = struct.unpack_from
        for _ in range(total):
            raw = handle.read(point_step)
            if len(raw) < point_step:
                break
            try:
                values = unpack_from(point_format, raw)
            except struct.error:
                break
            append((values[index["x"]], values[index["y"]], values[index["z"]]))
        return points


def map_payload(path, leaf=0.08):
    points = voxel_filter(parse_pcd(path), leaf, MAX_MAP_POINTS)
    flat = []
    extend = flat.extend
    for x, y, z in points:
        extend((x, y, z))
    return flat


# ----------------------------------------------------------------------- state
class Scene(object):
    """Newest sample of every topic, plus the stream control state."""

    def __init__(self):
        self.lock = threading.RLock()
        self.battery = None
        self.chassis = None
        self.pose = None
        self.cloud = None
        self.cloud_stamp = 0.0
        self.cloud_frame = "map"
        self.cloud_latency = None
        self.trace = []
        self.streaming = False
        self.cloud_hz = 5.0
        self.pose_hz = 10.0
        self.pending_map = None
        self.pending_map_token = None
        self.map_sent = None
        self.pending_waypoints = None
        self.pose_samples = []

    def push_trace(self, x, y, yaw):
        now = time.time()
        self.trace.append((now, x, y, yaw))
        cutoff = now - 90.0
        while self.trace and self.trace[0][0] < cutoff:
            self.trace.pop(0)
        del self.trace[:-1500]

    def snapshot(self, mock=False):
        with self.lock:
            return {
                "pose": self.pose, "cloud": self.cloud, "cloud_stamp": self.cloud_stamp,
                "cloud_frame": self.cloud_frame, "cloud_latency": self.cloud_latency,
                "battery": self.battery, "chassis": self.chassis,
                "trace": list(self.trace), "streaming": self.streaming,
                "cloud_hz": self.cloud_hz, "pose_hz": self.pose_hz,
                "pending_map": self.pending_map, "pending_map_token": self.pending_map_token,
                "map_sent": self.map_sent,
                "pending_waypoints": self.pending_waypoints,
                "pose_samples": list(self.pose_samples), "mock": mock,
            }

    def requested_map(self):
        with self.lock:
            return self.pending_map, self.pending_map_token

    def get_cloud_hz(self):
        with self.lock:
            return self.cloud_hz


SCENE = Scene()


# ------------------------------------------------------------------------ mock
def mock_waypoints(radius, count=160):
    flat = []
    for index in range(count):
        theta = index / float(count) * 2 * math.pi
        flat.append(round(radius * math.cos(theta), 3))
        flat.append(round(radius * math.sin(theta), 3))
    return flat


# ------------------------------------------------------------------- emission
class Pump(object):
    """把场景里最新的数据按各自的频率推到 stdout。mock 和 ROS 两个循环共用。"""

    def __init__(self):
        self.pose = 0.0
        self.battery = 0.0
        self.trace = 0.0
        self.status = 0.0
        self.cloud = 0.0
        self.waypoints = None
        self.map_name = None

    def run(self, data, now):
        pending = data["pending_waypoints"]
        if pending is not None and pending is not self.waypoints:
            self.waypoints = pending
            send_json(TYPE_WAYPOINTS, pending)
        pose = data["pose"]
        if pose and now - self.pose >= 1.0 / max(data["pose_hz"], 0.5):
            self.pose = now
            chassis = data["chassis"] or {}
            send_json(TYPE_POSE, {
                "x": pose["x"], "y": pose["y"], "yaw": pose["yaw"], "z": pose.get("z", 0.0),
                "frame_id": pose.get("frame_id") or "map", "stamp": pose.get("stamp", now),
                "speed": chassis.get("speed"), "steering": chassis.get("steering"),
            })
        battery = data["battery"]
        if battery and now - self.battery >= 0.5:
            self.battery = now
            send_json(TYPE_BATTERY, battery)
        trace = data["trace"]
        if trace and now - self.trace >= 1.0:
            self.trace = now
            recent = trace[-900:]
            send_json(TYPE_TRACE, {
                "count": len(recent),
                "points": [round(value, 3) for item in recent for value in (item[1], item[2])],
            })
        cloud = data["cloud"]
        if (data["streaming"] and cloud and data["cloud_stamp"]
                and now - self.cloud >= 1.0 / max(data["cloud_hz"], 1.0)
                and now - data["cloud_stamp"] <= 0.6):
            self.cloud = now
            cloud_frame(TYPE_CLOUD, data["cloud_stamp"], data["cloud_frame"], cloud, {
                "age_ms": round((now - data["cloud_stamp"]) * 1000.0, 1),
                "took_ms": data["cloud_latency"],
            })
        if now - self.status >= 1.0:
            self.status = now
            samples = data["pose_samples"]
            recent = len([value for value in samples if value > now - 3.0])
            send_json(TYPE_STATUS, {
                "mock": data["mock"], "ros": not data["mock"], "streaming": data["streaming"],
                "pose_hz": round(recent / 3.0, 1), "cloud": bool(cloud),
                "battery": bool(battery), "frame_id": pose and pose.get("frame_id"),
                "stamp": now,
            })

    def serve_map(self, requested, token, now, loader):
        """请求了新地图（或新 token）就解析并发一次；失败只报错，不重试刷屏。"""
        if not requested or token == self.map_name:
            return
        self.map_name = token
        try:
            flat = loader(requested)
        except Exception as exc:
            error_frame("地图解析失败：%s" % exc, "map")
            return
        cloud_frame(TYPE_MAP, now, "map", flat, {"name": requested})


def run_mock():
    log("mock mode: synthetic scene, no ROS")
    send_json(TYPE_HELLO, {
        "role": "bridge", "mock": True, "python": sys.version.split()[0],
        "topics": ["/bms_flag_Infor_fb", "/bms_Infor_fb", "/current_pose", "/final_waypoints", "/points_raw"],
    })
    started = time.time()
    radius = 9.0
    cloud_budget = 18000
    pump = Pump()
    while True:
        now = time.time()
        elapsed = now - started
        requested_map, map_token = SCENE.requested_map()
        if requested_map:
            mock_map = os.path.join(os.environ.get("BIGCAR_MAP_DIR", "/from_host"), requested_map)
            fallback = mock_waypoints(40.0, 4000)

            def loader(name, mock_map=mock_map, fallback=fallback):
                if os.path.isfile(mock_map):
                    return map_payload(mock_map)
                # 本地没有真实 PCD 时给一个环状假地图，保证前端能跑通。
                return [value for index in range(0, len(fallback), 2)
                        for value in (fallback[index], fallback[index + 1], -1.35)]

            pump.serve_map(requested_map, map_token, now, loader)
        with SCENE.lock:
            streaming = SCENE.streaming
        if not streaming:
            time.sleep(0.15)
            continue
        angle = (elapsed * 0.25) % (2 * math.pi)
        x = radius * math.cos(angle)
        y = radius * math.sin(angle)
        yaw = angle + math.pi / 2
        with SCENE.lock:
            SCENE.pose = {"x": x, "y": y, "z": 0.0, "yaw": yaw, "qx": 0.0, "qy": 0.0,
                          "qz": math.sin(yaw / 2), "qw": math.cos(yaw / 2),
                          "frame_id": "map", "stamp": now}
            SCENE.chassis = {"speed": round(0.2 + 0.1 * math.sin(elapsed), 3), "steering": 0.0,
                             "gear": 1, "mode": 0, "brake": 0, "stamp": now}
            SCENE.battery = {"soc": 94 - int(elapsed / 12) % 15, "voltage": round(49.79 - 0.01 * (int(elapsed) % 50), 2),
                             "current": -1.1, "remaining_ah": 18.8, "low": False, "charge": False,
                             "temp_high": 26.0, "temp_low": 25.0, "stamp": now}
            SCENE.push_trace(x, y, yaw)
            SCENE.pose_samples.append(now)
            del SCENE.pose_samples[:-60]
            if SCENE.pending_waypoints is None:
                SCENE.pending_waypoints = {"stamp": now, "frame_id": "map", "count": 160,
                                           "points": mock_waypoints(radius)}
        points = []
        append = points.append
        for ring in range(16):
            height = -1.55 + ring * 0.1
            for beam in range(160):
                bearing = beam / 160.0 * 2 * math.pi + yaw
                distance = 6.0 + 5.0 * math.sin(3 * bearing + elapsed)
                append(x + distance * math.cos(bearing))
                append(y + distance * math.sin(bearing))
                append(height)
        del points[cloud_budget * 3:]
        with SCENE.lock:
            SCENE.cloud = points
            SCENE.cloud_stamp = now
            SCENE.cloud_frame = "map"
            SCENE.cloud_latency = 3.5
        pump.run(SCENE.snapshot(mock=True), time.time())
        time.sleep(1.0 / max(SCENE.get_cloud_hz(), 1.0))


def run_ros():
    import rospy
    from geometry_msgs.msg import PoseStamped
    from sensor_msgs.msg import PointCloud2
    from autoware_msgs.msg import Lane
    from yhs_can_msgs.msg import bms_Infor_fb, bms_flag_Infor_fb, ctrl_fb

    # Reconnecting sessions may overlap while the old Docker process exits.
    # Unique names prevent ROS master from shutting down the other session.
    rospy.init_node("bigcar_live_bridge", anonymous=True, disable_signals=True)
    send_json(TYPE_HELLO, {
        "role": "bridge", "mock": False, "python": sys.version.split()[0],
        "topics": ["/bms_flag_Infor_fb", "/bms_Infor_fb", "/current_pose", "/final_waypoints", "/points_raw"],
    })

    listener = {"buffer": None}
    try:
        import tf2_ros
        listener["buffer"] = tf2_ros.Buffer()
        tf2_ros.TransformListener(listener["buffer"])
    except Exception as exc:  # pragma: no cover - depends on the car image
        log("tf2_ros unavailable (%s); using the fixed lidar mount" % exc)

    def translator(frame_id):
        """Map frame_id -> map: (rotation, translation, target_frame)."""
        with SCENE.lock:
            pose = SCENE.pose
        if listener["buffer"] is not None:
            try:
                transform = listener["buffer"].lookup_transform(
                    "map", frame_id, rospy.Time(0), rospy.Duration(0.05))
                rotation = quaternion_matrix((transform.transform.rotation.x, transform.transform.rotation.y,
                                              transform.transform.rotation.z, transform.transform.rotation.w))
                translation = (transform.transform.translation.x, transform.transform.translation.y,
                               transform.transform.translation.z)
                return rotation, translation, "map"
            except Exception:
                pass
        if pose is not None:
            # Approximate: current map->base_link pose applied to the lidar mount.
            rotation = quaternion_matrix((pose["qx"], pose["qy"], pose["qz"], pose["qw"]))
            return rotation, matrix_apply(rotation, LIDAR_FALLBACK), "map"
        return None, None, frame_id

    def on_battery(message):
        with SCENE.lock:
            SCENE.battery = {
                "soc": float(message.bms_flag_Infor_soc),
                "low": bool(message.bms_flag_Infor_single_uv or message.bms_flag_Infor_uv),
                "over": bool(message.bms_flag_Infor_ov or message.bms_flag_Infor_single_ov),
                "charge": bool(message.bms_flag_Infor_charge_flag),
                "temp_high": float(message.bms_flag_Infor_hight_temperature),
                "temp_low": float(message.bms_flag_Infor_low_temperature),
                "stamp": time.time(),
            }

    def on_battery_info(message):
        with SCENE.lock:
            merged = dict(SCENE.battery or {})
            merged.update({
                "voltage": float(message.bms_Infor_voltage),
                "current": float(message.bms_Infor_current),
                "remaining_ah": float(message.bms_Infor_remaining_capacity),
                "stamp": time.time(),
            })
            SCENE.battery = merged

    def on_chassis(message):
        with SCENE.lock:
            SCENE.chassis = {
                "speed": float(message.ctrl_fb_velocity),
                "steering": float(message.ctrl_fb_steering),
                "gear": int(message.ctrl_fb_gear),
                "mode": int(message.ctrl_fb_mode),
                "brake": int(message.ctrl_fb_Brake),
                "stamp": time.time(),
            }

    def on_pose(message):
        position = message.pose.position
        orientation = message.pose.orientation
        yaw = quaternion_yaw((orientation.x, orientation.y, orientation.z, orientation.w))
        now = time.time()
        with SCENE.lock:
            SCENE.pose = {
                "x": position.x, "y": position.y, "z": position.z, "yaw": yaw,
                "qx": orientation.x, "qy": orientation.y, "qz": orientation.z, "qw": orientation.w,
                "frame_id": message.header.frame_id or "map", "stamp": now,
            }
            SCENE.push_trace(position.x, position.y, yaw)
            SCENE.pose_samples.append(now)
            del SCENE.pose_samples[:-60]

    def on_waypoints(message):
        if len(message.waypoints) < 2:
            return
        step = max(1, len(message.waypoints) // 400)
        points = []
        for index in range(0, len(message.waypoints), step):
            waypoint = message.waypoints[index]
            points.append(round(waypoint.pose.pose.position.x, 3))
            points.append(round(waypoint.pose.pose.position.y, 3))
        last = message.waypoints[-1]
        points.append(round(last.pose.pose.position.x, 3))
        points.append(round(last.pose.pose.position.y, 3))
        with SCENE.lock:
            SCENE.pending_waypoints = {
                "stamp": time.time(), "frame_id": message.header.frame_id or "map",
                "count": len(points) // 2, "points": points,
            }

    def on_cloud(message):
        started = time.time()
        rotation, translation, target = translator(message.header.frame_id or "velodyne")
        if numpy is not None:
            points = read_pointcloud2_fast(message)
            if points is None or not len(points):
                return
            points = voxel_filter_fast(points, 0.12, MAX_CLOUD_POINTS)
            if rotation is not None:
                points = transform_points_fast(points, rotation, translation)
            flat = points.astype("<f4").ravel().tolist()
        else:
            points = read_pointcloud2(message)
            if not points:
                return
            points = voxel_filter(points, 0.12, MAX_CLOUD_POINTS)
            if rotation is not None:
                points = transform_points(points, rotation, translation)
            flat = []
            extend = flat.extend
            for x, y, z in points:
                extend((x, y, z))
        with SCENE.lock:
            SCENE.cloud = flat
            SCENE.cloud_stamp = time.time()
            SCENE.cloud_frame = target
            SCENE.cloud_latency = round((time.time() - started) * 1000.0, 1)

    rospy.Subscriber("/bms_flag_Infor_fb", bms_flag_Infor_fb, on_battery, queue_size=1)
    rospy.Subscriber("/bms_Infor_fb", bms_Infor_fb, on_battery_info, queue_size=1)
    rospy.Subscriber("/ctrl_fb", ctrl_fb, on_chassis, queue_size=1)
    rospy.Subscriber("/current_pose", PoseStamped, on_pose, queue_size=1)
    rospy.Subscriber("/final_waypoints", Lane, on_waypoints, queue_size=1)
    rospy.Subscriber("/points_raw", PointCloud2, on_cloud, queue_size=1)
    log("subscribed; waiting for commands on stdin")

    pump = Pump()

    while not rospy.is_shutdown():
        now = time.time()
        data = SCENE.snapshot()
        requested_map = data["pending_map"]
        if requested_map:
            def loader(name):
                candidate = os.path.join("/from_host", name)
                if not os.path.isfile(candidate):
                    raise ValueError("地图文件不存在：%s" % candidate)
                return map_payload(candidate)

            pump.serve_map(requested_map, data["pending_map_token"], now, loader)
        # 航点只推最新一帧，避免浏览器积压。
        data["pending_waypoints"] = data["pending_waypoints"] or None
        if data["pending_waypoints"]:
            with SCENE.lock:
                SCENE.pending_waypoints = None
        pump.run(data, now)
        time.sleep(0.05)

    send_json(TYPE_STATUS, {"ros": False, "stopping": True})


LIDAR_FALLBACK = (1.2, 0.0, 2.0)


# -------------------------------------------------------------------- commands
def handle_command(command):
    name = command.get("cmd")
    with SCENE.lock:
        if name == "start":
            SCENE.streaming = True
            try:
                SCENE.cloud_hz = min(max(float(command.get("cloud_hz", 5.0)), 0.5), 10.0)
                SCENE.pose_hz = min(max(float(command.get("pose_hz", 10.0)), 1.0), 20.0)
            except (TypeError, ValueError):
                pass
        elif name == "stop":
            SCENE.streaming = False
        elif name == "map":
            name_value = str(command.get("map", ""))
            SCENE.pending_map = name_value or None
            # token 变了就重发一次：新开的页面/重连也要拿到地图那一帧。
            SCENE.pending_map_token = str(command.get("token", "")) or name_value
            SCENE.map_sent = None
        elif name == "ping":
            pass
    if name == "ping":
        send_json(TYPE_STATUS, {"pong": time.time()})


def command_loop():
    """Read newline-delimited JSON commands from stdin without busy-waiting."""
    buffer = b""
    while True:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        except (select.error, ValueError):
            return
        if not ready:
            continue
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(command, dict):
                handle_command(command)


def main():
    args = sys.argv[1:]
    mock = "--mock" in args
    worker = threading.Thread(target=command_loop, name="bridge-commands")
    worker.daemon = True
    worker.start()
    if mock:
        run_mock()
    else:
        run_ros()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        error_frame(str(exc))
        raise
