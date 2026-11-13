"""Exercise the generated host broker against disposable PTYs, never hardware.

These tests require Conserver. They skip explicitly when it is absent, unless
SERIAL_PARTY_REQUIRE_CONSERVER=1. Only generated logfile paths are relocated
into a temporary directory; the actual broker directives and rendered service
arguments are used unchanged. No system service or /etc file is touched.
"""

import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import tty
import unittest
from unittest import mock

from serial_party import client, host
from serial_party.client import Serial, SerialBusyError, SerialException


def drain(fd, timeout=0.15):
    result = bytearray()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([fd], [], [], max(0, end - time.monotonic()))[0]:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            result.extend(data)
    return bytes(result)


class RenderedBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daemon = os.environ.get("SERIAL_PARTY_TEST_CONSERVER") or shutil.which("conserver")
        if not cls.daemon and Path("/usr/sbin/conserver").is_file():
            cls.daemon = "/usr/sbin/conserver"
        cls.console = os.environ.get("SERIAL_PARTY_TEST_CONSOLE") or shutil.which("console")
        if not cls.daemon or not cls.console:
            message = "Generated broker tests require real 'conserver' and 'console' binaries"
            if os.environ.get("SERIAL_PARTY_REQUIRE_CONSERVER") == "1":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.temp = tempfile.TemporaryDirectory(prefix="serial-party-rendered-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.logs = cls.root / "logs"
        cls.logs.mkdir()
        cls.masters, cls.slaves = [], []
        for _ in range(2):
            master, slave = pty.openpty()
            tty.setraw(slave)
            cls.masters.append(master)
            cls.slaves.append(slave)
            cls.addClassCleanup(os.close, master)
            cls.addClassCleanup(os.close, slave)
        sockets = []
        try:
            for _ in range(6):
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                sockets.append(sock)
            ports = [sock.getsockname()[1] for sock in sockets]
            cls.config = {
                "version": 1, "listen_host": "127.0.0.1",
                "broker_port": ports[0], "broker_secondary_port": ports[1],
                "writers": ["operator", "hermes", "codex", "webcontrol"],
                "boards": [
                    {"id": f"board-{letter}", "name": f"Board {letter.upper()}",
                     "device": os.ttyname(cls.slaves[i]), "baud": baud,
                     "monitor_port": ports[2 + i * 2], "control_port": ports[3 + i * 2]}
                    for i, (letter, baud) in enumerate((("a", 115200), ("b", 9600)))
                ],
            }
            cls.files = host.render_files(cls.config)
            cls.path = cls.root / "config.json"
            cls.path.write_text(cls.files["/etc/serial-party/config.json"])
            cls.broker_path = cls.root / "conserver.cf"
            rendered = cls.files["/etc/serial-party/conserver.cf"]
            cls.broker_path.write_text(rendered)
            syntax = subprocess.run(
                [cls.daemon, "-S", "-C", str(cls.broker_path)],
                capture_output=True, text=True, timeout=5,
            )
            if syntax.returncode:
                raise RuntimeError("Generated broker configuration is invalid:\n" + syntax.stdout + syntax.stderr)
            cls.broker_path.write_text(rendered.replace("/var/log/serial-party/", str(cls.logs) + "/"))
            unit = cls.files["/etc/systemd/system/serial-party-conserver.service"]
            line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
            argv = shlex.split(line.removeprefix("ExecStart="))
            argv[0] = cls.daemon
            argv[argv.index("-C") + 1] = str(cls.broker_path)
        finally:
            for sock in sockets:
                sock.close()
        cls.output = (cls.root / "daemon.log").open("wb")
        cls.addClassCleanup(cls.output.close)
        cls.server = subprocess.Popen(argv, stdout=cls.output, stderr=cls.output, start_new_session=True)
        cls.addClassCleanup(cls.stop_server)
        end = time.monotonic() + 10
        while True:
            if cls.server.poll() is not None or time.monotonic() >= end:
                raise RuntimeError("Rendered broker failed to start:\n" + (cls.root / "daemon.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", ports[0]), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        cls.binary_patch = mock.patch.object(client, "_console_binary", return_value=cls.console)
        cls.binary_patch.start()
        cls.addClassCleanup(cls.binary_patch.stop)

    @classmethod
    def stop_server(cls):
        try:
            os.killpg(cls.server.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            cls.server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(cls.server.pid, signal.SIGKILL)
            cls.server.wait(timeout=3)

    def setUp(self):
        for fd in self.masters:
            drain(fd, 0.03)

    def connect(self, board="board-a", **kwargs):
        connection = Serial(board, config=self.path, timeout=0.05, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def receive(self, connection, expected):
        result = bytearray()
        end = time.monotonic() + 2
        while len(result) < len(expected) and time.monotonic() < end:
            result.extend(connection.read(len(expected) - len(result)))
        self.assertEqual(bytes(result), expected)

    def test_generated_acl_options_and_two_board_writer_routing(self):
        observer = self.connect(read_only=True)
        first = self.connect(label="hermes")
        second = self.connect("board-b", label="codex")
        os.write(self.masters[0], b"rendered-a\r\n")
        os.write(self.masters[1], b"rendered-b\r\n")
        self.receive(observer, b"rendered-a\r\n")
        self.receive(first, b"rendered-a\r\n")
        self.receive(second, b"rendered-b\r\n")
        with self.assertRaises(SerialException):
            observer.write(b"must not send\r")
        with self.assertRaises(SerialBusyError):
            self.connect(label="operator")
        first.write(b"a\r\x03\x1b[A")
        second.write(b"b\r\n")
        first.flush()
        second.flush()
        self.assertEqual(drain(self.masters[0]), b"a\r\x03\x1b[A")
        self.assertEqual(drain(self.masters[1]), b"b\r\n")
        self.assertEqual(observer.read(128), b"")

    def test_generated_monitor_replays_only_its_board_and_cannot_send_input(self):
        a_marker = b"unique-replay-A\r\n"
        b_marker = b"unique-replay-B\r\n"
        # A wrapper opens the connection without requesting write ownership,
        # ensuring the target is ready before synthetic output is emitted.
        self.connect(read_only=True)
        self.connect("board-b", read_only=True)
        os.write(self.masters[0], a_marker)
        os.write(self.masters[1], b_marker)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(marker in (self.logs / f"board-{letter}.log").read_bytes()
                   for letter, marker in (("a", a_marker), ("b", b_marker))):
                break
            time.sleep(0.03)
        monitor_argv = host.terminal_argv(
            self.config, self.config["boards"][0], "monitor", "fixture:" + "x" * 32
        )
        argv = monitor_argv[monitor_argv.index("/usr/bin/console"):]
        argv[0] = self.console
        master, slave = pty.openpty()
        tty.setraw(slave)
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        try:
            replay = drain(master, 1)
            self.assertIn(b"unique-replay-A", replay)
            self.assertNotIn(b"unique-replay-B", replay)
            # Exercise the broker's own read-only account, beyond ttyd's input
            # gate: literal input and an explicit acquire cannot reach UART.
            os.write(master, b"must not send\r\x05ca")
            response = drain(master, 0.3)
            self.assertIn(b"read-only", response)
            os.write(master, b"still must not send\r")
            self.assertEqual(drain(self.masters[0]), b"")
            self.assertEqual(drain(self.masters[1]), b"")
        finally:
            os.close(master)
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def test_rollback_guard_understands_real_broker_client_status(self):
        # The host guard uses the native status API, not UART reads or writes.
        self.connect(read_only=True)
        host.assert_no_writers(self.config, self.config["boards"][0])
        owner = self.connect(label="hermes")
        with self.assertRaises(host.HostError):
            host.assert_no_writers(self.config, self.config["boards"][0])
        owner.close()
        host.assert_no_writers(self.config, self.config["boards"][0])
        self.assertEqual(drain(self.masters[0]), b"")


if __name__ == "__main__":
    unittest.main()
