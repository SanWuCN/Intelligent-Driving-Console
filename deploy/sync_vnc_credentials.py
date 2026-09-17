#!/usr/bin/env python3
"""Import the existing local x11vnc credential without displaying it."""
import json
import os
from pathlib import Path

from Crypto.Cipher import DES

config_path = Path('/home/nvidia/Desktop/bigcar-console/runtime/config.json')
password_file = Path('/etc/x11vnc.pass')
encrypted = password_file.read_bytes()
if len(encrypted) < 8:
    raise RuntimeError('Invalid x11vnc password file')
# VNC's d3des key convention reverses the bits within each DES key byte.
key = bytes(int(format(value, '08b')[::-1], 2) for value in [23, 82, 107, 6, 35, 78, 88, 7])
password = DES.new(key, DES.MODE_ECB).decrypt(encrypted[:8]).rstrip(b'\0').decode('latin-1')
if not password:
    raise RuntimeError('Empty VNC credential')
config = json.loads(config_path.read_text())
config['vnc_password'] = password
temporary = config_path.with_suffix('.json.tmp')
fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
os.replace(temporary, config_path)
os.chmod(config_path, 0o600)
print('Existing VNC credential synchronized; password unchanged.')
