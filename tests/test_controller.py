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
        live = {"/points_raw", "/current_pose", "/final_waypoints"}
        self.controller.simulate = False
        with patch.object(self.controller, "_container_running", return_value=True):
            self.assertEqual(self.controller._stage(nodes, live), 5)
            self.assertEqual(self.controller._stage(nodes, live | {"/ctrl_cmd"}), 6)

    def test_tracking_enables_control_command_output(self):
        self.controller.simulate = False
        started = []
        with patch.object(
            self.controller,
            "_live_topics",
            return_value={"/points_raw", "/current_pose", "/final_waypoints"},
        ), patch.object(
            self.controller, "_start_process", side_effect=lambda name, command: started.append((name, command))
        ), patch.object(self.controller, "_topic_has_message", return_value=True), patch("controller.time.sleep"):
            self.controller._launch_stage("start_tracking", {"safety_confirmed": True})

        pure_pursuit = next(command for name, command in started if name == "pure_pursuit")
        self.assertIn("publishes_for_steering_robot:=true", pure_pursuit)
        self.assertIn("minimum_lookahead_distance:=2.0", pure_pursuit)

    def test_route_uses_five_centimeter_obstacle_stop_distance(self):
        (self.data / "route.csv").write_text("0,0,0,0,1\n1,0,0,0,1\n", encoding="utf-8")
        self.controller.simulate = False
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
