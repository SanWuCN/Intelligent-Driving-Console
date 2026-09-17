#!/usr/bin/env python2
"""RGB-D person monitor. Publishes diagnostics only; never vehicle commands."""
from __future__ import print_function

import json
import threading
import time
import urllib2

import cv2
import message_filters
import numpy as np
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image
from std_msgs.msg import String

from person_stop_policy import PersonStopPolicy


class Observer(object):
    def __init__(self):
        self.bridge = CvBridge()
        self.policy = PersonStopPolicy()
        self.lock = threading.Lock()
        self.pending = None
        self.result = {"stop": True, "reason": "waiting_for_camera"}
        self.offset = rospy.get_param("~camera_to_front_m", None)
        self.alignment_verified = rospy.get_param("~alignment_verified", False)
        self.pub = rospy.Publisher("/person_guard/status", String, queue_size=1)
        color = message_filters.Subscriber("/person_camera/color/image_raw", Image)
        depth = message_filters.Subscriber("/person_camera/depth/image_raw", Image)
        self.sync = message_filters.ApproximateTimeSynchronizer([color, depth], 3, 0.08)
        self.sync.registerCallback(self.capture)
        self.worker = threading.Thread(target=self.process)
        self.worker.daemon = True
        self.worker.start()

    def capture(self, color, depth):
        with self.lock:
            self.pending = (color, depth, time.time())

    def process(self):
        while not rospy.is_shutdown():
            with self.lock:
                item, self.pending = self.pending, None
            if item is None:
                time.sleep(.02)
                continue
            color, depth, received = item
            try:
                age = rospy.Time.now().to_sec() - min(color.header.stamp.to_sec(), depth.header.stamp.to_sec())
                if age < 0 or age > .4:
                    raise ValueError("stale_camera_frame")
                frame_time = received - age
                rgb = self.bridge.imgmsg_to_cv2(color, "bgr8")
                values = self.bridge.imgmsg_to_cv2(depth, "passthrough").astype(np.float32)
                if depth.encoding == "16UC1":
                    values *= .001
                elif depth.encoding != "32FC1":
                    raise ValueError("unsupported_depth_encoding")
                if values.shape != rgb.shape[:2]:
                    raise ValueError("depth_not_aligned")
                if color.header.frame_id != depth.header.frame_id:
                    raise ValueError("depth_frame_mismatch")
                ok, encoded = cv2.imencode(".jpg", rgb, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise ValueError("image_encoding_failed")
                request = urllib2.Request("http://127.0.0.1:8771/detect", encoded.tostring(),
                                          {"Content-Type": "image/jpeg"})
                response = urllib2.urlopen(request, timeout=.5)
                try:
                    persons = json.loads(response.read())["persons"]
                finally:
                    response.close()
                distances = []
                h, w = values.shape
                for person in persons:
                    x1, y1, x2, y2 = person["box"]
                    # Torso inset reduces background leakage; reject missing depth.
                    xa, xb = int((x1 + .25 * (x2-x1))*w), int((x2 - .25*(x2-x1))*w)
                    ya, yb = int((y1 + .2 * (y2-y1))*h), int((y1 + .65*(y2-y1))*h)
                    roi = values[max(0,ya):min(h,yb), max(0,xa):min(w,xb)]
                    valid = roi[np.isfinite(roi) & (roi > .15) & (roi < 10)]
                    distance = None
                    if roi.size and valid.size >= max(20, roi.size * .3):
                        distance = float(np.percentile(valid, 20)) - float(self.offset or 0)
                    person["distance_m"] = distance
                    distances.append(distance)
                with self.lock:
                    valid_config = self.alignment_verified and self.offset is not None
                    status = self.policy.update(distances, frame_time, time.time())
                    if not valid_config:
                        status = {"stop": True, "reason": "calibration_required"}
                    self.result = dict(status, persons=persons, mode="observe_only",
                                       calibrated=bool(valid_config), frame_time=frame_time)
            except Exception as exc:
                with self.lock:
                    self.policy.update([], received, time.time(), valid=False)
                    self.result = {"stop": True, "error": str(exc), "mode": "observe_only"}

    def run(self):
        rate = rospy.Rate(10)
        while not rospy.is_shutdown():
            with self.lock:
                result = dict(self.result)
                result.update(self.policy.status(time.time()))
                if not self.alignment_verified or self.offset is None:
                    result.update(stop=True, reason="calibration_required")
            self.pub.publish(String(data=json.dumps(result)))
            rate.sleep()


if __name__ == "__main__":
    rospy.init_node("person_camera_observer")
    Observer().run()
