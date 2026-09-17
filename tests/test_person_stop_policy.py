import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from person_stop_policy import PersonStopPolicy


class PersonStopTests(unittest.TestCase):
    def clear(self, policy, start=0):
        for index in range(22):
            now = start + index * 0.1
            policy.update([], now, now)
        return now

    def test_stop_and_clear_hysteresis(self):
        p = PersonStopPolicy()
        t = self.clear(p)
        self.assertFalse(p.status(t)["stop"])
        self.assertTrue(p.update([1.49], t + .1, t + .1)["stop"])
        for index in range(30):
            t += .1
            self.assertTrue(p.update([1.7], t, t)["stop"])
        self.assertFalse(p.status(self.clear(p, t + .1))["stop"])

    def test_unknown_depth_and_timeout_stop(self):
        p = PersonStopPolicy()
        t = self.clear(p)
        self.assertTrue(p.update([None], t + .1, t + .1)["stop"])
        t = self.clear(p, t + .2)
        self.assertTrue(p.status(t + .7)["stop"])

    def test_missing_frames_cannot_count_as_clear(self):
        p = PersonStopPolicy()
        p.update([], 1, 1)
        self.assertTrue(p.update([], 5, 5)["stop"])
        self.assertTrue(p.update([], 5, 5.2)["stop"])
        self.assertTrue(p.update([], 4, 5.2)["stop"])

    def test_stale_inference_does_not_release(self):
        p = PersonStopPolicy()
        for index in range(30):
            t = index * .1
            self.assertTrue(p.update([], t, t + .8)["stop"])


if __name__ == "__main__":
    unittest.main()
