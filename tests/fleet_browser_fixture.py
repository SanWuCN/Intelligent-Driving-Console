"""Isolated simulated vehicles for browser integration checks; no ROS commands."""
import sys
import socket
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app import ConsoleHandler
from controller import BigCarController
from fleet import Fleet, Handler, ROOT

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for index in (1, 2):
        vehicle_root = root / str(index)
        controller = BigCarController(vehicle_root, simulate=True)
        controller.data_dir = vehicle_root
        (vehicle_root / 'room.pcd').write_text('test map')
        (vehicle_root / 'loop.csv').write_text('x,y,z,yaw,velocity\n0,0,0,0,0.2\n1,0,0,0,0.2\n0,0,0,0,0.2\n')
        controller.config.update(selected_map='room.pcd', selected_route='loop.csv', control_token='browser-test')
        handler = type('SimVehicle', (ConsoleHandler,), {'controller': controller, 'static_root': ROOT / 'dist'})
        server_class = ThreadingHTTPServer if index == 1 else type('IPv6Server', (ThreadingHTTPServer,), {'address_family': socket.AF_INET6})
        server = server_class(('127.0.0.1' if index == 1 else '::1', 8872), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    fleet = Fleet(root / 'fleet.json')
    fleet.settings['vehicle_port'] = 8872
    fleet.settings['poll_seconds'] = 2
    handler = type('TestFleet', (Handler,), {'fleet': fleet})
    server = ThreadingHTTPServer(('127.0.0.1', 8873), handler)
    threading.Thread(target=fleet.poll, daemon=True).start()
    print('READY', flush=True)
    server.serve_forever()
