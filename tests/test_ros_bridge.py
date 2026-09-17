"""Tests for the ROS-side bridge helpers (backend/ros_bridge.py).

The module targets Python 2.7 on the car but imports cleanly under Python 3 as
long as no ROS module is touched, so the parsers and the wire format can be
verified on the developer machine.
"""
import importlib.util
import io
import json
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from controller import BRIDGE_HEADER, BRIDGE_MAGIC, decode_bridge_frame


def load_bridge():
    spec = importlib.util.spec_from_file_location("ros_bridge_under_test", BACKEND / "ros_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RosBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_ascii_pcd_is_parsed(self):
        path = self.root / "ascii.pcd"
        path.write_text(
            "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z intensity\nSIZE 4 4 4 4\n"
            "TYPE F F F F\nCOUNT 1 1 1 1\nWIDTH 3\nHEIGHT 1\n"
            "VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 3\nDATA ascii\n"
            "1.5 -2.5 0.25 7\n0 0 0 0\nbad line\n3 4 5 9\n",
            encoding="utf-8")
        points = self.bridge.parse_pcd(str(path))
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0], (1.5, -2.5, 0.25))
        self.assertEqual(points[2], (3.0, 4.0, 5.0))

    def test_binary_pcd_is_parsed(self):
        path = self.root / "binary.pcd"
        blob = struct.pack("<8f", 1.0, 2.0, 3.0, 0.5, -1.0, -2.0, -3.0, 0.25)
        header = (
            "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z intensity\nSIZE 4 4 4 4\n"
            "TYPE F F F F\nCOUNT 1 1 1 1\nWIDTH 2\nHEIGHT 1\n"
            "VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 2\nDATA binary\n"
        ).encode()
        path.write_bytes(header + blob)
        points = self.bridge.parse_pcd(str(path))
        self.assertEqual(points, [(1.0, 2.0, 3.0), (-1.0, -2.0, -3.0)])

    def test_unsupported_pcd_encoding_is_reported(self):
        path = self.root / "binary_compressed.pcd"
        path.write_bytes(b"FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\nWIDTH 1\nPOINTS 1\nDATA binary_compressed\n")
        with self.assertRaisesRegex(ValueError, "压缩格式"):
            self.bridge.parse_pcd(str(path))

    def test_voxel_filter_thins_and_caps(self):
        dense = [(0.01 * index, 0.0, 0.0) for index in range(500)]
        thinned = self.bridge.voxel_filter(dense, 0.1, 10000)
        self.assertLess(len(thinned), len(dense))
        capped = self.bridge.voxel_filter(dense, 0.0, 25)
        self.assertEqual(len(capped), 25)

    def test_map_payload_is_flat_xyz(self):
        path = self.root / "map.pcd"
        path.write_text(
            "FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\nWIDTH 2\nHEIGHT 1\n"
            "POINTS 2\nDATA ascii\n1 2 3\n4 5 6\n", encoding="utf-8")
        flat = self.bridge.map_payload(str(path))
        self.assertEqual(len(flat), 6)
        self.assertAlmostEqual(flat[3], 4.0)

    def test_quaternion_yaw_matches_rotation(self):
        for degrees in (-90, -30, 0, 45, 135):
            angle = math.radians(degrees)
            quaternion = (0.0, 0.0, math.sin(angle / 2), math.cos(angle / 2))
            self.assertAlmostEqual(self.bridge.quaternion_yaw(quaternion), angle, places=6)

    def test_transform_points_applies_rotation_and_translation(self):
        rotation = self.bridge.quaternion_matrix((0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)))
        moved = self.bridge.transform_points([(1.0, 0.0, 0.0)], rotation, (1.0, 1.0, 0.0))
        self.assertAlmostEqual(moved[0][0], 1.0, places=5)
        self.assertAlmostEqual(moved[0][1], 2.0, places=5)

    def test_frame_wire_format_matches_the_host_decoder(self):
        """帧头必须是 5 字节标准尺寸，宿主机的解析器要能原样读回来。"""
        fake = io.BytesIO()
        original = self.bridge.STDOUT
        self.bridge.STDOUT = fake
        try:
            self.bridge.send_json(self.bridge.TYPE_BATTERY, {"soc": 88})
            self.bridge.cloud_frame(self.bridge.TYPE_CLOUD, 12.5, "map", [0.0, 0.0, 0.0, 1.0, 2.0, 3.0],
                                    {"took_ms": 4})
        finally:
            self.bridge.STDOUT = original
        raw = fake.getvalue()

        self.assertEqual(raw[:2], BRIDGE_MAGIC)
        kind, size = BRIDGE_HEADER.unpack_from(raw, 2)
        self.assertEqual(kind, self.bridge.TYPE_BATTERY)
        first = decode_bridge_frame(kind, raw[7:7 + size])
        self.assertEqual(first["data"]["soc"], 88)

        offset = 7 + size
        kind, size = BRIDGE_HEADER.unpack_from(raw, offset + 2)
        self.assertEqual(kind, self.bridge.TYPE_CLOUD)
        second = decode_bridge_frame(kind, raw[offset + 7:offset + 7 + size])
        self.assertEqual(second["header"]["frame_id"], "map")
        self.assertEqual(second["header"]["count"], 2)
        self.assertEqual(second["header"]["took_ms"], 4)
        self.assertEqual(struct.unpack("<6f", second["blob"]), (0.0, 0.0, 0.0, 1.0, 2.0, 3.0))

    def test_commands_toggle_streaming_and_map_requests(self):
        self.bridge.SCENE.streaming = False
        self.bridge.SCENE.pending_map = None
        self.bridge.handle_command({"cmd": "start", "cloud_hz": 30, "pose_hz": 0})
        self.assertTrue(self.bridge.SCENE.streaming)
        # 频率被夹在合理区间，避免浏览器请求把车端打满。
        self.assertEqual(self.bridge.SCENE.cloud_hz, 10.0)
        self.assertEqual(self.bridge.SCENE.pose_hz, 1.0)
        self.bridge.handle_command({"cmd": "map", "map": "map.pcd"})
        self.assertEqual(self.bridge.SCENE.pending_map, "map.pcd")
        self.bridge.handle_command({"cmd": "stop"})
        self.assertFalse(self.bridge.SCENE.streaming)

    def test_mock_waypoints_are_flat_pairs(self):
        points = self.bridge.mock_waypoints(9.0, 8)
        self.assertEqual(len(points), 16)
        self.assertAlmostEqual(points[0], 9.0, places=3)
        self.assertAlmostEqual(points[1], 0.0, places=3)


class CommandLoopTests(unittest.TestCase):
    def test_command_loop_reads_newline_delimited_json(self):
        import os
        bridge = load_bridge()
        bridge.SCENE.streaming = False
        reader, writer = os.pipe()
        with open(reader, "rb", buffering=0) as stream:
            original = sys.stdin
            sys.stdin = stream
            try:
                os.write(writer, b'{"cmd": "start"}\nnot json\n{"cmd": "map", "map": "a.pcd"}\n')
                os.close(writer)
                bridge.command_loop()
            finally:
                sys.stdin = original
        self.assertTrue(bridge.SCENE.streaming)
        self.assertEqual(bridge.SCENE.pending_map, "a.pcd")


if __name__ == "__main__":
    unittest.main()
