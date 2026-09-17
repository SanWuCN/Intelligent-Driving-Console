"""Read-only route projection and following recommendations; never commands ROS."""
import csv
import math


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class LoopRoute:
    def __init__(self, points):
        clean = []
        for point in points:
            if not all(math.isfinite(v) for v in point):
                raise ValueError('non-finite route point')
            if not clean or distance(clean[-1], point) > .01:
                clean.append(point)
        if len(clean) < 4:
            raise ValueError('route needs at least four points')
        # A recording may end just beyond its start. Remove that short reversed seam.
        while len(clean) > 4 and distance(clean[-1], clean[0]) < 1:
            last, prior, first = clean[-1], clean[-2], clean[0]
            dot = ((last[0] - prior[0]) * (first[0] - last[0]) +
                   (last[1] - prior[1]) * (first[1] - last[1]))
            if dot > 0:
                break
            clean.pop()
        if distance(clean[-1], clean[0]) > 2:
            raise ValueError('route is not a closed loop (endpoint gap > 2m)')
        self.points, self.segments, self.length = clean, [], 0.0
        for a, b in zip(clean, clean[1:] + clean[:1]):
            length = distance(a, b)
            if length < .01:
                continue
            self.segments.append((a, b, length, self.length))
            self.length += length

    @classmethod
    def load(cls, path):
        with open(path) as stream:
            rows = csv.DictReader(stream)
            return cls([(float(row['x']), float(row['y'])) for row in rows])

    def project(self, x, y, yaw, previous=None, elapsed=0):
        candidates = []
        for a, b, length, start in self.segments:
            dx, dy = b[0] - a[0], b[1] - a[1]
            alignment = (math.cos(yaw) * dx + math.sin(yaw) * dy) / length
            if alignment < .5:
                continue
            t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / length ** 2))
            error = distance((x, y), (a[0] + t * dx, a[1] + t * dy))
            s = (start + t * length) % self.length
            if error > 1.5:
                continue
            if previous is not None:
                change = min((s - previous) % self.length, (previous - s) % self.length)
                if change > 2.5 * elapsed + 1.5:
                    continue
            candidates.append((error, s))
        if not candidates:
            raise ValueError('off_route_or_heading')
        candidates.sort()
        error, s = candidates[0]
        for other_error, other_s in candidates[1:]:
            apart = min((s - other_s) % self.length, (other_s - s) % self.length)
            if other_error - error < .25 and apart > 3:
                raise ValueError('ambiguous_route_segment')
        return s, error


class FollowingAdvice:
    def __init__(self, hold_seconds=0):
        self.hold_seconds = hold_seconds
        self.stopped_at = None
        self.clear_since = None

    def update(self, reference_gap, velocity, now):
        # Reference-point distance includes a provisional 2m vehicle-length margin.
        stop_gap = 5 + velocity + velocity * velocity
        release_gap = stop_gap + 2
        if reference_gap <= stop_gap:
            if self.stopped_at is None:
                self.stopped_at = now
            self.clear_since = None
        elif self.stopped_at is not None:
            if reference_gap < release_gap:
                self.clear_since = None
            elif self.clear_since is None:
                self.clear_since = now
            elif now - self.clear_since >= 2 and now - self.stopped_at >= self.hold_seconds:
                self.stopped_at = self.clear_since = None
        if self.stopped_at is not None:
            return 'stop', 0.0, stop_gap
        if reference_gap < release_gap + 1:
            return 'slow', min(2, max(0, (reference_gap - stop_gap) / 3)), stop_gap
        return 'clear', 2.0, stop_gap
