"""Pure route projection and convoy spacing policy; never commands ROS."""
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
        closure_gap = distance(clean[-1], clean[0])
        if closure_gap > 2:
            raise ValueError('route is not a closed loop (endpoint gap > 2m)')
        # Autoware CSV files commonly repeat the first row at the end.  Keep a
        # single copy so the explicit wraparound segment below is well formed.
        if closure_gap <= .01:
            clean.pop()
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


class SpacingState:
    """State machine for one rear/front pair on the same closed route.

    A gap below 2 m starts a mandatory five-second hold.  Once the hold has
    elapsed the pair stays in spacing mode until its along-route gap is above
    5 m.  Keeping this as an explicit state prevents a noisy 2 m sample from
    repeatedly restarting the five-second timer.
    """

    def __init__(self, stop_gap=2.0, release_gap=5.0, hold_seconds=5.0):
        self.stop_gap = float(stop_gap)
        self.release_gap = float(release_gap)
        self.hold_seconds = float(hold_seconds)
        self.phase = 'clear'
        self.triggered_at = None
        self.event = 0

    def update(self, gap, now):
        gap = float(gap)
        now = float(now)
        if not math.isfinite(gap) or gap < 0:
            raise ValueError('invalid route gap')

        if self.phase == 'clear' and gap < self.stop_gap:
            self.phase = 'hold'
            self.triggered_at = now
            self.event += 1

        if self.phase == 'hold' and now - self.triggered_at >= self.hold_seconds:
            self.phase = 'spacing'

        if self.phase in ('hold', 'spacing') and gap > self.release_gap:
            # The rear car must still complete the full five-second stop even
            # if the leader opens the gap immediately.
            if self.phase != 'hold' or now - self.triggered_at >= self.hold_seconds:
                self.phase = 'clear'
                self.triggered_at = None

        remaining = 0.0
        if self.phase == 'hold':
            remaining = max(0.0, self.hold_seconds - (now - self.triggered_at))
        return self.phase, remaining, self.event


def ordered_gaps(progress, route_length):
    """Return ``(rear, front, gap)`` pairs for immediate neighbours."""
    if route_length <= 0:
        raise ValueError('route length must be positive')
    ordered = sorted(progress.items(), key=lambda item: item[1])
    if len(ordered) < 2:
        return []
    pairs = []
    for index, (rear, rear_s) in enumerate(ordered):
        front, front_s = ordered[(index + 1) % len(ordered)]
        pairs.append((rear, front, (front_s - rear_s) % route_length))
    return pairs
