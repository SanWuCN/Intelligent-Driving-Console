import copy
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from convoy_coordinator import ConvoyCoordinator
from convoy_policy import LoopRoute, SpacingState, ordered_gaps


class ConvoyPolicyTests(unittest.TestCase):
    def test_exact_thresholds_hold_and_release(self):
        state = SpacingState()
        self.assertEqual(state.update(2.0, 0)[0], 'clear')
        phase, remaining, event = state.update(1.99, 1)
        self.assertEqual((phase, event), ('hold', 1))
        self.assertAlmostEqual(remaining, 5.0)
        self.assertEqual(state.update(6.0, 4)[0], 'hold', 'five-second stop cannot release early')
        self.assertEqual(state.update(5.0, 6)[0], 'spacing')
        self.assertEqual(state.update(5.01, 7)[0], 'clear')

    def test_closed_route_gaps_include_the_wraparound_pair(self):
        pairs = ordered_gaps({'a': 1.0, 'b': 4.0, 'c': 9.0}, 10.0)
        self.assertEqual(pairs, [('a', 'b', 3.0), ('b', 'c', 5.0), ('c', 'a', 2.0)])

    def test_route_projection_uses_heading_and_progress(self):
        route = LoopRoute([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        progress, error = route.project(3, 0.2, 0)
        self.assertAlmostEqual(progress, 3.0, places=1)
        self.assertAlmostEqual(error, 0.2, places=1)


class FakeFleet:
    def __init__(self):
        self.lock = threading.RLock()
        self.vehicles = {
            'a': {'id': 'a', 'name': '01', 'ip': '10.0.0.1', 'port': 8765, 'token': 'x'},
            'b': {'id': 'b', 'name': '02', 'ip': '10.0.0.2', 'port': 8765, 'token': 'x'},
        }
        self.states = {identifier: {'online': True, 'state': {'current_stage': 6, 'emergency': False,
                                                              'parameters': {'speed_limit_mps': 1.0}}}
                       for identifier in self.vehicles}
        self.jobs = [{'id': 'job', 'rows': [
            {'vehicle_id': 'a', 'status': 'completed', 'route': 'loop.csv'},
            {'vehicle_id': 'b', 'status': 'completed', 'route': 'loop.csv'},
        ]}]
        self.telemetry = {
            'a': self.payload(1.0, 0.0, 0.0),
            'b': self.payload(2.5, 0.0, 0.0),
        }
        self.commands = {}
        self.route = {'points': [{'x': x, 'y': y} for x, y in
                                 [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]}

    @staticmethod
    def payload(x, y, yaw):
        return {
            'selected_map': 'room.pcd', 'selected_route': 'loop.csv', 'route_hash': 'same-hash',
            'pose': {'x': x, 'y': y, 'yaw': yaw, 'pose_age': 0.1, 'frame': 'map'},
            'control': {'nominal_speed_mps': 1.0},
        }

    def vehicle(self, identifier):
        return dict(self.vehicles[identifier])

    def request(self, vehicle, path='/api/state', body=None, timeout=12):
        identifier = vehicle['id']
        if path == '/api/convoy':
            return copy.deepcopy(self.telemetry[identifier])
        if path.startswith('/api/route'):
            return copy.deepcopy(self.route)
        if path == '/api/action':
            self.commands[identifier] = dict(body)
            return {'ok': True}
        raise AssertionError(path)


class ConvoyCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.fleet = FakeFleet()
        self.coordinator = ConvoyCoordinator(self.fleet)

    def test_close_pair_stops_rear_before_speed_adjustments(self):
        self.coordinator.tick()
        self.assertEqual(self.fleet.commands['a']['mode'], 'hold')
        self.assertEqual(self.fleet.commands['a']['speed_mps'], 0.0)
        self.assertEqual(self.fleet.commands['a']['after_speed_mps'], 0.7)
        self.assertEqual(self.fleet.commands['b']['mode'], 'clear')
        status = self.coordinator.snapshot()
        self.assertTrue(status['active'])
        self.assertAlmostEqual(status['vehicles']['a']['gap_m'], 1.5)

    def test_after_five_seconds_rear_slows_until_gap_above_five(self):
        self.coordinator.tick()
        pair = next(iter(self.coordinator.pairs.values()))
        pair.triggered_at -= 5.1
        self.coordinator.tick()
        self.assertEqual(self.fleet.commands['a']['mode'], 'slow')
        self.assertAlmostEqual(self.fleet.commands['a']['speed_mps'], 0.7)
        self.assertEqual(self.fleet.commands['b']['mode'], 'boost')
        self.assertAlmostEqual(self.fleet.commands['b']['speed_mps'], 1.1)

        self.fleet.telemetry['b'] = self.fleet.payload(7.0, 0.0, 0.0)
        self.coordinator.projections.clear()
        self.coordinator.tick()
        self.assertEqual(self.fleet.commands['a']['mode'], 'clear')
        self.assertEqual(self.fleet.commands['b']['mode'], 'clear')

    def test_missing_pose_stops_every_reachable_car(self):
        self.fleet.telemetry['a']['pose'] = None
        self.coordinator.tick()
        self.assertEqual(self.fleet.commands['a']['mode'], 'safety_stop')
        self.assertEqual(self.fleet.commands['b']['mode'], 'safety_stop')
        self.assertEqual(self.coordinator.snapshot()['groups'][0]['state'], 'safety_stop')

    def test_route_content_mismatch_stops_both_cars(self):
        self.fleet.telemetry['b']['route_hash'] = 'different-hash'
        self.coordinator.tick()
        self.assertEqual({command['mode'] for command in self.fleet.commands.values()}, {'safety_stop'})

    def test_unreadable_route_stops_both_cars(self):
        self.fleet.route = {'points': []}
        self.coordinator.tick()
        self.assertEqual({command['mode'] for command in self.fleet.commands.values()}, {'safety_stop'})
        self.assertIn('路线数据读取失败', self.coordinator.snapshot()['groups'][0]['error'])

    def test_three_car_chain_never_boosts_a_car_that_must_stop(self):
        self.fleet.vehicles['c'] = {'id': 'c', 'name': '03', 'ip': '10.0.0.3', 'port': 8765, 'token': 'x'}
        self.fleet.states['c'] = {'online': True, 'state': {'current_stage': 6, 'emergency': False,
                                                            'parameters': {'speed_limit_mps': 1.0}}}
        self.fleet.jobs[0]['rows'].append({'vehicle_id': 'c', 'status': 'completed', 'route': 'loop.csv'})
        self.fleet.telemetry['c'] = self.fleet.payload(3.5, 0.0, 0.0)
        self.coordinator.tick()
        self.assertEqual(self.fleet.commands['a']['mode'], 'hold')
        self.assertEqual(self.fleet.commands['b']['mode'], 'hold')
        self.assertEqual(self.fleet.commands['c']['mode'], 'clear')


if __name__ == '__main__':
    unittest.main()
