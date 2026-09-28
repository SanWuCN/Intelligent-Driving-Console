import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "deploy" / "wifi-watchdog.sh"


class WifiWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.root / "connected"
        self.calls = self.root / "calls"

        self._command(
            "nmcli",
            f"""
            printf '%s\\n' "$*" >> {self.calls!s}
            if [[ "$*" == "-g GENERAL.STATE device show wlan0" ]]; then
              [[ -f {self.marker!s} ]] && echo '100 (connected)' || echo '30 (disconnected)'
              exit 0
            fi
            if [[ "$*" == "--wait 20 device connect wlan0" ]]; then
              [[ "${{NMCLI_CONNECT_RESULT:-success}}" == success ]] || exit 10
              touch {self.marker!s}
              exit 0
            fi
            if [[ "$*" == "-t -f UUID,TYPE,AUTOCONNECT connection show" ]]; then
              exit 0
            fi
            exit 0
            """,
        )
        self._command(
            "ip",
            f"""
            if [[ "$*" == "-4 -o address show dev wlan0 scope global" && -f {self.marker!s} ]]; then
              echo '3: wlan0 inet 192.168.31.134/24 scope global wlan0'
            fi
            """,
        )
        self._command("iw", f"printf '%s\\n' \"$*\" >> {self.calls!s}")

    def tearDown(self):
        self.temp.cleanup()

    def _command(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def _run_once(self, **extra_env):
        env = os.environ.copy()
        env.update(extra_env)
        env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        return subprocess.run(
            ["bash", str(WATCHDOG), "--once"],
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )

    def test_healthy_local_link_does_not_require_public_internet(self):
        self.marker.touch()
        result = self._run_once()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("device connect", self.calls.read_text(encoding="utf-8"))

    def test_disconnected_link_reconnects_a_saved_network(self):
        result = self._run_once()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn("Wi-Fi reconnect succeeded", result.stdout)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn("device connect wlan0", calls)
        self.assertIn("dev wlan0 set power_save off", calls)

    def test_failed_reconnect_returns_failure_in_one_shot_mode(self):
        result = self._run_once(NMCLI_CONNECT_RESULT="failure")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not find an available saved network", result.stdout)


if __name__ == "__main__":
    unittest.main()
