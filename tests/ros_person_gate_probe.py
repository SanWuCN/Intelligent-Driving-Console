"""Run with ROS Python 2 on vehicle; all command outputs are isolated test topics."""
from __future__ import print_function
import json
import subprocess
import time

import rospy
from autoware_msgs.msg import ControlCommandStamped
from std_msgs.msg import String
from std_srvs.srv import SetBool

rospy.init_node("person_gate_validation", anonymous=True)
prefix = "/person_guard_validation"
pub = rospy.Publisher(prefix + "/input", ControlCommandStamped, queue_size=1)
status = rospy.Publisher(prefix + "/status", String, queue_size=1)
seen = []
rospy.Subscriber(prefix + "/output", ControlCommandStamped,
                 lambda m: seen.append(m.cmd.linear_velocity))
proc = subprocess.Popen([
    "python2", "/from_host/bigcar-console/backend/person_command_gate.py",
    "__name:=person_gate_test", "_require_arm:=true",
    "_input:=" + prefix + "/input", "_output:=" + prefix + "/output",
    "_status:=" + prefix + "/status",
    "/person_guard/arm:=" + prefix + "/arm",
    "/person_guard/check:=" + prefix + "/check",
    "/person_guard/control_status:=" + prefix + "/control_status"])


def phase(stop, duration, send_status=True, send_command=True, target=.2):
    seen[:] = []
    end = time.time() + duration
    while time.time() < end:
        msg = ControlCommandStamped()
        msg.cmd.linear_velocity = target
        if send_command:
            pub.publish(msg)
        if send_status:
            status.publish(String(data=json.dumps({
                "stop": stop, "calibrated": True, "frame_time": time.time()})))
        time.sleep(.05)
    assert len(seen) >= 5, "no output"
    return seen[-5:]


try:
    rospy.wait_for_service(prefix + "/arm", timeout=10)
    arm = rospy.ServiceProxy(prefix + "/arm", SetBool)
    assert max(phase(False, .7)) == 0, "must start disarmed"
    assert arm(True).success
    assert min(phase(False, 2.5)) > .15, "resume ramp"
    assert min(phase(False, 4, target=.5)) > .45, "must follow speed above old .2 cap"
    assert max(phase(True, .6)) == 0, "person stop"
    assert min(phase(False, 2.5)) > .15, "person release"
    assert arm(False).success
    assert max(phase(False, .7)) == 0, "manual stop must not auto release"
    assert arm(True).success
    phase(False, 2.5)
    assert max(phase(False, 1, send_status=False)) == 0, "detector timeout"
    assert max(phase(False, .8, send_command=False)) == 0, "command timeout"
    print("PASS: startup lock, resume ramp, person stop/release, manual stop lock, detector and command timeout")
finally:
    proc.terminate()
    proc.wait()
