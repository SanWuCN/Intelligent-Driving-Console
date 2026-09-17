#!/usr/bin/env python3
"""Local fleet coordinator. Vehicle actions remain owned by the vehicle API."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import http.client
import ipaddress
import json
import os
import select
import socket
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ['environment_check', 'start_hardware', 'start_autoware', 'start_localization', 'load_route', 'start_tracking']
TITLES = ['环境检查', '底盘与雷达', 'Autoware', '地图与标定', '路径配置', '循迹运行']
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


class FleetError(ValueError):
    pass


class Fleet:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.dispatch_lock = threading.RLock()
        self.settings = {'vehicle_port': 8765, 'poll_seconds': 3, 'step_timeout': 120}
        self.vehicles = {}
        self.jobs = []
        self.states = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.settings.update(saved['settings'])
            self.vehicles = saved['vehicles']
            self.jobs = saved['jobs']
        for job in self.jobs:
            for row in job['rows']:
                if row['status'] not in TERMINAL:
                    row.update(status='interrupted', message='管理服务已重启，请核对车端状态后新建任务')
        self.save()

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump({'settings': self.settings, 'vehicles': self.vehicles, 'jobs': self.jobs}, handle, ensure_ascii=False)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)

    def vehicle(self, identifier):
        with self.lock:
            if identifier not in self.vehicles:
                raise FleetError('车辆不存在')
            return dict(self.vehicles[identifier])

    def request(self, vehicle, path='/api/state', body=None):
        connection = http.client.HTTPConnection(vehicle['ip'], vehicle['port'], timeout=12)
        try:
            headers = {'X-Control-Token': vehicle['token'], 'Content-Type': 'application/json'}
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            data = json.loads(response.read(4 * 1024 * 1024))
            if response.status >= 400:
                raise FleetError(data.get('error', f'车端返回 HTTP {response.status}'))
            return data
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise FleetError(str(exc)) from exc
        finally:
            connection.close()

    @staticmethod
    def validate_state(state):
        if not isinstance(state, dict) or not isinstance(state.get('current_stage'), int) or not isinstance(state.get('workflow'), list) or not isinstance(state.get('telemetry'), dict):
            raise FleetError('目标不是兼容的智能驾驶控制台')

    def add(self, data):
        name = str(data.get('name', '')).strip()
        try:
            address = ipaddress.ip_address(str(data.get('ip', '')).strip())
        except ValueError as exc:
            raise FleetError('请输入有效 IP，无需端口') from exc
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise FleetError('该 IP 不可用')
        if not name or len(name) > 60:
            raise FleetError('车辆名称须为 1–60 个字符')
        vehicle = {'id': uuid.uuid4().hex, 'name': name, 'ip': str(address), 'port': self.settings['vehicle_port'], 'token': str(data.get('token', ''))}
        state = self.request(vehicle)
        self.validate_state(state)
        with self.lock:
            if any(v['ip'] == vehicle['ip'] for v in self.vehicles.values()):
                raise FleetError('该 IP 已添加')
            self.vehicles[vehicle['id']] = vehicle
            self.states[vehicle['id']] = {'online': True, 'state': state, 'updated': time.time(), 'error': ''}
            self.save()
        return {'ok': True}

    def refresh(self, identifier):
        try:
            state = self.request(self.vehicle(identifier))
            self.validate_state(state)
            result = {'online': True, 'state': state, 'updated': time.time(), 'error': ''}
        except Exception as exc:
            result = {'online': False, 'state': None, 'updated': time.time(), 'error': str(exc)}
        with self.lock:
            if identifier in self.vehicles:
                self.states[identifier] = result
        return result

    def poll(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            while True:
                with self.lock:
                    identifiers = list(self.vehicles)
                list(pool.map(self.refresh, identifiers))
                time.sleep(self.settings['poll_seconds'])

    def snapshot(self):
        with self.lock:
            vehicles = [{**{k: v for k, v in vehicle.items() if k != 'token'}, **self.states.get(identifier, {'online': False, 'state': None, 'error': '等待连接'})} for identifier, vehicle in self.vehicles.items()]
            return copy.deepcopy({'vehicles': vehicles, 'jobs': self.jobs, 'settings': self.settings, 'timestamp': time.time()})

    def change(self, row, **values):
        with self.lock:
            if row['status'] == 'cancelled':
                return
            row.update(values, updated=time.time())
            self.save()

    def create_job(self, data):
        identifiers = data.get('vehicles', [])
        target = data.get('target', 5)
        if not isinstance(identifiers, list) or not identifiers or len(identifiers) > 50 or len(set(identifiers)) != len(identifiers):
            raise FleetError('请选择 1–50 辆车辆')
        if target not in (4, 5, 6):
            raise FleetError('无效的目标阶段')
        with self.lock:
            occupied = {row['vehicle_id'] for job in self.jobs for row in job['rows'] if row['status'] not in TERMINAL}
            rows = []
            for identifier in identifiers:
                vehicle = self.vehicle(identifier)
                if identifier in occupied:
                    raise FleetError(f"{vehicle['name']} 已有进行中的任务")
                cached = self.states.get(identifier, {})
                if not cached.get('online'):
                    raise FleetError(f"{vehicle['name']} 离线")
                state = cached['state']
                files = data.get('files', {}).get(identifier, {})
                map_name = files.get('map', state['selected_map'])
                route = files.get('route', state['selected_route'])
                if map_name not in [f['name'] for f in state['maps']]:
                    raise FleetError(f"请为 {vehicle['name']} 选择地图")
                if target >= 5 and route not in [f['name'] for f in state['routes']]:
                    raise FleetError(f"请为 {vehicle['name']} 选择路径")
                if state['emergency'] or state['busy'] or state['current_stage'] >= 6:
                    raise FleetError(f"{vehicle['name']} 忙碌、正在运行或处于急停状态，请先在车端处理")
                rows.append({'vehicle_id': identifier, 'name': vehicle['name'], 'ip': vehicle['ip'], 'status': 'queued', 'step': 0, 'message': '等待执行', 'map': map_name, 'route': route, 'manual_confirmed': False, 'safety_confirmed': False, 'events': []})
            job = {'id': uuid.uuid4().hex, 'created': time.time(), 'target': target, 'rows': rows}
            self.jobs.insert(0, job)
            self.save()
        threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
        return {'ok': True, 'id': job['id']}

    def run_job(self, job):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda row: self.run_row(job, row), job['rows']))

    def wait_state(self, row, predicate):
        deadline = time.monotonic() + self.settings['step_timeout']
        while time.monotonic() < deadline:
            if row['status'] == 'cancelled':
                return False
            result = self.refresh(row['vehicle_id'])
            if not result['online']:
                raise FleetError('车辆连接中断：' + result['error'])
            state = result['state']
            if state['emergency']:
                raise FleetError('车端已急停')
            if state['last_error'] and not state['busy']:
                raise FleetError(state['last_error'])
            if not state['busy'] and predicate(state):
                return True
            time.sleep(0.5)
        raise FleetError('等待车端状态超时')

    def run_row(self, job, row):
        try:
            vehicle = self.vehicle(row['vehicle_id'])
            for step in range(1, job['target'] + 1):
                if row['status'] == 'cancelled':
                    return
                state = self.request(vehicle)
                if state['emergency'] or state['busy']:
                    raise FleetError('车端急停或正在执行其他动作')
                if state['current_stage'] >= 6:
                    raise FleetError('车辆已在其他会话进入循迹，已停止本任务')
                # Map and route choices must be applied even when ROS already reached that stage.
                skip = step <= 3 and state['current_stage'] >= step
                self.change(row, status='running', step=step, message=TITLES[step - 1])
                if step == 6:
                    self.change(row, status='awaiting_start', message='等待循迹安全确认')
                    while not row['safety_confirmed']:
                        if row['status'] == 'cancelled':
                            return
                        time.sleep(0.2)
                if not skip:
                    with self.dispatch_lock:
                        if row['status'] == 'cancelled':
                            return
                        self.request(vehicle, '/api/action', {'action': ACTIONS[step - 1], 'map': row['map'], 'route': row['route'], 'safety_confirmed': row['safety_confirmed']})
                    if step == 4:
                        if not self.wait_state(row, lambda s: True):
                            return
                        self.change(row, status='awaiting_localization', message='等待人工定位')
                        while not row['manual_confirmed']:
                            if row['status'] == 'cancelled':
                                return
                            time.sleep(0.2)
                    if not self.wait_state(row, lambda s: s['current_stage'] >= step):
                        return
                with self.lock:
                    row['events'].append({'time': time.time(), 'message': TITLES[step - 1] + '：状态校验通过'})
                self.change(row, status='running', message=TITLES[step - 1] + '完成')
            self.change(row, status='completed', message='目标流程完成')
        except Exception as exc:
            self.change(row, status='failed', message=str(exc))

    def job_action(self, identifier, data):
        with self.lock:
            job = next((j for j in self.jobs if j['id'] == identifier), None)
            if not job:
                raise FleetError('任务不存在')
            row = next((r for r in job['rows'] if r['vehicle_id'] == data.get('vehicle_id')), None)
        if not row or row['status'] in TERMINAL:
            raise FleetError('该车辆任务已结束或不存在')
        action = data.get('action')
        if action == 'localization_done' and row['status'] == 'awaiting_localization':
            state = self.request(self.vehicle(row['vehicle_id']))
            if state['busy'] or state['emergency'] or state['current_stage'] < 4 or (not state.get('simulated') and '/current_pose' not in state['live_topics']):
                raise FleetError('定位数据尚未就绪，请完成 RViz 初始位姿标定')
            self.change(row, manual_confirmed=True, status='running', message='定位已确认，正在继续流程')
        elif action == 'confirm_start' and row['status'] == 'awaiting_start' and data.get('safety_confirmed') is True:
            self.change(row, safety_confirmed=True, status='running', message='正在启动循迹')
        elif action == 'cancel':
            with self.dispatch_lock:
                self.change(row, status='cancelled', message='已取消后续步骤；车端现有节点保持运行')
        else:
            raise FleetError('当前状态不支持该操作')
        return {'ok': True}

    def emergency(self, identifier):
        with self.dispatch_lock:
            with self.lock:
                for job in self.jobs:
                    for row in job['rows']:
                        if row['vehicle_id'] == identifier and row['status'] not in TERMINAL:
                            self.change(row, status='cancelled', message='已取消任务并请求急停')
            return self.request(self.vehicle(identifier), '/api/action', {'action': 'emergency_stop'})


class Handler(SimpleHTTPRequestHandler):
    fleet: Fleet

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'dist'), **kwargs)

    def log_message(self, fmt, *args):
        if '/api/fleet/state' not in self.path:
            super().log_message(fmt, *args)

    def valid_origin(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        return host in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}') and (not origin or origin in (f'http://{host}', f'https://{host}'))

    def reply(self, body, status=200):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if not self.valid_origin():
            self.reply({'error': '仅允许本机同源访问'}, 403)
            return
        try:
            parts = urlparse(self.path).path.strip('/').split('/')
            if self.path == '/api/fleet/state':
                self.reply(self.fleet.snapshot())
            elif len(parts) == 7 and parts[:3] == ['api', 'fleet', 'vehicles'] and parts[4:] == ['proxy', 'api', 'screen']:
                self.screen(self.fleet.vehicle(parts[3]))
            elif self.path.startswith('/api/'):
                self.reply({'error': '接口不存在'}, 404)
            else:
                if self.path == '/':
                    self.send_response(302)
                    self.send_header('Location', '/fleet')
                    self.end_headers()
                    return
                if self.path.startswith('/fleet'):
                    self.path = '/index.html'
                super().do_GET()
        except Exception as exc:
            self.reply({'error': str(exc)}, 400)

    def do_POST(self):
        if not self.valid_origin() or 'application/json' not in self.headers.get('Content-Type', ''):
            self.reply({'error': '仅允许本机同源 JSON 请求'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 65536:
                raise FleetError('无效请求长度')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise FleetError('无效 JSON 对象')
            parts = urlparse(self.path).path.strip('/').split('/')
            if parts == ['api', 'fleet', 'vehicles']:
                result = self.fleet.add(data)
            elif parts == ['api', 'fleet', 'jobs']:
                result = self.fleet.create_job(data)
            elif len(parts) == 4 and parts[:3] == ['api', 'fleet', 'jobs']:
                result = self.fleet.job_action(parts[3], data)
            elif parts == ['api', 'fleet', 'settings']:
                settings = {key: int(data[key]) for key in ('vehicle_port', 'poll_seconds', 'step_timeout')}
                if not 1 <= settings['vehicle_port'] <= 65535 or not 2 <= settings['poll_seconds'] <= 60 or not 10 <= settings['step_timeout'] <= 600:
                    raise FleetError('设置值超出允许范围')
                with self.fleet.lock:
                    self.fleet.settings = settings
                    self.fleet.save()
                result = {'ok': True}
            elif len(parts) >= 5 and parts[:3] == ['api', 'fleet', 'vehicles']:
                vehicle = self.fleet.vehicle(parts[3])
                if parts[4:] == ['proxy', 'api', 'screen-ticket']:
                    result = self.fleet.request(vehicle, '/api/screen-ticket', {})
                elif parts[4:] == ['emergency']:
                    result = self.fleet.emergency(parts[3])
                elif parts[4:] == ['remove']:
                    with self.fleet.lock:
                        if any(r['vehicle_id'] == parts[3] and r['status'] not in TERMINAL for j in self.fleet.jobs for r in j['rows']):
                            raise FleetError('请先结束该车辆任务')
                        del self.fleet.vehicles[parts[3]]
                        self.fleet.states.pop(parts[3], None)
                        self.fleet.save()
                    result = {'ok': True}
                elif parts[4:] == ['update']:
                    name = str(data.get('name', '')).strip()
                    if not name or len(name) > 60:
                        raise FleetError('车辆名称须为 1–60 个字符')
                    with self.fleet.lock:
                        self.fleet.vehicles[parts[3]]['name'] = name
                        if data.get('token'):
                            self.fleet.vehicles[parts[3]]['token'] = str(data['token'])
                        self.fleet.save()
                    result = {'ok': True}
                else:
                    raise FleetError('接口不存在')
            else:
                raise FleetError('接口不存在')
            self.reply(result)
        except Exception as exc:
            self.reply({'error': str(exc)}, 400)

    def screen(self, vehicle):
        if self.headers.get('Upgrade', '').lower() != 'websocket':
            raise FleetError('需要 WebSocket')
        query = urlparse(self.path).query
        upstream = socket.create_connection((vehicle['ip'], vehicle['port']), timeout=10)
        try:
            headers = [f'GET /api/screen?{query} HTTP/1.1', f"Host: {vehicle['ip']}:{vehicle['port']}", 'Connection: Upgrade', 'Upgrade: websocket']
            for key in ('Sec-WebSocket-Key', 'Sec-WebSocket-Version', 'Sec-WebSocket-Protocol'):
                if self.headers.get(key):
                    headers.append(f'{key}: {self.headers[key]}')
            upstream.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode())
            upstream.settimeout(None)
            self.close_connection = True
            while True:
                readable, _, _ = select.select([upstream, self.connection], [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    (self.connection if source is upstream else upstream).sendall(chunk)
        finally:
            upstream.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8870)
    parser.add_argument('--data', type=Path, default=ROOT / 'runtime' / 'fleet.json')
    args = parser.parse_args()
    fleet = Fleet(args.data)
    handler = type('FleetHandler', (Handler,), {'fleet': fleet})
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    threading.Thread(target=fleet.poll, daemon=True).start()
    print(f'智能驾驶管理平台 http://127.0.0.1:{args.port}/fleet', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
