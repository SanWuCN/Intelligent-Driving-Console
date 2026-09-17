import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


class GateConfigurationTests(unittest.TestCase):
    def test_live_output_ignores_stale_test_status_and_requires_arm(self):
        params = {"~output": "/ctrl_cmd", "~status": "/person_guard/test_status",
                  "~require_arm": False}
        rospy = MagicMock()
        rospy.get_param.side_effect = lambda name, default: params.get(name, default)
        modules = {"rospy": rospy, "rosgraph": MagicMock()}
        for name, fields in {
            "autoware_msgs.msg": ["ControlCommandStamped"],
            "std_msgs.msg": ["String"],
            "std_srvs.srv": ["SetBool", "SetBoolResponse", "Trigger", "TriggerResponse"],
        }.items():
            module = types.ModuleType(name)
            for field in fields:
                setattr(module, field, MagicMock())
            modules[name] = module
        path = Path(__file__).resolve().parents[1] / "backend/person_command_gate.py"
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("gate_config_test", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            gate = module.CommandGate()
        self.assertTrue(gate.require_arm)
        self.assertFalse(gate.armed)
        self.assertEqual(gate.status_topic, "/person_guard/status")
        topics = [call.args[0] for call in rospy.Subscriber.call_args_list]
        self.assertIn("/person_guard/status", topics)
        self.assertNotIn("/person_guard/test_status", topics)


if __name__ == "__main__":
    unittest.main()
