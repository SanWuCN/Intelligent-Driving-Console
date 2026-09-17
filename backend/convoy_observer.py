#!/usr/bin/env python2
"""ROS subscriber only. Best-effort telemetry; no publishers or control services."""
from __future__ import print_function
import hashlib
import json
import math
import os
import threading
import time
import urllib2
import uuid

import rospy
from geometry_msgs.msg import PoseStamped
from yhs_can_msgs.msg import ctrl_fb


class Observer(object):
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.pose = None
        self.speed = None
        self.pose_received = self.speed_received = 0
        self.session, self.seq = uuid.uuid4().hex, 0
        self.hashes = {}
        self.last_metadata = 0
        self.metadata = {}
        rospy.Subscriber('/current_pose', PoseStamped, self.on_pose, queue_size=1)
        rospy.Subscriber('/ctrl_fb', ctrl_fb, self.on_speed, queue_size=1)

    def on_pose(self, msg):
        q = msg.pose.orientation
        yaw = math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2 * (q.y*q.y + q.z*q.z))
        age = rospy.Time.now().to_sec() - msg.header.stamp.to_sec()
        with self.lock:
            self.pose = dict(x=msg.pose.position.x, y=msg.pose.position.y, yaw=yaw,
                             frame=msg.header.frame_id.lstrip('/'), source_age=age)
            self.pose_received = time.time()

    def on_speed(self, msg):
        with self.lock:
            self.speed = float(msg.ctrl_fb_velocity)
            self.speed_received = time.time()

    def digest(self, path):
        stat = os.stat(path)
        key = (path, stat.st_mtime, stat.st_size)
        if key not in self.hashes:
            result = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    result.update(chunk)
            self.hashes = dict((k, v) for k, v in self.hashes.items() if k[0] != path)
            self.hashes[key] = result.hexdigest()
        return self.hashes[key]

    def payload(self):
        now = time.time()
        if now - self.last_metadata > 5:
            try:
                with open('/from_host/bigcar-console/runtime/config.json') as stream:
                    settings = json.load(stream)
                names = {key: settings.get('selected_' + key, '') for key in ('map', 'route')}
                if any(not name or os.path.basename(name) != name for name in names.values()):
                    raise ValueError('no selected map/route')
                self.metadata = {key + '_hash': self.digest('/from_host/' + name)
                                 for key, name in names.items()}
                self.metadata.update(names)
            except Exception as exc:
                self.metadata = {'metadata_error': str(exc)}
            self.last_metadata = now
        with self.lock:
            pose = dict(self.pose or {})
            pose_age = now - self.pose_received
            pose['pose_age'] = max(pose_age, pose.get('source_age', 999) + pose_age)
            pose['speed_age'] = now - self.speed_received
            pose['speed'] = self.speed
        self.seq += 1
        pose.update(self.metadata)
        pose.update(vehicle=self.config['vehicle'], session=self.session, seq=self.seq)
        return pose

    def run(self):
        while not rospy.is_shutdown():
            try:
                request = urllib2.Request(self.config['url'] + '/api/telemetry',
                    json.dumps(self.payload()).encode('utf-8'),
                    {'Content-Type': 'application/json', 'X-Observer-Token': self.config['token']})
                response = urllib2.urlopen(request, timeout=.5)
                response.read(2048)
                response.close()
            except Exception as exc:
                rospy.logwarn_throttle(30, 'Convoy observation unavailable (driving unaffected): %s', exc)
            time.sleep(.1)


if __name__ == '__main__':
    rospy.init_node('bigcar_convoy_observer', anonymous=True)
    with open('/from_host/bigcar-console/runtime/convoy-observer.json') as stream:
        Observer(json.load(stream)).run()
