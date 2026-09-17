import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fleet import ACTIONS, Fleet, FleetError


class FakeFleet(Fleet):
    def __init__(self, path):
        self.remote = {'current_stage': 0, 'workflow': [], 'telemetry': {}, 'maps': [{'name': 'room.pcd'}], 'routes': [{'name': 'loop.csv'}], 'selected_map': 'room.pcd', 'selected_route': 'loop.csv', 'busy': None, 'last_error': None, 'emergency': False, 'live_topics': [], 'simulated': False}
        self.calls = []
        self.unreachable = False
        super().__init__(path)

    def request(self, vehicle, path='/api/state', body=None):
        if self.unreachable:
            raise FleetError('offline')
        with self.lock:
            if body is not None:
                action = body['action']
                self.calls.append(action)
                if action == 'emergency_stop':
                    self.remote['emergency'] = True
                elif action != 'start_localization':
                    self.remote['current_stage'] = ACTIONS.index(action) + 1
                return {'ok': True}
            return copy.deepcopy(self.remote)


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fleet = FakeFleet(Path(self.directory.name) / 'fleet.json')
        self.fleet.add({'name': 'Training 01', 'ip': '192.168.31.232', 'token': 'secret'})
        self.identifier = next(iter(self.fleet.vehicles))

    def tearDown(self):
        for job in self.fleet.jobs:
            for row in job['rows']:
                if row['status'] not in ('completed', 'failed', 'cancelled', 'interrupted'):
                    self.fleet.job_action(job['id'], {'vehicle_id': row['vehicle_id'], 'action': 'cancel'})
        time.sleep(0.25)
        self.directory.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('timed out waiting for worker')

    def job(self, target=6):
        self.fleet.create_job({'vehicles': [self.identifier], 'target': target})
        return self.fleet.jobs[0], self.fleet.jobs[0]['rows'][0]

    def localize(self, job, row):
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        with self.fleet.lock:
            self.fleet.remote.update(current_stage=4, live_topics=['/current_pose'])
        self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'localization_done'})

    def test_add_requires_connection_and_hides_token(self):
        self.assertNotIn('secret', json.dumps(self.fleet.snapshot()))
        self.assertEqual(self.fleet.path.stat().st_mode & 0o777, 0o600)
        self.fleet.unreachable = True
        with self.assertRaises(FleetError):
            self.fleet.add({'name': 'Bad', 'ip': '192.168.31.233'})
        self.assertEqual(len(self.fleet.vehicles), 1)

    def test_duplicate_and_invalid_ip_rejected(self):
        for ip in ['192.168.31.232', '192.168.31.233:8765', '0.0.0.0', 'example.com']:
            with self.assertRaises(FleetError):
                self.fleet.add({'name': 'Bad', 'ip': ip})

    def test_localization_and_tracking_are_separate_gates(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        self.assertEqual(self.fleet.calls, ACTIONS[:4])
        with self.assertRaises(FleetError):
            self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'localization_done'})
        self.localize(job, row)
        self.wait_for(lambda: row['status'] == 'awaiting_start')
        self.assertNotIn('start_tracking', self.fleet.calls)
        with self.assertRaises(FleetError):
            self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'confirm_start'})
        self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'confirm_start', 'safety_confirmed': True})
        self.wait_for(lambda: row['status'] == 'completed')
        self.assertEqual(self.fleet.calls, ACTIONS)

    def test_cancel_does_not_continue_after_manual_gate(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'cancel'})
        time.sleep(0.3)
        self.assertEqual(row['status'], 'cancelled')
        self.assertNotIn('load_route', self.fleet.calls)

    def test_duplicate_jobs_rejected(self):
        self.job()
        with self.assertRaises(FleetError):
            self.job()

    def test_emergency_cancels_pending_start(self):
        job, row = self.job()
        self.localize(job, row)
        self.wait_for(lambda: row['status'] == 'awaiting_start')
        self.fleet.emergency(self.identifier)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(self.fleet.calls[-1], 'emergency_stop')
        self.assertNotIn('start_tracking', self.fleet.calls)

    def test_disconnect_fails_without_advancing(self):
        job, row = self.job()
        self.localize(job, row)
        self.fleet.unreachable = True
        self.wait_for(lambda: row['status'] == 'failed')
        self.assertNotIn('start_tracking', self.fleet.calls)

    def test_restart_marks_unfinished_jobs_interrupted(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        restored = Fleet(self.fleet.path)
        self.assertEqual(restored.jobs[0]['rows'][0]['status'], 'interrupted')
        self.assertEqual(restored.vehicles[self.identifier]['name'], 'Training 01')

    def test_missing_file_and_offline_rejected(self):
        with self.assertRaises(FleetError):
            self.fleet.create_job({'vehicles': [self.identifier], 'target': 5, 'files': {self.identifier: {'map': '../bad.pcd'}}})
        self.fleet.unreachable = True
        self.fleet.refresh(self.identifier)
        with self.assertRaises(FleetError):
            self.job()

    def test_stale_snapshot_cannot_reconfigure_running_vehicle(self):
        self.fleet.remote['current_stage'] = 6
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'failed')
        self.assertEqual(self.fleet.calls, [])


if __name__ == '__main__':
    unittest.main()
