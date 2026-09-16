#!/usr/bin/env python2
# -*- coding: utf-8 -*-
from __future__ import print_function

import gettext
import sys

import rosgraph
import wx


RUNTIME_MANAGER_LIB = "/root/autoware_1.14.0/install/runtime_manager/lib/runtime_manager"
if RUNTIME_MANAGER_LIB not in sys.path:
    sys.path.insert(0, RUNTIME_MANAGER_LIB)

import runtime_manager_dialog as runtime_manager  # noqa: E402


COMPONENT_NODES = {
    "setup_tf": ("base_link_to_localizer",),
    "vehicle_model": ("robot_state_publisher",),
    "point_cloud": ("points_map_loader",),
    "tf": ("world_to_map",),
    "voxel_grid_filter": ("voxel_grid_filter",),
    "ring_ground_filter": ("ring_ground_filter",),
    "ndt_matching": ("ndt_matching",),
    "vel_pose_connect": ("pose_relay", "vel_relay"),
    "waypoint_loader": ("waypoint_loader",),
    "lane_rule": ("lane_rule",),
    "lane_stop": ("lane_stop",),
    "lane_select": ("lane_select",),
    "astar_avoid": ("astar_avoid",),
    "velocity_set": ("velocity_set",),
    "pure_pursuit": ("pure_pursuit",),
    "twist_filter": ("twist_filter",),
}


OriginalFrame = runtime_manager.MyFrame


class ManagedFrame(OriginalFrame):
    def __init__(self, *args, **kwargs):
        OriginalFrame.__init__(self, *args, **kwargs)
        self.SetTitle(u"Runtime Manager — 智能驾驶控制台托管")
        self.console_component_state = {}
        self.console_state_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.sync_console_state, self.console_state_timer)
        self.console_state_timer.Start(1000)
        wx.CallAfter(self.sync_console_state, None)

    @staticmethod
    def active_nodes():
        master = rosgraph.Master("/bigcar_console_runtime_manager")
        nodes = set()
        for group in master.getSystemState():
            for _resource, providers in group:
                nodes.update(name.lstrip("/") for name in providers)
        return nodes

    def sync_console_state(self, _event):
        try:
            active = self.active_nodes()
        except Exception:
            return
        for component, required_nodes in COMPONENT_NODES.items():
            config = self.cfg_dic({"name": component})
            control = config.get("obj") if config else None
            if control is None:
                if self.console_component_state.get(component) != "missing":
                    print("[console-managed] {} control not found".format(component))
                    self.console_component_state[component] = "missing"
                continue
            running = all(node in active for node in required_nodes)
            current = bool(control.GetValue()) if hasattr(control, "GetValue") else False
            if current != running:
                runtime_manager.set_val(control, running)
            if self.console_component_state.get(component) != running:
                print("[console-managed] {}={}".format(component, "running" if running else "stopped"))
                self.console_component_state[component] = running


if __name__ == "__main__":
    gettext.install("app")
    runtime_manager.MyFrame = ManagedFrame
    app = runtime_manager.MyApp(0)
    app.MainLoop()
