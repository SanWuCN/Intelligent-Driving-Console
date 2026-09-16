#!/usr/bin/env python2
from __future__ import print_function

import os
import signal
import subprocess
import sys
import time

import rosnode
import rospy
import tf
from geometry_msgs.msg import PoseWithCovarianceStamped


class LocalizationBootstrap(object):
    def __init__(self):
        self.initial_pose = None
        self.republishing = False
        self.child = None
        self.broadcaster = tf.TransformBroadcaster()
        self.publisher = rospy.Publisher("/initialpose", PoseWithCovarianceStamped, queue_size=1)
        rospy.Subscriber("/initialpose", PoseWithCovarianceStamped, self.capture_pose, queue_size=1)
        rospy.on_shutdown(self.stop_child)

    def capture_pose(self, message):
        if not self.republishing and self.initial_pose is None:
            self.initial_pose = message
            rospy.loginfo("Manual 2D Pose Estimate captured; starting NDT")

    def wait_for_manual_pose(self):
        rate = rospy.Rate(20)
        while not rospy.is_shutdown() and self.initial_pose is None:
            self.broadcaster.sendTransform(
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
                rospy.Time.now(),
                "base_link",
                "map",
            )
            rate.sleep()

    def start_ndt(self):
        command = ["roslaunch", "lidar_localizer", "ndt_matching.launch", "use_gnss:=0"]
        self.child = subprocess.Popen(command, stdout=sys.stdout, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
        deadline = time.time() + 12.0
        while time.time() < deadline and not rospy.is_shutdown():
            try:
                if "/ndt_matching" in rosnode.get_node_names():
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("NDT node did not become ready")

        self.republishing = True
        for _index in range(8):
            self.initial_pose.header.stamp = rospy.Time.now()
            self.publisher.publish(self.initial_pose)
            time.sleep(0.2)
        self.republishing = False
        rospy.loginfo("Initial pose delivered to NDT")

    def stop_child(self):
        if self.child is None or self.child.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.child.pid), signal.SIGINT)
            self.child.wait()
        except Exception:
            pass

    def run(self):
        self.wait_for_manual_pose()
        if rospy.is_shutdown() or self.initial_pose is None:
            return
        self.start_ndt()
        while not rospy.is_shutdown() and self.child.poll() is None:
            time.sleep(0.2)


if __name__ == "__main__":
    rospy.init_node("localization_bootstrap")
    LocalizationBootstrap().run()
