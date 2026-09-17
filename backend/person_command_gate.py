#!/usr/bin/env python2
"""Single-output command gate. Default topics are isolated from the chassis."""
import copy
import json
import threading
import time

import rospy
import rosgraph
from autoware_msgs.msg import ControlCommandStamped
from std_msgs.msg import String
from std_srvs.srv import SetBool, SetBoolResponse, Trigger, TriggerResponse


class CommandGate(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.command = None
        self.command_time = 0
        self.status_time = 0
        self.status = {}
        self.last_velocity = 0.0
        self.require_arm = rospy.get_param("~require_arm", False)
        self.armed = not self.require_arm
        self.input_topic = rospy.get_param("~input", "/person_guard/test_ctrl_input")
        self.output_topic = rospy.get_param("~output", "/person_guard/test_ctrl_cmd")
        self.status_topic = rospy.get_param("~status", "/person_guard/status")
        # Live chassis output must never trust leftover test-topic parameters.
        if self.output_topic == "/ctrl_cmd":
            self.status_topic = "/person_guard/status"
            self.require_arm = True
            self.armed = False
        self.pub = rospy.Publisher(self.output_topic,
                                   ControlCommandStamped, queue_size=1)
        rospy.Subscriber(self.input_topic,
                         ControlCommandStamped, self.on_command, queue_size=1)
        rospy.Subscriber(self.status_topic,
                         String, self.on_status, queue_size=1)
        rospy.on_shutdown(self.zero)
        rospy.Service("/person_guard/arm", SetBool, self.arm)
        rospy.Service("/person_guard/check", Trigger, self.check)
        self.control_status = rospy.Publisher("/person_guard/control_status", String, queue_size=1)

    def check(self, request):
        with self.lock:
            reason = self.readiness_error()
        return TriggerResponse(not reason, reason or "ready")

    def readiness_error(self):
        now = time.time()
        frame_time = self.status.get("frame_time", 0)
        if not self.status.get("calibrated"):
            return "camera not calibrated: " + str(self.status.get("error", self.status.get("reason")))
        if not isinstance(frame_time, (int, float)) or not 0 <= now - frame_time <= .6:
            return "camera frame stale or invalid: frame_time=%r now=%.3f" % (frame_time, now)
        if not 0 <= now - self.status_time <= .3:
            return "camera status stale: age=%.3f" % (now - self.status_time)
        if self.output_topic == "/ctrl_cmd":
            try:
                publishers = dict(rosgraph.Master(rospy.get_name()).getSystemState()[0])
                outputs = publishers.get("/ctrl_cmd", [])
                if outputs != [rospy.get_name()]:
                    return "unexpected chassis command publisher: " + repr(outputs)
                inputs = publishers.get(self.input_topic, [])
                if not inputs or set(inputs) - {"/twist_filter", "/twist_gate"}:
                    return "tracking remap not ready: " + repr(inputs)
            except Exception as exc:
                return "ROS graph unavailable: " + str(exc)
        return None

    def arm(self, request):
        with self.lock:
            if not request.data:
                self.armed = False
                self.command = None
                self.last_velocity = 0
                return SetBoolResponse(True, "disarmed")
            reason = self.readiness_error()
            if reason:
                rospy.logwarn("Person guard arm rejected: %s", reason)
                return SetBoolResponse(False, reason)
            self.armed = True
            return SetBoolResponse(True, "armed; person stop remains effective")

    def on_command(self, msg):
        with self.lock:
            self.command, self.command_time = msg, time.time()

    def on_status(self, msg):
        try:
            value = json.loads(msg.data)
            if not isinstance(value, dict):
                raise ValueError("invalid status")
        except (ValueError, TypeError):
            value = {}
        with self.lock:
            self.status, self.status_time = value, time.time()

    def zero(self):
        self.pub.publish(ControlCommandStamped())

    def run(self):
        rate = rospy.Rate(20)
        while not rospy.is_shutdown():
            now = time.time()
            with self.lock:
                out = copy.deepcopy(self.command) if self.command else ControlCommandStamped()
                frame_time = self.status.get("frame_time", 0)
                allowed = (self.armed and self.command is not None and
                           0 <= now - self.command_time <= .3 and
                           0 <= now - self.status_time <= .3 and
                           isinstance(frame_time, (int, float)) and
                           0 <= now - frame_time <= .6 and
                           self.status.get("calibrated") is True and
                           self.status.get("stop") is False)
            if not allowed:
                out.cmd.linear_velocity = 0
                out.cmd.linear_acceleration = 0
                self.last_velocity = 0
            else:
                # Ramp restart at <= 0.1 m/s^2; never manufacture upstream motion.
                target = out.cmd.linear_velocity
                if not 0 <= target <= 2.0:
                    # Front-facing camera cannot validate reversing.
                    target = 0
                self.last_velocity = min(target, self.last_velocity + .005)
                out.cmd.linear_velocity = self.last_velocity
            out.header.stamp = rospy.Time.now()
            self.pub.publish(out)
            self.control_status.publish(String(data=json.dumps({
                "armed": self.armed, "blocked": not allowed,
                "status_topic": self.status_topic,
                "output_velocity_mps": out.cmd.linear_velocity,
                "speed_cap_mps": 2.0, "output_topic": self.output_topic})))
            rate.sleep()


if __name__ == "__main__":
    rospy.init_node("person_command_gate")
    CommandGate().run()
