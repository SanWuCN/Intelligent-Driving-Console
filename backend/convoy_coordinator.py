"""Three-second multi-vehicle spacing coordinator for completed fleet jobs."""
from __future__ import annotations

import concurrent.futures
import copy
import math
import threading
import time
import uuid
from urllib.parse import quote

from convoy_policy import LoopRoute, SpacingState, ordered_gaps


INTERVAL_SECONDS = 3.0
POSE_MAX_AGE_SECONDS = 2.0
MODE_RANK = {'clear': 0, 'boost': 1, 'slow': 2, 'hold': 3, 'safety_stop': 4}


class ConvoyCoordinator:
    def __init__(self, fleet):
        self.fleet = fleet
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.routes = {}
        self.projections = {}
        self.pairs = {}
        self.last_ahead = {}
        self.last_modes = {}
        self.controlled = set()
        self.status = {
            'enabled': True, 'interval_seconds': INTERVAL_SECONDS, 'active': False,
            'session': self.session, 'updated': None, 'groups': [], 'vehicles': {}, 'error': '',
        }

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.status)

    def start(self):
        threading.Thread(target=self.run, name='convoy-coordinator', daemon=True).start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # a coordinator fault must not kill later safety checks
                with self.lock:
                    self.status.update(active=False, updated=time.time(), error=str(exc))
            remaining = max(0.0, INTERVAL_SECONDS - (time.monotonic() - started))
            self.stop_event.wait(remaining)

    def _groups(self):
        """Use the newest completed batch for each vehicle and route name."""
        with self.fleet.lock:
            jobs = copy.deepcopy(self.fleet.jobs)
            states = copy.deepcopy(self.fleet.states)
        claimed = set()
        groups = []
        for job in jobs:  # jobs are newest first
            by_route = {}
            for row in job.get('rows', []):
                if row.get('status') != 'completed' or row['vehicle_id'] in claimed:
                    continue
                state = states.get(row['vehicle_id'], {})
                remote = state.get('state') or {}
                # A known reset/emergency removes the vehicle.  An offline car
                # remains in the group because it may still physically be moving.
                if state.get('online') and (remote.get('emergency') or remote.get('current_stage', 0) < 6):
                    continue
                by_route.setdefault(row.get('route', ''), []).append(row['vehicle_id'])
            for route_name, identifiers in by_route.items():
                if route_name and len(identifiers) >= 2:
                    groups.append({'id': job['id'], 'route': route_name, 'vehicles': identifiers})
                    claimed.update(identifiers)
        return groups, states

    def _telemetry(self, identifiers):
        def read(identifier):
            vehicle = self.fleet.vehicle(identifier)
            return identifier, self.fleet.request(vehicle, '/api/convoy', timeout=4)

        values, errors = {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(identifiers))) as pool:
            futures = {pool.submit(read, identifier): identifier for identifier in identifiers}
            for future, identifier in [(future, futures[future]) for future in futures]:
                try:
                    key, payload = future.result()
                    values[key] = payload
                except Exception as exc:
                    errors[identifier] = str(exc)
        return values, errors

    @staticmethod
    def _validate_pose(payload):
        pose = payload.get('pose') if isinstance(payload, dict) else None
        if not isinstance(pose, dict):
            raise ValueError('无 /current_pose 定位数据')
        values = [float(pose[key]) for key in ('x', 'y', 'yaw', 'pose_age')]
        if not all(math.isfinite(value) for value in values):
            raise ValueError('定位数据非有限数')
        if values[3] > POSE_MAX_AGE_SECONDS:
            raise ValueError(f'定位数据已过期（{values[3]:.1f}s）')
        if pose.get('frame') not in ('map', 'world'):
            raise ValueError(f"定位坐标系不是 map/world：{pose.get('frame') or '空'}")
        return pose

    def _route(self, route_hash, route_name, vehicle_id):
        route = self.routes.get(route_hash)
        if route is not None:
            return route
        vehicle = self.fleet.vehicle(vehicle_id)
        payload = self.fleet.request(vehicle, '/api/route?file=' + quote(route_name), timeout=5)
        points = [(float(point['x']), float(point['y'])) for point in payload.get('points', [])]
        route = LoopRoute(points)
        self.routes[route_hash] = route
        return route

    def _next_sequence(self):
        with self.lock:
            self.sequence += 1
            return self.sequence

    def _command(self, identifier, command):
        payload = dict(command)
        payload.update(action='convoy_control', session=self.session, sequence=self._next_sequence())
        return self.fleet.request(self.fleet.vehicle(identifier), '/api/action', payload, timeout=4)

    def _send(self, commands):
        errors = {}
        sendable = {}
        for identifier, command in commands.items():
            signature = (command['mode'], round(command['speed_mps'], 4), command.get('event_id', ''))
            # Active modes are leases and must be refreshed every tick.  A
            # repeated clear command only creates needless replanning traffic.
            if command['mode'] != 'clear' or self.last_modes.get(identifier) != signature:
                sendable[identifier] = command
            self.last_modes[identifier] = signature
        if not sendable:
            return errors
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(sendable))) as pool:
            futures = {pool.submit(self._command, identifier, command): identifier
                       for identifier, command in sendable.items()}
            for future, identifier in [(future, futures[future]) for future in futures]:
                try:
                    future.result()
                except Exception as exc:
                    errors[identifier] = str(exc)
        return errors

    @staticmethod
    def _prefer(current, candidate):
        return candidate if MODE_RANK[candidate['mode']] > MODE_RANK[current['mode']] else current

    def _safe_stop(self, identifiers, reason, telemetry):
        commands = {}
        for identifier in identifiers:
            payload = telemetry.get(identifier) or {}
            nominal = float((payload.get('control') or {}).get('nominal_speed_mps') or 0.2)
            commands[identifier] = {
                'mode': 'safety_stop', 'speed_mps': 0.0, 'after_speed_mps': nominal * 0.7,
                'event_id': 'safety-' + self.session, 'detail': reason,
            }
        return commands

    def _nominal_command(self, identifier, telemetry, detail):
        payload = telemetry.get(identifier) or {}
        remote = (self.fleet.states.get(identifier, {}).get('state') or {})
        nominal = float((payload.get('control') or {}).get('nominal_speed_mps') or
                        remote.get('parameters', {}).get('speed_limit_mps') or 0.2)
        return {'mode': 'clear', 'speed_mps': nominal, 'after_speed_mps': nominal,
                'event_id': '', 'detail': detail}

    def _degraded_commands(self, group, failed, states, telemetry, reason):
        """Stop the affected tail of the last known chain, not its leader."""
        identifiers = group['vehicles']
        ahead = self.last_ahead.get((group['id'], group['route']))
        if not ahead or len(failed) >= len(identifiers):
            return None, set(identifiers)

        # A failed rear/middle car must stop locally.  Every car behind a
        # failed car also stops because its distance to the obstruction is now
        # unknown.  The frontmost car has no `ahead` entry and keeps nominal
        # speed so a follower outage cannot stop the leader.
        blocked = set(failed)
        stopped = {identifier for identifier in failed if identifier in ahead}
        changed = True
        while changed:
            changed = False
            for rear, front in ahead.items():
                if front in blocked and rear not in stopped:
                    stopped.add(rear)
                    blocked.add(rear)
                    changed = True

        commands = {}
        for identifier in identifiers:
            if not states.get(identifier, {}).get('online'):
                continue
            if identifier in stopped:
                commands.update(self._safe_stop([identifier], reason, telemetry))
            else:
                commands[identifier] = self._nominal_command(
                    identifier, telemetry, '前后关系已保留；故障位于本车后方，前车保持常速')
        return commands, stopped

    def tick(self):
        groups, states = self._groups()
        all_members = {identifier for group in groups for identifier in group['vehicles']}
        telemetry, read_errors = self._telemetry(list(all_members)) if all_members else ({}, {})
        commands = {}
        vehicle_status = {}
        group_status = []
        active_pair_keys = set()
        now = time.monotonic()

        for group in groups:
            identifiers = group['vehicles']
            group_view = {'job_id': group['id'], 'route': group['route'], 'vehicles': identifiers,
                          'route_hash': '', 'pairs': [], 'state': 'active', 'error': ''}
            failures = dict((identifier, read_errors[identifier]) for identifier in identifiers if identifier in read_errors)
            position_failures = set(failures)
            route_hashes = set()
            map_names = set()
            poses = {}
            for identifier in identifiers:
                payload = telemetry.get(identifier)
                try:
                    if not isinstance(payload, dict):
                        position_failures.add(identifier)
                        raise ValueError('车端编队接口无响应')
                    if payload.get('selected_route') != group['route']:
                        raise ValueError('车端选中路线与批次不一致')
                    route_hash = str(payload.get('route_hash') or '')
                    if not route_hash:
                        raise ValueError('路线指纹缺失')
                    route_hashes.add(route_hash)
                    map_name = str(payload.get('selected_map') or '')
                    if not map_name:
                        raise ValueError('地图名称缺失')
                    map_names.add(map_name)
                except Exception as exc:
                    failures[identifier] = str(exc)
                    continue
                try:
                    poses[identifier] = self._validate_pose(payload)
                except Exception as exc:
                    failures[identifier] = str(exc)
                    position_failures.add(identifier)

            if not failures and len(route_hashes) != 1:
                failures['route'] = '同名 CSV 内容不一致'
            if not failures and len(map_names) != 1:
                failures['map'] = '车辆选中的地图不一致'
            if failures:
                reason = '编队数据不完整，安全停车：' + '；'.join(f'{key}:{value}' for key, value in failures.items())
                failed = {key for key in failures if key in identifiers}
                partial = bool(failed) and failed == position_failures
                degraded, stopped = self._degraded_commands(
                    group, failed, states, telemetry, reason) if partial else (None, set(identifiers))
                if degraded is None:
                    degraded = self._safe_stop(
                        [identifier for identifier in identifiers if states.get(identifier, {}).get('online')],
                        reason, telemetry,
                    )
                commands.update(degraded)
                for identifier in identifiers:
                    is_stopped = identifier in stopped
                    vehicle_status[identifier] = {
                        'mode': 'safety_stop' if is_stopped else 'clear', 'gap_m': None,
                        'ahead': self.last_ahead.get((group['id'], group['route']), {}).get(identifier),
                        'detail': reason if is_stopped else '故障位于本车后方，前车保持常速',
                    }
                group_view.update(state='degraded' if stopped != set(identifiers) else 'safety_stop', error=reason)
                group_status.append(group_view)
                continue

            route_hash = next(iter(route_hashes))
            try:
                route = self._route(route_hash, group['route'], identifiers[0])
            except Exception as exc:
                reason = '路线数据读取失败，安全停车：' + str(exc)
                for identifier in identifiers:
                    vehicle_status[identifier] = {'mode': 'safety_stop', 'gap_m': None, 'ahead': None,
                                                  'detail': reason}
                commands.update(self._safe_stop(identifiers, reason, telemetry))
                group_view.update(state='safety_stop', error=reason)
                group_status.append(group_view)
                continue
            group_view['route_hash'] = route_hash
            progress = {}
            try:
                for identifier, pose in poses.items():
                    key = (identifier, route_hash)
                    previous = self.projections.get(key)
                    elapsed = now - previous[1] if previous else 0.0
                    s, error = route.project(float(pose['x']), float(pose['y']), float(pose['yaw']),
                                             previous[0] if previous else None, elapsed)
                    self.projections[key] = (s, now)
                    progress[identifier] = s
                    vehicle_status[identifier] = {
                        'mode': 'clear', 'progress_m': round(s, 2), 'route_error_m': round(error, 2),
                        'gap_m': None, 'ahead': None, 'detail': '编队间距正常',
                    }
            except Exception as exc:
                reason = '路线投影失败，安全停车：' + str(exc)
                for identifier in identifiers:
                    vehicle_status[identifier] = {'mode': 'safety_stop', 'gap_m': None, 'ahead': None,
                                                  'detail': reason}
                commands.update(self._safe_stop(identifiers, reason, telemetry))
                group_view.update(state='safety_stop', error=reason)
                group_status.append(group_view)
                continue

            desired = {}
            for identifier in identifiers:
                nominal = float(telemetry[identifier]['control']['nominal_speed_mps'])
                desired[identifier] = {
                    'mode': 'clear', 'speed_mps': nominal, 'after_speed_mps': nominal * 0.7,
                    'event_id': '', 'detail': '编队间距正常',
                }

            gaps = ordered_gaps(progress, route.length)
            if gaps:
                largest = max(gaps, key=lambda item: item[2])
                self.last_ahead[(group['id'], group['route'])] = {
                    rear: front for rear, front, gap in gaps if (rear, front, gap) != largest
                }

            for rear, front, gap in gaps:
                pair_key = (group['id'], route_hash, rear, front)
                active_pair_keys.add(pair_key)
                state = self.pairs.setdefault(pair_key, SpacingState())
                phase, remaining, event = state.update(gap, now)
                pair = {'rear': rear, 'front': front, 'gap_m': round(gap, 2), 'phase': phase,
                        'hold_remaining_seconds': round(remaining, 2)}
                group_view['pairs'].append(pair)
                vehicle_status[rear].update(gap_m=round(gap, 2), ahead=front)
                if phase == 'clear':
                    continue
                rear_nominal = float(telemetry[rear]['control']['nominal_speed_mps'])
                front_nominal = float(telemetry[front]['control']['nominal_speed_mps'])
                event_id = f"{group['id']}:{rear}:{front}:{event}"
                rear_command = {
                    'mode': 'hold' if phase == 'hold' else 'slow',
                    'speed_mps': 0.0 if phase == 'hold' else rear_nominal * 0.7,
                    'after_speed_mps': rear_nominal * 0.7, 'event_id': event_id,
                    'detail': f'前车距离 {gap:.2f} m；' + ('停车 5 秒' if phase == 'hold' else '减速 30%'),
                }
                front_command = {
                    'mode': 'boost', 'speed_mps': min(2.0, front_nominal * 1.1),
                    'after_speed_mps': front_nominal, 'event_id': event_id,
                    'detail': f'后车距离 {gap:.2f} m，提速 10% 拉开距离',
                }
                desired[rear] = self._prefer(desired[rear], rear_command)
                # The requested sequence is: rear holds for five seconds first;
                # only the following spacing phase slows the rear and boosts the leader.
                if phase == 'spacing':
                    desired[front] = self._prefer(desired[front], front_command)

            for identifier, command in desired.items():
                vehicle_status[identifier].update(mode=command['mode'], detail=command['detail'])
            commands.update(desired)
            group_status.append(group_view)

        # Any car previously controlled but no longer belonging to a live
        # cohort receives one explicit clear command.
        for identifier in self.controlled - all_members:
            try:
                nominal = float((self.fleet.states.get(identifier, {}).get('state') or {}).get('parameters', {}).get('speed_limit_mps', 0.2))
                commands[identifier] = {'mode': 'clear', 'speed_mps': nominal, 'after_speed_mps': nominal,
                                        'event_id': '', 'detail': '已退出编队'}
            except Exception:
                pass

        self.pairs = {key: value for key, value in self.pairs.items() if key in active_pair_keys}
        command_errors = self._send(commands)
        for identifier, error in command_errors.items():
            vehicle_status.setdefault(identifier, {}).update(command_error=error)
        self.controlled = set(all_members)
        with self.lock:
            self.status = {
                'enabled': True, 'interval_seconds': INTERVAL_SECONDS, 'active': bool(groups),
                'session': self.session, 'updated': time.time(), 'groups': group_status,
                'vehicles': vehicle_status,
                'error': '；'.join(f'{identifier}:{error}' for identifier, error in command_errors.items()),
            }
