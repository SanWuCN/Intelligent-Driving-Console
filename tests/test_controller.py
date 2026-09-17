import tempfile
import unittest
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
        waypoint_loader = next(command for name, command in started if name == "waypoint_loader")
        self.assertIn("velocity_max:=0.72", waypoint_loader)
        self.assertIn("replanning_mode:=true", waypoint_loader)

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
        with self.assertRaises(ControllerError):
            self.controller._update_parameters({
                "speed_limit_mps": 2,
                "lookahead_distance_m": 1.5,
                "obstacle_stop_distance_m": 0.1,
                "auto_loop": True,
            })

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
