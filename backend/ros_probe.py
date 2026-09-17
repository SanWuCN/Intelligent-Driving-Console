#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""One-shot ROS probe used by the console backend.

Prints nodes, published topics, which watched topics currently carry data, and a
BMS (battery) snapshot, all from a single short subscription window so the host
gets everything with one `docker exec`.

    python2 ros_probe.py /points_raw /current_pose ...
"""
from __future__ import print_function

import sys
import time
import json

import genpy
import rosgraph
import rospy
from rospy.msg import AnyMsg

# BMS 字段名 -> 上报键名。这里全部用 ASCII，方便宿主机直接解析。
BATTERY_FIELDS = {
    "bms_flag_Infor_soc": "soc",
    "bms_flag_Infor_single_ov": "single_ov",
    "bms_flag_Infor_single_uv": "single_uv",
    "bms_flag_Infor_ov": "over",
    "bms_flag_Infor_uv": "under",
    "bms_flag_Infor_charge_flag": "charge",
    "bms_flag_Infor_hight_temperature": "temp_high",
    "bms_flag_Infor_low_temperature": "temp_low",
    "bms_Infor_voltage": "voltage",
    "bms_Infor_current": "current",
    "bms_Infor_remaining_capacity": "remaining_ah",
}
BATTERY_TOPICS = ("/bms_flag_Infor_fb", "/bms_Infor_fb")


class MessageReader(object):
    """AnyMsg -> 真实消息对象。

    rospy 的 AnyMsg 只有原始字节，str() 会直接抛异常（没有 _slot_types）。
    因此先从发布列表查出类型名，再用 roslib 取得该包的消息类来反序列化。
    """

    def __init__(self):
        self.cache = {}

    def resolve(self, topic):
        try:
            for name, topic_type in rospy.get_published_topics():
                if name == topic:
                    return topic_type
        except Exception:
            return None
        return None

    def parse(self, topic, message):
        entry = self.cache.get(topic)
        if entry is None:
            topic_type = self.resolve(topic)
            if not topic_type or topic_type == "*":
                self.cache[topic] = False
                return None
            try:
                from roslib.message import get_message_class
                klass = get_message_class(topic_type)
            except Exception:
                klass = None
            if klass is None:
                self.cache[topic] = False
                return None
            entry = (klass, klass())
            self.cache[topic] = entry
        if entry is False:
            return None
        klass, holder = entry
        try:
            klass.deserialize(holder, message._buff)
        except Exception:
            return None
        return holder


def main():
    topics = sys.argv[1:]
    received = set()
    battery = {}
    guard = {}

    def read_guard(message):
        try:
            value = json.loads(message.data)
            if isinstance(value, dict):
                guard.update(value)
        except (ValueError, TypeError):
            pass

    def mark(_message, topic):
        received.add(topic)

    rospy.init_node("bigcar_console_probe", anonymous=True, disable_signals=True)
    master = rosgraph.Master(rospy.get_name())
    reader = MessageReader()

    def read_battery(message, topic):
        received.add(topic)
        parsed = reader.parse(topic, message)
        if parsed is None:
            return
        for name in BATTERY_FIELDS:
            try:
                battery[BATTERY_FIELDS[name]] = getattr(parsed, name)
            except AttributeError:
                continue
    system_state = master.getSystemState()
    nodes = set()
    for group in system_state:
        for _resource, providers in group:
            nodes.update(providers)
    nodes = {node for node in nodes if "bigcar_console_probe" not in node}
    published_topics = {name for name, _topic_type in master.getPublishedTopics("")}

    subscribers = [rospy.Subscriber(topic, AnyMsg, mark, callback_args=topic, queue_size=1) for topic in topics]
    subscribers += [rospy.Subscriber(topic, AnyMsg, read_battery, callback_args=topic, queue_size=1)
                    for topic in BATTERY_TOPICS]
    from std_msgs.msg import String
    subscribers.append(rospy.Subscriber('/person_guard/control_status', String, read_guard, queue_size=1))
    deadline = time.time() + 1.4
    while time.time() < deadline and not rospy.is_shutdown():
        if len(received) >= len(topics) + len(BATTERY_TOPICS) and guard:
            break
        time.sleep(0.03)
    for subscriber in subscribers:
        subscriber.unregister()

    print("__NODES__")
    for node in sorted(nodes):
        print(node)
    print("__TOPICS__")
    for topic in sorted(published_topics):
        print(topic)
    print("__LIVE__")
    for topic in topics:
        if topic in received:
            print(topic)
    print("__BATTERY__")
    for key in sorted(battery):
        print("%s %r" % (key, battery[key]))
    print("__GUARD__")
    print(json.dumps(guard))


if __name__ == "__main__":
    main()
