#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""One-shot pose/speed probe used by the three-second convoy coordinator."""
from __future__ import print_function

import json
import math
import time

import rospy
from geometry_msgs.msg import PoseStamped


def main():
    rospy.init_node('bigcar_convoy_probe', anonymous=True, disable_signals=True)
    pose = rospy.wait_for_message('/current_pose', PoseStamped, timeout=1.2)
    q = pose.pose.orientation
    yaw = math.atan2(2 * (q.w * q.z + q.x * q.y),
                     1 - 2 * (q.y * q.y + q.z * q.z))
    stamp = pose.header.stamp.to_sec()
    now_ros = rospy.Time.now().to_sec()
    payload = {
        'x': float(pose.pose.position.x),
        'y': float(pose.pose.position.y),
        'yaw': float(yaw),
        'frame': pose.header.frame_id.lstrip('/'),
        'pose_age': max(0.0, now_ros - stamp) if stamp > 0 else 0.0,
        'received_at': time.time(),
    }
    print(json.dumps(payload, separators=(',', ':')))


if __name__ == '__main__':
    main()
