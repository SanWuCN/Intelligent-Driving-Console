"""Fail-closed state machine; times are monotonic seconds, distances metres."""
import math


def finite(value):
    return not math.isnan(value) and not math.isinf(value)


class PersonStopPolicy(object):
    def __init__(self, stop_distance=1.5, release_distance=1.8,
                 clear_seconds=2.0, stale_seconds=0.6):
        self.stop_distance = stop_distance
        self.release_distance = release_distance
        self.clear_seconds = clear_seconds
        self.stale_seconds = stale_seconds
        self.last_frame = None
        self.clear_since = None
        self.blocked = True
        self.reason = "waiting_for_camera"

    def update(self, distances, frame_time, now, valid=True):
        if (not valid or not finite(frame_time) or
                frame_time > now or now - frame_time > self.stale_seconds):
            self.clear_since = None
            self.blocked, self.reason = True, "invalid_or_stale_frame"
            return self.status(now)
        if self.last_frame is not None and frame_time <= self.last_frame:
            return self.status(now)
        if self.last_frame is None or frame_time - self.last_frame > self.stale_seconds:
            self.clear_since = None
            self.blocked = True
        self.last_frame = frame_time
        if any(d is None or not finite(d) or d <= 0 for d in distances):
            self.clear_since = None
            self.blocked, self.reason = True, "person_depth_unknown"
        elif any(d < self.stop_distance for d in distances):
            self.clear_since = None
            self.blocked, self.reason = True, "person_near"
        elif self.blocked:
            if any(d < self.release_distance for d in distances):
                self.clear_since = None
                self.reason = "person_in_release_margin"
            else:
                if self.clear_since is None:
                    self.clear_since = frame_time
                if frame_time - self.clear_since >= self.clear_seconds:
                    self.blocked, self.reason = False, "clear"
                else:
                    self.reason = "confirming_clear"
        return self.status(now)

    def status(self, now):
        if (self.last_frame is None or now < self.last_frame or
                now - self.last_frame > self.stale_seconds):
            self.blocked, self.reason = True, "camera_timeout"
            self.clear_since = None
        return {"stop": self.blocked, "reason": self.reason}
