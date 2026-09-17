import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fleet import ACTIONS, TITLES, Fleet, FleetError


class FakeFleet(Fleet):
    def __init__(self, path):
        self.remote = {'current_stage': 0, 'workflow': [], 'telemetry': {}, 'maps': [{'name': 'room.pcd'}], 'routes': [{'name': 'loop.csv'}], 'selected_map': 'room.pcd', 'selected_route': 'loop.csv', 'busy': None, 'last_error': None, 'emergency': False, 'live_topics': [], 'simulated': False}
        self.calls = []
        self.unreachable = False
        self.flaky = 0
        super().__init__(path)

    def refresh(self, identifier):
        if self.flaky:
            self.flaky -= 1
            return {'online': False, 'state': None, 'updated': time.time(), 'error': 'timed out'}
        return super().refresh(identifier)

    def request(self, vehicle, path='/api/state', body=None):
        if self.unreachable:
            raise FleetError('offline')
        with self.lock:
            if body is not None:
                action = body['action']
                self.calls.append(action)
                if action == 'emergency_stop':
                    self.remote['emergency'] = True
                elif action == 'restart_workflow':
                    self.remote['current_stage'] = 0
                elif action in ACTIONS and action != 'start_localization':
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

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
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

    # ---------------------------------------------------------------- step ledger
    def test_step_ledger_records_timings_and_car_reported_detail(self):
        self.fleet.remote['workflow'] = [{'id': index + 1, 'title': title, 'detail': f'{title}·车端自检通过'}
                                         for index, title in enumerate(TITLES)]
        job, row = self.job(target=4)
        self.wait_for(lambda: row['status'] == 'awaiting_localization')

        ledger = row['steps']
        self.assertEqual([record['index'] for record in ledger], [1, 2, 3, 4])
        self.assertEqual([record['status'] for record in ledger[:3]], ['ok', 'ok', 'ok'])
        self.assertEqual(ledger[3]['status'], 'running')
        for record in ledger[:3]:
            self.assertIsNotNone(record['started'])
            self.assertIsNotNone(record['finished'])
            self.assertGreaterEqual(record['duration'], 0)
            self.assertTrue(record['detail'].startswith(TITLES[record['index'] - 1]))
        self.assertEqual(ledger[0]['stage_before'], 0)
        self.assertEqual(ledger[0]['stage_after'], 1)
        self.assertFalse(ledger[0]['skipped'])
        self.assertTrue(any(event['message'].startswith('已下发动作 environment_check') for event in row['events']))

    def test_already_satisfied_steps_are_marked_skipped(self):
        self.fleet.remote['current_stage'] = 3
        job, row = self.job(target=4)
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        self.assertEqual([record['status'] for record in row['steps'][:3]], ['skipped', 'skipped', 'skipped'])
        self.assertEqual(self.fleet.calls, ['start_localization'])

    def test_car_that_advances_on_its_own_releases_the_gate(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        # Operator finished localization on the car console instead of the platform.
        with self.fleet.lock:
            self.fleet.remote['current_stage'] = 5
        self.wait_for(lambda: row['status'] == 'awaiting_start')
        self.assertTrue(row['manual_confirmed'])
        self.assertEqual(row['steps'][3]['status'], 'ok')
        self.assertTrue(any('自动对账放行' in event['message'] for event in row['events']))

    def test_localization_timeout_releases_the_vehicle(self):
        self.fleet.settings['localization_timeout'] = 1
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'failed', timeout=6)
        self.assertIn('等待人工定位', row['message'])
        self.assertEqual(row['steps'][3]['status'], 'failed')
        # The vehicle is free again, so create_job accepts it (it would raise otherwise).
        self.job()
        self.assertEqual(len(self.fleet.jobs), 2)

    def test_transient_poll_failures_do_not_fail_the_row(self):
        job, row = self.job(target=4)
        self.fleet.flaky = 2
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        self.assertNotEqual(row['status'], 'failed')
        self.assertEqual(row['steps'][0]['status'], 'ok')

    def test_retry_requeues_a_failed_vehicle_with_attempt_count(self):
        job, row = self.job()
        self.localize(job, row)
        self.fleet.unreachable = True
        self.wait_for(lambda: row['status'] == 'failed')
        self.fleet.unreachable = False
        self.fleet.refresh(self.identifier)

        response = self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'retry'})
        self.assertNotEqual(response['id'], job['id'])
        retried = self.fleet.jobs[0]
        self.assertEqual(retried['target'], job['target'])
        self.assertEqual(retried['rows'][0]['map'], row['map'])
        self.wait_for(lambda: retried['rows'][0]['attempts'] >= 2)
        self.wait_for(lambda: retried['rows'][0]['status'] == 'awaiting_localization')
        with self.assertRaises(FleetError):
            self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'retry'})

    def test_records_can_only_be_deleted_once_finished(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        with self.assertRaises(FleetError):
            self.fleet.delete_job(job['id'])
        with self.assertRaises(FleetError):
            self.fleet.clear_jobs()
        self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'cancel'})
        self.assertEqual(self.fleet.clear_jobs()['removed'], 1)
        self.assertEqual(self.fleet.jobs, [])

    def test_reset_vehicle_is_blocked_while_a_task_is_live(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        with self.assertRaises(FleetError):
            self.fleet.reset_vehicle(self.identifier)
        self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'cancel'})
        self.fleet.reset_vehicle(self.identifier)
        self.assertEqual(self.fleet.calls[-1], 'restart_workflow')
        self.assertEqual(self.fleet.remote['current_stage'], 0)

    def test_summary_and_released_cancel(self):
        job, row = self.job()
        self.wait_for(lambda: row['status'] == 'awaiting_localization')
        view = self.fleet.job_view(job)
        self.assertEqual(view['target_title'], '循迹运行')
        self.assertEqual(view['summary']['total'], 1)
        self.assertEqual(view['summary']['waiting'], 1)
        self.assertEqual(view['summary']['active'], 1)
        self.assertIsNone(view['summary']['finished'])
        self.fleet.job_action(job['id'], {'vehicle_id': self.identifier, 'action': 'cancel'})
        self.assertIsNotNone(row['finished'])
        self.assertEqual(row['steps'][3]['status'], 'cancelled')
        self.assertEqual(self.fleet.job_view(job)['summary']['cancelled'], 1)


if __name__ == '__main__':
    unittest.main()
