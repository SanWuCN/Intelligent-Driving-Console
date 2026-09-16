#!/usr/bin/env python2
from __future__ import print_function

import sys
import time

import rosgraph
import rospy
from rospy.msg import AnyMsg


def main():
    topics = sys.argv[1:]
    received = set()

    def mark(_message, topic):
        received.add(topic)

    rospy.init_node("bigcar_console_probe", anonymous=True, disable_signals=True)
    master = rosgraph.Master(rospy.get_name())
    system_state = master.getSystemState()
    nodes = set()
    for group in system_state:
        for _resource, providers in group:
            nodes.update(providers)
    nodes = {node for node in nodes if "bigcar_console_probe" not in node}
    published_topics = {name for name, _topic_type in master.getPublishedTopics("")}
    subscribers = [rospy.Subscriber(topic, AnyMsg, mark, callback_args=topic, queue_size=1) for topic in topics]
    deadline = time.time() + 1.3
    while time.time() < deadline and len(received) < len(topics) and not rospy.is_shutdown():
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


if __name__ == "__main__":
    main()
