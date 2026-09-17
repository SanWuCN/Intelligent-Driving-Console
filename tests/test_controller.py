import json
import struct
import subprocess
import threading
import time
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from controller import BigCarController, ControllerError


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.root / "runtime").mkdir()
        (self.root / "runtime" / "config.json").write_text(
            '{"container":"demo","data_dir":"%s","control_token":"test"}' % str(self.data),
            encoding="utf-8",
        )
        self.controller = BigCarController(self.root, simulate=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_route_parsing_and_length(self):
        (self.data / "route.csv").write_text("x,y,z,yaw,velocity\n0,0,0,0,1\n3,4,0,0,1\n", encoding="utf-8")
        route = self.controller.route_data("route.csv")
        self.assertEqual(len(route["points"]), 2)
        self.assertAlmostEqual(route["length_m"], 5.0)

    def test_file_traversal_is_rejected(self):
        with self.assertRaises(ControllerError):
            self.controller.resolve_data_file("../secret.csv", ".csv")

    def test_rviz_runs_in_independent_systemd_unit_with_host_log(self):
        import shlex
        import subprocess
        with patch.object(self.controller, "_ros", return_value=subprocess.CompletedProcess([], 1)), patch.object(self.controller, "_run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as run, patch("controller.time.sleep"):
            self.controller._start_rviz()
        args = run.call_args_list[0].args[0]
        self.assertEqual(args[0], "systemd-run")
        self.assertTrue(args[1].startswith("--unit=bigcar-rviz-"))
        command = args[-1]
        tokens = shlex.split(command)
        self.assertEqual(tokens[tokens.index(">>") + 1], str(self.controller.logs_dir / "rviz.log"))
        self.assertIn("nsenter", tokens)
        self.assertNotIn("&", tokens)
        inner = tokens[tokens.index("-lc") + 1]
        self.assertIn("/from_host/bigcar-console/runtime/rviz.pid", inner)

    def test_guard_disarmed_or_stale_cannot_show_tracking_running(self):
        self.controller._sim_stage = 6
        self.controller.config["person_guard_enabled"] = True
        for sample in [(time.monotonic(), {"armed": False}), (0, {"armed": True})]:
            self.controller._guard_cache = sample
            state = self.controller.snapshot()
            self.assertEqual(state["current_stage"], 5)
            self.assertEqual(state["workflow"][5]["state"], "current")
        self.controller._guard_cache = (time.monotonic(), {"armed": True})
        self.assertEqual(self.controller.snapshot()["current_stage"], 6)

    def test_simulated_workflow_progresses(self):
        (self.data / "map.pcd").write_text("VERSION .7\n", encoding="utf-8")
        (self.data / "route.csv").write_text("0,0,0,0,1\n1,0,0,0,1\n", encoding="utf-8")
        self.controller.action("start_hardware", {})
        import time
        time.sleep(0.6)
        self.assertEqual(self.controller.snapshot()["current_stage"], 2)

    def test_emergency_stop_requires_no_running_stage(self):
        self.controller.action("emergency_stop", {})
        state = self.controller.snapshot()
        self.assertTrue(state["emergency"])
        self.assertLessEqual(state["current_stage"], 5)

    def test_restart_workflow_resets_to_safe_initial_stage(self):
        self.controller._sim_stage = 5
        self.controller._emergency = True
        self.controller._restart_workflow()
        state = self.controller.snapshot()
        self.assertEqual(state["current_stage"], 1)
        self.assertFalse(state["emergency"])
        self.assertEqual(state["workflow"][1]["state"], "current")

    def test_state_exposes_live_topics_and_manual_localization_action(self):
        self.controller._sim_stage = 3
        state = self.controller.snapshot()
        self.assertIn("/points_raw", state["live_topics"])
        self.assertNotIn("/current_pose", state["live_topics"])
        self.assertEqual(state["workflow"][3]["action_label"], "打开 RViz 并开始标定")

    def test_real_stage_requires_live_chassis_control_output(self):
        nodes = {
            "/yhs_can_control_qt_node", "/hesai_lidar", "/base_link_to_localizer",
            "/robot_state_publisher", "/voxel_grid_filter", "/ring_ground_filter",
            "/points_map_loader", "/ndt_matching", "/pose_relay", "/vel_relay",
            "/waypoint_loader", "/lane_rule", "/lane_stop", "/lane_select",
            "/astar_avoid", "/velocity_set", "/pure_pursuit", "/twist_filter",
            "/twist_gate",
        }
        live = {"/points_raw", "/current_pose", "/final_waypoints", "/ctrl_fb"}
        self.controller.simulate = False
        with patch.object(self.controller, "_container_running", return_value=True):
            self.assertEqual(self.controller._stage(nodes, live), 5)
            self.assertEqual(self.controller._stage(nodes, live | {"/ctrl_cmd"}), 6)
            self.assertEqual(self.controller._stage(nodes, (live | {"/ctrl_cmd"}) - {"/ctrl_fb"}), 1)

    def test_tracking_enables_control_command_output(self):
        self.controller.simulate = False
        started = []
        with patch.object(
            self.controller,
            "_live_topics",
            return_value={"/points_raw", "/current_pose", "/final_waypoints", "/ctrl_fb"},
        ), patch.object(
            self.controller, "_start_process", side_effect=lambda name, command: started.append((name, command))
        ), patch.object(self.controller, "_topic_has_message", return_value=True), patch("controller.time.sleep"):
            self.controller._launch_stage("start_tracking", {"safety_confirmed": True})

        pure_pursuit = next(command for name, command in started if name == "pure_pursuit")
        self.assertIn("publishes_for_steering_robot:=true", pure_pursuit)
        self.assertIn("minimum_lookahead_distance:=2.0", pure_pursuit)

    def test_person_guard_tracking_remaps_and_requires_arm(self):
        import subprocess
        self.controller.simulate = False
        self.controller.config["person_guard_enabled"] = True
        with patch.object(self.controller, "_live_topics", return_value={
            "/points_raw", "/current_pose", "/final_waypoints", "/ctrl_fb"
        }), patch.object(self.controller, "_start_process") as start, patch.object(
            self.controller, "_topic_has_message", return_value=True
        ), patch.object(self.controller, "_publish_live_parameters"), patch.object(
            self.controller, "_ros", return_value=subprocess.CompletedProcess([], 0, "success: True")
        ) as ros, patch("controller.time.sleep"):
            self.controller._launch_stage("start_tracking", {"safety_confirmed": True})
        self.assertIn("person_guard_tracking.launch", start.call_args_list[0].args[1])
        self.assertIn("data: false", ros.call_args_list[0].args[0])
        self.assertIn("data: true", ros.call_args_list[-1].args[0])

    def test_person_guard_rejects_unready_camera(self):
        import subprocess
        self.controller.simulate = False
        self.controller.config["person_guard_enabled"] = True
        with patch.object(self.controller, "_live_topics", return_value={
            "/points_raw", "/current_pose", "/final_waypoints", "/ctrl_fb"
        }), patch.object(self.controller, "_start_process"), patch.object(
            self.controller, "_topic_has_message", return_value=True
        ), patch.object(self.controller, "_publish_live_parameters"), patch.object(
            self.controller, "_ros", side_effect=[subprocess.CompletedProcess([], 0, "success: True"),
                                                   subprocess.CompletedProcess([], 0, "success: False")]
        ), patch.object(self.controller, "_emergency_stop") as stop, patch("controller.time.sleep"):
            with self.assertRaisesRegex(ControllerError, "人体停车门控拒绝启动"):
                self.controller._launch_stage("start_tracking", {"safety_confirmed": True})
            stop.assert_not_called()

    def test_parameter_timeout_is_bounded_and_readable(self):
        import subprocess
        self.controller.simulate = False
        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_ros", side_effect=subprocess.TimeoutExpired("publish", 17)
        ) as run:
            with self.assertRaisesRegex(ControllerError, "实时参数下发超时.*waypoint_follower"):
                self.controller._publish_live_parameters(self.controller.parameters())
        self.assertIn("timeout -k 2 12 rostopic pub -1", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 17)

    def test_tracking_worker_stops_partial_start_on_failure(self):
        import time
        self.controller.simulate = False
        with patch.object(self.controller, "_launch_stage", side_effect=ControllerError("parameter failure")), patch.object(
            self.controller, "_emergency_stop"
        ) as stop:
            self.controller.action("start_tracking", {"safety_confirmed": True})
            deadline = time.monotonic() + 2
            while self.controller._busy and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(self.controller._busy)
            stop.assert_called_once()
            self.assertEqual(self.controller._last_error, "parameter failure")

    def test_tracking_rejects_missing_chassis_feedback(self):
        self.controller.simulate = False
        with patch.object(self.controller, "_live_topics", return_value={
            "/points_raw", "/current_pose", "/final_waypoints"
        }), patch.object(self.controller, "_start_process") as start:
            with self.assertRaisesRegex(ControllerError, "底盘没有实时反馈"):
                self.controller._launch_stage("start_tracking", {"safety_confirmed": True})
            start.assert_not_called()

    def test_stale_chassis_registration_is_not_healthy(self):
        self.controller._sim_stage = 6
        with patch.object(self.controller, "_live_topics", return_value={"/ctrl_cmd", "/points_raw"}):
            state = self.controller.snapshot()
        modules = {module["key"]: module for module in state["modules"]}
        self.assertEqual(modules["chassis"]["state"], "error")
        self.assertEqual(modules["planning"]["state"], "warn")
        self.assertEqual(state["workflow"][5]["state"], "blocked")

    def test_route_uses_five_centimeter_obstacle_stop_distance(self):
        (self.data / "route.csv").write_text("0,0,0,0,1\n1,0,0,0,1\n", encoding="utf-8")
        self.controller.simulate = False
        self.controller.config["parameters"] = {
            "speed_limit_mps": 0.2,
            "lookahead_distance_m": 2.0,
            "obstacle_stop_distance_m": 0.05,
            "auto_loop": False,
        }
        started = []
        required_nodes = {"/base_link_to_localizer", "/robot_state_publisher"}
        with patch.object(self.controller, "_live_topics", return_value={"/current_pose"}), patch.object(
            self.controller, "_ros_snapshot", return_value=(required_nodes, set())
        ), patch.object(
            self.controller, "_start_process", side_effect=lambda name, command: started.append((name, command))
        ):
            self.controller._launch_stage("load_route", {"route": "route.csv"})

        velocity_set = next(command for name, command in started if name == "velocity_set")
        self.assertIn("stop_distance_obstacle:=0.05", velocity_set)
        self.assertIn("detection_range:=0.0", velocity_set)
        self.assertIn("points_threshold:=2000000000", velocity_set)
        waypoint_loader = next(command for name, command in started if name == "waypoint_loader")
        self.assertIn("velocity_max:=0.72", waypoint_loader)
        self.assertIn("replanning_mode:=true", waypoint_loader)
        astar_avoid = next(command for name, command in started if name == "astar_avoid")
        self.assertIn("closest_search_size:=300", astar_avoid)

    def test_lidar_obstacle_avoidance_is_disabled_in_live_config(self):
        import subprocess
        import controller as controller_module

        self.assertFalse(controller_module.LIDAR_OBSTACLE_AVOIDANCE_ENABLED)
        published = []

        def record(command, timeout=6):
            published.append(command)
            return subprocess.CompletedProcess([], 0, stdout="")

        self.controller.simulate = False
        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_ros", side_effect=record
        ):
            self.controller._publish_live_parameters({
                "speed_limit_mps": 0.2,
                "lookahead_distance_m": 2.0,
                "obstacle_stop_distance_m": 0.05,
            })

        velocity_set = next(command for command in published if "/config/velocity_set" in command)
        self.assertIn("detection_range: 0.0", velocity_set)
        self.assertIn("threshold_points: 2000000000", velocity_set)
        # The lidar thresholds must not silently fall back to the Autoware defaults.
        self.assertNotIn("detection_range: 1.3", velocity_set)
        self.assertNotIn("threshold_points: 10,", velocity_set)
        self.assertIn("stop_distance_obstacle: 0.0500", velocity_set)

    def test_loop_route_is_repeated_and_speed_is_rewritten(self):
        source = self.data / "loop.csv"
        source.write_text(
            "x,y,z,yaw,velocity\n0,0,0,0,1\n1,0,0,0,1\n0.2,0.1,0,0,0\n",
            encoding="utf-8",
        )
        target = self.controller._prepare_loop_route(source, 0.2, laps=3)
        rows = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row.endswith(",0.7200") for row in rows[1:]))

    def test_loop_route_rejects_non_closed_path(self):
        source = self.data / "open.csv"
        source.write_text("0,0,0,0,1\n1,0,0,0,1\n4,0,0,0,1\n", encoding="utf-8")
        with self.assertRaisesRegex(ControllerError, "路径未闭环"):
            self.controller._prepare_loop_route(source, 0.2, laps=2)

    def test_loop_csv_preserves_autoware_change_flag_header(self):
        source = self.data / "route.csv"
        source.write_bytes(b"x,y,z,yaw,velocity,change_flag\r\n0,0,0,0,0,0\r\n1,0,0,0,1,1\r\n0,0,0,0,0,0\r\n")
        target = self.controller._prepare_loop_route(source, 0.2, laps=2)
        raw = target.read_bytes()
        self.assertNotIn(b"\r", raw)
        # Match the loader's getline + comma split, without Python newline normalization.
        lines = raw.decode().split("\n")
        headers = lines[0].split(",")
        self.assertEqual(headers[-1], "change_flag")
        flags = [int(dict(zip(headers, line.split(",")))["change_flag"]) for line in lines[1:] if line]
        self.assertEqual(flags, [0, 1, 0, 0, 1, 0])

    def test_parameters_are_validated_and_persisted(self):
        self.controller._update_parameters({
            "speed_limit_mps": 0.25,
            "lookahead_distance_m": 1.5,
            "obstacle_stop_distance_m": 0.1,
            "auto_loop": True,
        })
        self.assertEqual(self.controller.parameters()["speed_limit_mps"], 0.25)
        # 前视距离 / 障碍停车不再由界面提供，后端保持固定值，不接受请求覆盖。
        self.assertEqual(self.controller.parameters()["lookahead_distance_m"], 2.0)
        self.assertEqual(self.controller.parameters()["obstacle_stop_distance_m"], 0.05)
        with self.assertRaises(ControllerError):
            self.controller._update_parameters({"speed_limit_mps": 2.5, "auto_loop": True})

    def test_speed_range_is_two_tenths_to_two_meters_per_second(self):
        for value in (0.2, 0.75, 1.0, 2.0):
            self.controller.apply_speed(value)
            self.assertAlmostEqual(self.controller.parameters()["speed_limit_mps"], value)
        for value in (0.19, 0.05, 2.01, 5.0, float("nan")):
            with self.assertRaises(ControllerError):
                self.controller.apply_speed(value)

    def test_set_speed_action_reports_saved_when_car_is_offline(self):
        # simulate=True 的控制器不连接车端，只保存。
        message = self.controller.action("set_speed", {"speed_limit_mps": 1.5})
        self.assertIn("1.50 m/s", message)
        self.assertAlmostEqual(self.controller.parameters()["speed_limit_mps"], 1.5)

    def test_speed_dispatch_is_coalesced_in_the_background(self):
        """连续拖动滑杆只下发最后一次，且请求线程不被 rostopic pub 阻塞。"""
        self.controller.simulate = False
        published = []

        def fake_publish(parameters):
            published.append(parameters["speed_limit_mps"])
            return True

        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_publish_live_parameters", side_effect=fake_publish
        ):
            for value in (0.4, 0.6, 0.8, 1.0):
                self.controller.apply_speed(value)
            time.sleep(0.3)
        self.assertEqual(published, [1.0])
        self.assertAlmostEqual(self.controller.parameters()["speed_limit_mps"], 1.0)

    def test_speed_publish_uses_kmh_and_realtime_tuning(self):
        self.controller.simulate = False
        commands = []

        def fake_ros(command, timeout=8):
            commands.append(command)
            return subprocess.CompletedProcess([], 0, stdout="")

        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_ros", side_effect=fake_ros
        ):
            published = self.controller.apply_speed(1.0)
            # 下发在后台线程做，等它把合并窗口走完。
            time.sleep(0.6)

        self.assertTrue(published["published"])
        replanner = next(command for command in commands if "waypoint_replanner" in command)
        follower = next(command for command in commands if "waypoint_follower" in command)
        # Autoware 的 velocity_max / velocity 单位是 km/h：1.0 m/s => 3.6 km/h。
        self.assertIn("velocity_max: 3.6000", replanner)
        self.assertIn("realtime_tuning_mode: true", replanner)
        self.assertIn("velocity: 3.6000", follower)

    def test_speed_change_rewrites_the_running_loop_route(self):
        (self.data / "loop.csv").write_text(
            "x,y,z,yaw,velocity\n0,0,0,0,1\n1,0,0,0,1\n0.2,0.1,0,0,0\n", encoding="utf-8")
        self.controller._save_selection({"map": "", "route": "loop.csv"})
        self.controller._prepare_loop_route(self.data / "loop.csv", 0.2, laps=2)
        self.controller.apply_speed(1.6)
        rows = (self.controller.runtime / "loop_route.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 7)  # 表头 + 2 圈 × 3 航点
        self.assertTrue(all(row.endswith(",5.7600") for row in rows[1:]))

    def test_battery_state_falls_back_to_the_topic_probe(self):
        self.controller.simulate = False
        payload = (
            "__NODES__\n/yhs_can_control_qt_node\n__TOPICS__\n/ctrl_fb\n__LIVE__\n/ctrl_fb\n"
            "__BATTERY__\ncharge False\ncurrent -1.1\nremaining_ah 18.8\nsoc 88\n"
            "temp_high 26.0\ntemp_low 25.0\nunder False\nvoltage 49.79\n"
        )
        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_ros", return_value=subprocess.CompletedProcess([], 0, stdout=payload)
        ):
            battery = self.controller.battery_state()
        self.assertEqual(battery["soc"], 88)
        self.assertEqual(battery["source"], "probe")
        self.assertFalse(battery["low"])
        self.assertAlmostEqual(battery["voltage"], 49.79)
        # 刚跑过的探针结果直接复用，不再执行 docker exec。
        self.controller._ros_cache = (time.monotonic(), set(), set())
        self.controller._live_cache = (time.monotonic(), set())
        # 探针结果会缓存：第二次读取不再执行 docker exec。
        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_ros", side_effect=AssertionError("不应再次探测")
        ):
            self.assertEqual(self.controller.battery_state()["soc"], 88)

    def test_battery_state_marks_low_voltage(self):
        values = self.controller._normalise_battery({"soc": 15, "under": True, "single_uv": False, "charge": True})
        self.assertTrue(values["low"])
        self.assertTrue(values["charge"])
        self.assertEqual(values["soc"], 15.0)

    def test_battery_state_prefers_the_live_bridge(self):
        self.controller.live.cache["battery"] = {"type": "battery", "data": {"soc": 91, "voltage": 49.5}}
        self.controller.live.cache["chassis"] = {"type": "pose", "data": {"speed": 0.3}}
        battery = self.controller.battery_state()
        self.assertEqual(battery["soc"], 91)
        self.assertEqual(battery["source"], "live")

    def test_snapshot_carries_battery_live_and_speed_blocks(self):
        snapshot = self.controller.snapshot()
        for key in ("battery", "live", "speed"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["speed"]["min_mps"], 0.2)
        self.assertEqual(snapshot["speed"]["max_mps"], 2.0)

    def test_bridge_frames_decode_json_and_cloud(self):
        from controller import decode_bridge_frame
        header = json.dumps({"count": 2, "frame_id": "map", "stamp": 1.5}).encode()
        blob = struct.pack("<6f", 0, 0, 0, 1, 2, 3)
        frame = decode_bridge_frame(7, struct.pack("<I", len(header)) + header + blob)
        self.assertEqual(frame["type"], "cloud")
        self.assertEqual(frame["header"]["frame_id"], "map")
        self.assertEqual(frame["header"]["points"], 2)
        self.assertEqual(len(frame["blob"]), 24)
        self.assertEqual(decode_bridge_frame(3, b'{"soc": 90}')["data"]["soc"], 90)

    def test_live_bridge_parses_frames_and_fans_out(self):
        import io
        import json as jsonlib
        from controller import BRIDGE_HEADER

        def frame(kind, payload):
            return b"BC" + BRIDGE_HEADER.pack(kind, len(payload)) + payload

        wire = (
            frame(1, b'{"role": "bridge"}')
            + frame(3, b'{"soc": 77}')
            + frame(4, b'{"x": 1, "y": 2, "yaw": 0}')
            + b"noise" + frame(9, b'{"scope": "map", "message": "bad pcd"}')
        )
        received = []
        bridge = self.controller.live
        bridge.process = type("Fake", (), {"poll": staticmethod(lambda: None), "stdout": io.BytesIO(wire),
                                           "stdin": None})()
        bridge.clients["token"] = {"subscriber": received.append, "wants": {"pose", "battery"},
                                   "queue": deque(), "wake": threading.Event(), "sent": 0}
        bridge._read_frames()
        self.assertIn("hello", bridge.cache)
        self.assertEqual(bridge.snapshot("battery")["data"]["soc"], 77)
        self.assertEqual(bridge.latest_pose()["x"], 1)
        # 写线程是异步的：直接排空队列来验证「只发订阅的类型」。
        queued = []
        while bridge.clients["token"]["queue"]:
            queued.append(bridge.clients["token"]["queue"].popleft()["type"])
        self.assertEqual(queued, ["battery", "pose"])
        self.assertEqual([item["type"] for item in received], [])
        self.assertIn("bad pcd", bridge.status()["error"])
        bridge.clients.clear()

    def test_live_ticket_is_single_use(self):
        issued = self.controller.issue_live_ticket()
        self.assertTrue(self.controller.consume_live_ticket(issued["ticket"]))
        self.assertFalse(self.controller.consume_live_ticket(issued["ticket"]))

    def test_screen_ticket_is_single_use(self):
        issued = self.controller.issue_screen_ticket()
        self.assertIsNotNone(self.controller.consume_screen_ticket(issued["ticket"]))
        self.assertIsNone(self.controller.consume_screen_ticket(issued["ticket"]))

    def test_emergency_zero_command_uses_live_topic_type(self):
        self.controller.simulate = False
        commands = []
        with patch.object(self.controller, "_container_running", return_value=True), patch.object(
            self.controller, "_ros", side_effect=lambda command, timeout=8: commands.append(command)
        ):
            self.controller._emergency_stop()

        zero_command = next(command for command in commands if "rostopic pub" in command)
        self.assertIn("autoware_msgs/ControlCommandStamped", zero_command)


if __name__ == "__main__":
    unittest.main()
