"""Text-console safety and multi-board routing checks using only fake PTYs.

The protocol fixture tests run without Conserver. The integration class runs
the real daemon/client when installed, using temporary PTYs and high loopback
ports. Set SERIAL_PARTY_REQUIRE_CONSERVER=1 in CI to treat missing binaries as
an error. SERIAL_PARTY_TEST_CONSERVER / SERIAL_PARTY_TEST_CONSOLE may select
test binary paths; these overrides never affect the installed application.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tty
import unittest
from unittest import mock

from serial_party import client
from serial_party.client import Serial, SerialBusyError, SerialException, SerialTimeoutException


def config_data(devices=None, ports=None):
    devices = devices or ["/dev/serial/by-id/usb-board-a", "/dev/serial/by-id/usb-board-b"]
    ports = ports or [48200, 48201, 48202, 48203, 48204, 48205]
    return {
        "version": 1,
        "broker_port": ports[0],
        "broker_secondary_port": ports[1],
        "listen_host": "127.0.0.1",
        "writers": ["operator", "hermes", "codex", "webcontrol"],
        "boards": [
            {"id": name, "name": name, "device": device, "baud": baud,
             "monitor_port": ports[2 + i * 2], "control_port": ports[3 + i * 2]}
            for i, (name, device, baud) in enumerate(
                zip(("board-a", "board-b"), devices, (115200, 9600))
            )
        ],
    }


def read_until(connection, size, timeout=2):
    output = bytearray()
    deadline = time.monotonic() + timeout
    while len(output) < size and time.monotonic() < deadline:
        output.extend(connection.read(size - len(output)))
    return bytes(output)


def drain(fd, timeout=0.15):
    output = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            try:
                output.extend(os.read(fd, 65536))
            except OSError:
                break
    return bytes(output)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


class ProtocolTests(unittest.TestCase):
    """Exercise a real child process and raw PTY, independent of host packages."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="serial-party-client-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.data = config_data()
        self.path.write_text(json.dumps(self.data))
        executable = self.root / "console-fixture"
        fixture = Path(__file__).with_name("fake_console.py").read_text()
        executable.write_text(f"#!{sys.executable}\n" + fixture)
        executable.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {"SERIAL_PARTY_TEST_DIRECTORY": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.binary = mock.patch.object(client, "_console_binary", return_value=str(executable))
        self.binary.start()
        self.addCleanup(self.binary.stop)

    def connect(self, board="board-a", **kwargs):
        kwargs.setdefault("timeout", 0.03)
        connection = Serial(board, config=self.path, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def emit(self, board, data):
        with (self.root / f"{board}.rx").open("ab") as output:
            output.write(data)

    def tx(self, board="board-a"):
        path = self.root / f"{board}.tx"
        return path.read_bytes() if path.exists() else b""

    def test_board_selection_and_configured_baud(self):
        with self.connect("/dev/serial/by-id/usb-board-b") as connection:
            self.assertEqual(connection.port, "board-b")
            self.assertEqual(connection.baudrate, 9600)
        for kwargs in ({"port": "missing"}, {"port": "board-b", "baudrate": 115200},
                       {"port": None}, {"port": "board-a", "label": "monitor"},
                       {"port": "board-a", "label": "unconfigured"}):
            with self.subTest(kwargs=kwargs), mock.patch.object(client.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    Serial(config=self.path, **kwargs)
                popen.assert_not_called()

    def test_invalid_settings_fail_before_starting_any_process(self):
        for kwargs in ({"exclusive": False}, {"rtscts": True}, {"parity": "E"},
                       {"timeout": -1}, {"timeout": float("nan")},
                       {"write_timeout": None}, {"write_timeout": float("inf")},
                       {"startup_timeout": 0}):
            with self.subTest(kwargs=kwargs), mock.patch.object(client.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    Serial("board-a", config=self.path, **kwargs)
                popen.assert_not_called()

    def test_writer_preserves_control_bytes_and_flush_preserves_output(self):
        connection = self.connect()
        payload = b"status\r\n\x03\x1b[A\t"
        self.assertEqual(connection.write(payload), len(payload))
        asynchronous = b"new board output\r\n"
        (self.root / "flush-data").write_bytes(asynchronous)
        connection.flush()
        self.assertEqual(self.tx(), payload)
        self.assertEqual(read_until(connection, len(asynchronous)), asynchronous)
        for bad in (b"safe prefix\x1d\x1ef", b"\x1d", b"\x1d\x1ea"):
            with self.assertRaises(ValueError):
                connection.write(bad)
        with self.assertRaises(TypeError):
            connection.write("text must be encoded")
        self.assertEqual(self.tx(), payload)

    def test_observers_are_read_only_and_normal_protocol_like_text_survives(self):
        first = self.connect(read_only=True, label="codex")
        second = self.connect(read_only=True)
        self.assertEqual(first.label, "monitor")
        payload = b"boot\r\n[attached]\r\n[spying]\r\nready\r\n"
        self.emit("board-a", payload)
        self.assertEqual(read_until(first, len(payload)), payload)
        self.assertEqual(read_until(second, len(payload)), payload)
        with self.assertRaises(SerialException):
            first.write(b"must not send\r")
        self.assertEqual(self.tx(), b"")

    def test_busy_fails_promptly_disconnects_and_does_not_wait_for_promotion(self):
        owner = self.connect(label="hermes")
        start = time.monotonic()
        with self.assertRaises(SerialBusyError), mock.patch.object(client.subprocess, "Popen", wraps=subprocess.Popen) as launch:
            self.connect(label="codex")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(launch.call_count, 1)
        owner.close()
        replacement = self.connect(label="operator")
        replacement.write(b"new writer\r")
        replacement.flush()
        self.assertEqual(self.tx(), b"new writer\r")

    def test_startup_and_acquire_uart_text_cannot_override_busy_denial(self):
        owner = self.connect(label="hermes")
        for filename, payload in (
            ("startup-board-data", b"[attached]\r\n"),
            ("acquire-board-data", b"[attached]\r\n[ok]\r\n"),
            # Even a forged in-band fence cannot substitute for ownership:
            # the separate native status request still sees a pending client.
            ("acquire-board-data", b"[attached]\r\n\r\n[version `8.2.7']\r\n"),
        ):
            path = self.root / filename
            path.write_bytes(payload)
            with self.subTest(filename=filename, payload=payload):
                with self.assertRaises(SerialBusyError):
                    self.connect(label="codex")
                self.assertEqual(self.tx(), b"")
            path.unlink()
        owner.close()
        replacement = self.connect(label="codex")
        replacement.write(b"explicit new attempt\r")
        replacement.flush()
        self.assertEqual(self.tx(), b"explicit new attempt\r")

    def test_success_preserves_preexisting_acquire_shaped_board_output(self):
        payload = b"boot\r\n[attached]\r\n[ok]\r\n"
        (self.root / "startup-board-data").write_bytes(payload)
        connection = self.connect()
        self.assertEqual(read_until(connection, len(payload)), payload)
        self.assertEqual(self.tx(), b"")

    def test_same_label_cannot_impersonate_an_existing_writer(self):
        owner = self.connect(label="hermes")
        spoof = self.root / "acquire-board-data"
        spoof.write_bytes(b"[attached]\r\n\r\n[version `8.2.7']\r\n")
        with self.assertRaises(SerialBusyError):
            self.connect(label="hermes")
        self.assertEqual(self.tx(), b"")
        spoof.unlink()
        owner.close()
        self.connect(label="hermes")

    def test_failed_authoritative_status_releases_acquired_writer(self):
        for response in (
            subprocess.CompletedProcess([], 1, b"", b"failure"),
            subprocess.CompletedProcess([], 0, b"unexpected status", b""),
        ):
            with self.subTest(response=response), mock.patch.object(client.subprocess, "run", return_value=response):
                with self.assertRaises(SerialException):
                    self.connect()
        connection = self.connect(label="codex")
        connection.write(b"explicit writer\r")
        connection.flush()
        self.assertEqual(self.tx(), b"explicit writer\r")

    def test_two_boards_have_independent_owners_and_streams(self):
        first = self.connect("board-a", label="hermes")
        second = self.connect("board-b", label="codex")
        self.emit("board-a", b"a boot\r\n")
        self.emit("board-b", b"b boot\r\n")
        self.assertEqual(read_until(first, 8), b"a boot\r\n")
        self.assertEqual(read_until(second, 8), b"b boot\r\n")
        first.write(b"a command\r")
        second.write(b"b command\r")
        first.flush()
        second.flush()
        self.assertEqual(self.tx("board-a"), b"a command\r")
        self.assertEqual(self.tx("board-b"), b"b command\r")
        self.assertEqual(first.read(100), b"")
        self.assertEqual(second.read(100), b"")

    def test_timeout_closes_owned_child_and_context_closes_on_exception(self):
        (self.root / "no-handshake").touch()
        real_popen = subprocess.Popen
        children = []

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        with mock.patch.object(client.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(SerialTimeoutException):
                self.connect(startup_timeout=0.2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        (self.root / "no-handshake").unlink()
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with self.connect() as connection:
                raise RuntimeError("caller failed")
        self.assertFalse(connection.is_open)
        connection.close()
        with self.assertRaises(SerialException):
            connection.read()
        self.connect(label="codex")

    def test_command_ignores_client_config_and_stays_on_loopback(self):
        self.data["listen_host"] = "0.0.0.0"  # Web terminal binding only.
        args = client._console_args(self.data, "codex")
        self.assertEqual(args[1:], ["-n", "-C", "/dev/null", "-E", "-M",
                                    "127.0.0.1", "-p", "48200", "-l", "codex"])
        self.assertNotIn("-f", args)
        self.assertNotIn("-F", args)

    def test_cli_options_work_before_and_after_mode(self):
        for argv in (
            ["--config", str(self.path), "--board", "board-b", "control", "--label", "codex"],
            ["control", "--label", "codex", "--board", "board-b", "--config", str(self.path)],
        ):
            with self.subTest(argv=argv), mock.patch.object(client, "_interactive") as interactive:
                self.assertEqual(client.main(argv), 0)
                interactive.assert_called_once_with(True, "codex", str(self.path), "board-b")
        with mock.patch.object(client.subprocess, "call", return_value=0) as status:
            self.assertEqual(client.main(["--config", str(self.path), "status", "--board", "board-b"]), 0)
            self.assertEqual(status.call_args.args[0][-2:], ["-w", "board-b"])
        with mock.patch.object(client, "_interactive", side_effect=SerialBusyError("owner")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(client.main(["control", "--board", "board-a"]), 2)


class ConserverIntegrationTests(unittest.TestCase):
    """Run the actual daemon against two disposable PTYs, never hardware."""

    @classmethod
    def setUpClass(cls):
        cls.server_binary = os.environ.get("SERIAL_PARTY_TEST_CONSERVER") or shutil.which("conserver")
        if not cls.server_binary and Path("/usr/sbin/conserver").is_file():
            cls.server_binary = "/usr/sbin/conserver"
        cls.console_binary = os.environ.get("SERIAL_PARTY_TEST_CONSOLE") or shutil.which("console")
        if not cls.server_binary or not cls.console_binary:
            message = "Real Conserver integration tests require both 'conserver' and 'console' binaries"
            if os.environ.get("SERIAL_PARTY_REQUIRE_CONSERVER") == "1":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.temp = tempfile.TemporaryDirectory(prefix="serial-party-integration-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.masters = []
        cls.slaves = []
        for _ in range(2):
            master, slave = pty.openpty()
            tty.setraw(slave)
            cls.masters.append(master)
            cls.slaves.append(slave)
            cls.addClassCleanup(os.close, master)
            cls.addClassCleanup(os.close, slave)
        # Hold ephemeral loopback ports while selecting distinct values. Release
        # immediately before launching the isolated daemon (no production ports).
        reservations = []
        try:
            while len(reservations) < 6:
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                if sock.getsockname()[1] < 1024:
                    sock.close()
                    continue
                reservations.append(sock)
            ports = [sock.getsockname()[1] for sock in reservations]
            cls.data = config_data([os.ttyname(fd) for fd in cls.slaves], ports)
            cls.config = cls.root / "config.json"
            cls.config.write_text(json.dumps(cls.data))
            cls.conserver_config = cls.root / "conserver.cf"
            lines = [
                "default * { master localhost; rw operator,hermes,codex,webcontrol; ro monitor; }",
                "access * { trusted 127.0.0.1; }",
            ]
            for board in cls.data["boards"]:
                lines.append(
                    f"console {board['id']} {{ type device; device {board['device']}; "
                    f"baud {board['baud']}; parity none; }}"
                )
            cls.conserver_config.write_text("\n".join(lines) + "\n")
        finally:
            for sock in reservations:
                sock.close()
        cls.log = (cls.root / "daemon.log").open("wb")
        cls.addClassCleanup(cls.log.close)
        cls.server = subprocess.Popen(
            [cls.server_binary, "-E", "-M", "127.0.0.1", "-p", str(ports[0]),
             "-b", str(ports[1]), "-C", str(cls.conserver_config),
             "-L", str(cls.root / "service.log")],
            stdout=cls.log, stderr=cls.log, start_new_session=True,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while True:
            if cls.server.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Isolated Conserver failed to start:\n" + (cls.root / "daemon.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", ports[0]), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        cls.binary_patch = mock.patch.object(client, "_console_binary", return_value=cls.console_binary)
        cls.binary_patch.start()
        cls.addClassCleanup(cls.binary_patch.stop)

    @classmethod
    def stop_server(cls):
        # Only the process group we created is signalled; never a system daemon.
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
        connection = Serial(board, config=self.config, timeout=0.05, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def status(self, board="board-a"):
        return subprocess.check_output(
            client._console_args(self.data, "monitor") + ["-w", board],
            text=True, timeout=3,
        )

    def test_multi_board_streams_and_writers_are_isolated(self):
        a_viewers = [self.connect(read_only=True), self.connect(read_only=True)]
        b_viewer = self.connect("board-b", read_only=True)
        a_writer = self.connect(label="hermes")
        b_writer = self.connect("board-b", label="codex")
        a_payload = b"board A\r\n[attached]\r\n[spying]\r\n"
        b_payload = b"board B\r\n"
        os.write(self.masters[0], a_payload)
        os.write(self.masters[1], b_payload)
        for connection in (*a_viewers, a_writer):
            self.assertEqual(read_until(connection, len(a_payload)), a_payload)
            self.assertEqual(connection.read(64), b"")
        for connection in (b_viewer, b_writer):
            self.assertEqual(read_until(connection, len(b_payload)), b_payload)
            self.assertEqual(connection.read(64), b"")
        a_input = b"A status\r\x03\x1b[A"
        b_input = b"B status\r\n"
        a_writer.write(a_input)
        b_writer.write(b_input)
        a_writer.flush()
        b_writer.flush()
        self.assertEqual(drain(self.masters[0]), a_input)
        self.assertEqual(drain(self.masters[1]), b_input)
        with self.assertRaises(SerialException):
            a_viewers[0].write(b"must not send\r")
        with self.assertRaises(ValueError):
            a_writer.write(b"must not send\x1d\x1ef")
        self.assertEqual(drain(self.masters[0]), b"")

    def test_busy_writer_disconnects_without_queued_promotion(self):
        observer = self.connect(read_only=True)
        owner = self.connect(label="hermes")
        started = time.monotonic()
        with self.assertRaises(SerialBusyError):
            self.connect(label="codex")
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotIn("codex", self.status())
        owner.close()
        self.assertNotIn("codex", self.status())
        replacement = self.connect(label="operator")
        replacement.write(b"owner changed\r")
        replacement.flush()
        self.assertEqual(drain(self.masters[0]), b"owner changed\r")
        os.write(self.masters[0], b"observer survives\r\n")
        self.assertEqual(read_until(observer, 19), b"observer survives\r\n")

    def test_native_monitor_account_cannot_write_even_without_wrapper(self):
        master, slave = pty.openpty()
        tty.setraw(slave)
        process = subprocess.Popen(
            client._console_args(self.data, "monitor") + ["-a", "board-a"],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        try:
            self.assertIn(b"read-only", drain(master, 1))
            os.write(master, b"must not reach board\r")
            self.assertEqual(drain(self.masters[0]), b"")
        finally:
            os.close(master)
            stop_process(process)

    def test_killed_caller_relinquishes_writer(self):
        script = (
            "import sys,time; from unittest.mock import patch; "
            "from serial_party.client import Serial; "
            "p=patch('serial_party.client._console_binary',return_value=sys.argv[2]); p.start(); "
            "s=Serial('board-a',config=sys.argv[1],label='hermes'); "
            "print('READY',flush=True); time.sleep(30)"
        )
        env = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        caller = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(self.config), self.console_binary],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        try:
            self.assertTrue(select.select([caller.stdout], [], [], 8)[0], "Child did not open its console")
            self.assertEqual(caller.stdout.readline(), b"READY\n")
            caller.kill()
            caller.wait(timeout=3)
            # Release happens asynchronously when its orphaned console sees
            # PTY EOF. This test polls ownership, never queues a waiting writer.
            deadline = time.monotonic() + 3
            while "hermes" in self.status() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertNotIn("hermes", self.status())
            self.connect(label="codex")
        finally:
            stop_process(caller)
            caller.stdout.close()
            caller.stderr.close()


if __name__ == "__main__":
    unittest.main()
