"""Shared text consoles with one polite writer and any number of observers.

This is a small pySerial-shaped API, not a drop-in pySerial implementation.
It talks only to a local Conserver broker and never opens a hardware device.
Board IDs and exact configured device paths both select a configured console.

Ctrl-] (0x1d) is reserved for the broker control channel. ``write()`` rejects
the entire payload if it contains that byte; binary transfers are unsupported.
``flush()`` is a broker-processing barrier, not proof of target execution or
hardware UART drain. All hardware clients must use the broker.
"""

from __future__ import annotations

import argparse
import errno
import math
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty

from .config import get_board, load_config

EIGHTBITS = 8
PARITY_NONE = "N"
STOPBITS_ONE = 1
_ESCAPE = b"\x1d\x1e"
_BANNER = re.compile(rb"\[Enter `\^\]\^\^\?' for help\]\r?\n")
_VERSION = re.compile(rb"(?:\r?\n)?\[version `[^\r\n]*'\]\r?\n")
_ATTACH = re.compile(
    rb"(?:\r?\n)?\[(attached(?: \(nologging\))?|ok(?: \(nologging\))?"
    rb"|no, [^\r\n]* is attached|read-only|console is read-only)\](?:\r?\n|$)"
)


class SerialException(OSError):
    """The broker is unavailable or the console connection was lost."""


class SerialBusyError(SerialException):
    """Another client owns the writer; this caller has disconnected."""


class SerialTimeoutException(SerialException):
    """A bounded broker operation timed out."""


def _console_binary() -> str:
    binary = shutil.which("console")
    if binary is None:
        raise SerialException("Conserver's 'console' client is not installed or not on PATH")
    return binary


def _console_args(config: dict, label: str) -> list[str]:
    if label != "monitor" and label not in config["writers"]:
        raise ValueError("label must be one of the configured writer labels")
    # -n and -C prevent user/system console configuration from adding attach
    # commands. -E is safe only because both broker ports are loopback-bound.
    # config.listen_host controls the web terminals, never this broker address.
    return [
        _console_binary(), "-n", "-C", "/dev/null", "-E", "-M", "127.0.0.1",
        "-p", str(config["broker_port"]), "-l", label,
    ]


def _timeout(value, name: str, *, allow_none=False, allow_zero=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'} and finite")
    return value


class Serial:
    """An exclusive writer, or a read-only observer with ``read_only=True``.

    ``config`` is a JSON config file path (or None for the default path).
    ``port`` is a board ID or its exact configured device path. Omit it only
    when the config contains one board. The server owns UART settings:
    ``baudrate=None`` adopts the configured baud; an explicit mismatch fails.
    Only 8N1 without flow control is supported. Writers never force ownership
    and a busy attempt disconnects immediately instead of waiting in a queue.
    """

    def __init__(
        self, port=None, baudrate=None, timeout=0.2, write_timeout=2,
        exclusive=True, bytesize=EIGHTBITS, parity=PARITY_NONE,
        stopbits=STOPBITS_ONE, *, label="operator", config=None,
        read_only=False, startup_timeout=5.0, xonxoff=False, rtscts=False,
        dsrdtr=False,
    ):
        self.is_open = False
        self._fd = -1
        self._process = None
        self._rx = bytearray()
        self._lock = threading.RLock()
        self._config = load_config(config)
        board = get_board(self._config, port)
        if baudrate is not None and baudrate != board["baud"]:
            raise ValueError(f"The configured baud for {board['id']} is {board['baud']}")
        if (bytesize, parity, stopbits) != (8, "N", 1):
            raise ValueError("Only the broker's configured 8N1 console is supported")
        if xonxoff or rtscts or dsrdtr or exclusive is not True:
            raise ValueError("Flow control and non-exclusive writers are unsupported")
        self.timeout = _timeout(timeout, "timeout", allow_none=True, allow_zero=True)
        self.write_timeout = _timeout(write_timeout, "write_timeout")
        startup_timeout = _timeout(startup_timeout, "startup_timeout")
        self.port = board["id"]
        self.baudrate = board["baud"]
        self.read_only = bool(read_only)
        self.label = "monitor" if self.read_only else label
        if not self.read_only and self.label not in self._config["writers"]:
            raise ValueError("label must be one of the configured writer labels")
        if not self.read_only and self.label == "monitor":
            raise ValueError("The monitor account cannot request write access")
        command = _console_args(self._config, self.label)
        command += ["-k", "-e", "^]^^", "-s", self.port]
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            os.set_blocking(master, False)
            self._process = subprocess.Popen(
                command, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True,
                env=dict(os.environ, LC_ALL="C", TERM="dumb"),
            )
        except OSError as error:
            os.close(master)
            raise SerialException(f"Could not start the Conserver client: {error}") from error
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._fd = master
        self.is_open = True
        try:
            deadline = time.monotonic() + startup_timeout
            self._expect(_BANNER, deadline, discard_before=True)
            # Fence the client's initial spy command before requesting control.
            self._send_raw(_ESCAPE + b"v", deadline)
            self._expect(_VERSION, deadline)
            # Only startup protocol chatter is removed. Identical text from a
            # running board must remain visible to observers.
            self._rx[:] = re.sub(
                rb"(?:\r?\n)?\[spying\](?:\r?\n)?", b"", self._rx, count=1
            )
            if not self.read_only:
                self._acquire(deadline)
        except BaseException:
            self.close()
            raise

    def _acquire(self, deadline):
        # Pre-existing board output is not a reply to our acquire request.
        earlier_output = bytes(self._rx)
        self._rx.clear()
        self._send_raw(_ESCAPE + b"a" + _ESCAPE + b"v", deadline)
        fence = self._expect(_VERSION, deadline)
        response = fence.string[:fence.start()]
        del self._rx[:fence.start()]
        answers = list(_ATTACH.finditer(response))
        for answer in answers:
            if not answer.group(1).startswith((b"attached", b"ok")):
                # Never accept the first success-shaped line: UART text could
                # precede the real denial. Close instead of remaining queued.
                raise SerialBusyError(answer.group(1).decode("utf-8", "replace"))
        if not answers:
            raise SerialException("Conserver did not acknowledge writer acquisition")
        # Text responses share the UART output stream. Confirm ownership using
        # a separate status request before returning any writer to the caller.
        self._verify_writer(deadline)
        answer = answers[-1]
        preserved = response[:answer.start()] + response[answer.end():]
        self._rx[:0] = earlier_output + preserved

    def _verify_writer(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SerialTimeoutException("Writer ownership verification timed out")
        try:
            result = subprocess.run(
                _console_args(self._config, "monitor") + ["-i", self.port],
                capture_output=True, timeout=remaining,
            )
        except subprocess.TimeoutExpired as error:
            raise SerialTimeoutException("Writer ownership verification timed out") from error
        if result.returncode:
            raise SerialException("Could not verify writer ownership with the broker")
        try:
            fields = result.stdout.decode("utf-8").strip().split(":")
        except UnicodeDecodeError as error:
            raise SerialException("Invalid broker ownership status") from error
        if len(fields) != 15 or fields[0] != self.port:
            raise SerialException("Invalid broker ownership status")
        sessions = [value.split("@") for value in fields[4].split(",") if value]
        matching = [value for value in sessions if len(value) >= 2 and value[1] == self.label]
        # The native status has no session ID. Multiple clients with the same
        # label are therefore ambiguous and must fail closed, even if one writes.
        if len(matching) != 1 or len(matching[0]) != 4 or matching[0][0] != "w":
            raise SerialBusyError("Broker did not confirm unambiguous writer ownership")

    def _receive(self, deadline):
        if not self.is_open:
            raise SerialException("Console connection is closed")
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        if not select.select([self._fd], [], [], remaining)[0]:
            return b""
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as error:
            if error.errno == errno.EIO:
                self.close()
                raise SerialException("Conserver console disconnected") from None
            raise
        if not data:
            self.close()
            raise SerialException("Conserver console disconnected")
        return data

    def _expect(self, pattern, deadline, discard_before=False):
        while True:
            # The match must own immutable bytes: deleting from a bytearray
            # otherwise changes a match's captured groups after the fact.
            match = pattern.search(bytes(self._rx))
            if match:
                if discard_before:
                    del self._rx[:match.end()]
                else:
                    del self._rx[match.start():match.end()]
                return match
            if len(self._rx) > 1024 * 1024:
                raise SerialException("Unexpectedly large console protocol response")
            if time.monotonic() >= deadline:
                raise SerialTimeoutException("Conserver did not complete the console handshake")
            self._rx.extend(self._receive(deadline))

    def _send_raw(self, data, deadline):
        view = memoryview(data)
        while view:
            if not self.is_open:
                raise SerialException("Console connection is closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self._fd], [], remaining)[1]:
                raise SerialTimeoutException("Conserver write timed out")
            try:
                count = os.write(self._fd, view)
            except BlockingIOError:
                continue
            except OSError as error:
                self.close()
                raise SerialException("Conserver console disconnected during write") from error
            if not count:
                raise SerialException("Conserver console accepted no input")
            view = view[count:]

    def read(self, size=1):
        """Return up to size bytes using the configured read timeout."""
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("size must be a non-negative integer")
        if not self.is_open:
            raise SerialException("Console connection is closed")
        if size == 0:
            return b""
        with self._lock:
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while len(self._rx) < size:
                data = self._receive(deadline)
                if data:
                    self._rx.extend(data)
                if deadline is not None and time.monotonic() >= deadline:
                    break
            value = bytes(self._rx[:size])
            del self._rx[:size]
            return value

    def write(self, data):
        """Send text-console bytes, refusing reserved escapes before any send."""
        if not self.is_open:
            raise SerialException("Console connection is closed")
        if self.read_only:
            raise SerialException("Read-only monitor cannot send serial input")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("write() requires bytes, bytearray, or memoryview")
        data = bytes(data)
        if b"\x1d" in data:
            raise ValueError("Ctrl-] (0x1d) is reserved; binary transfers are unsupported")
        with self._lock:
            self._send_raw(data, time.monotonic() + self.write_timeout)
        return len(data)

    def flush(self):
        """Wait for the broker to parse preceding input, preserving output."""
        with self._lock:
            deadline = time.monotonic() + max(2.0, self.write_timeout)
            self._send_raw(_ESCAPE + b"v", deadline)
            self._expect(_VERSION, deadline)

    def close(self):
        """Disconnect this client and relinquish only its own ownership."""
        self.is_open = False
        fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            else:
                process.wait()
            self._process = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _interactive(control, label, config, board):
    previous = termios.tcgetattr(0) if os.isatty(0) else None
    connection = None
    try:
        connection = Serial(
            port=board, label=label, config=config, read_only=not control, timeout=0
        )
        if previous is not None:
            tty.setraw(0)
        mode = "CONTROL" if control else "READ ONLY"
        os.write(2, f"[{mode}: {connection.port}; Ctrl-] releases and exits]\r\n".encode())
        while True:
            ready = select.select([0, connection._fd], [], [], 1)[0]
            if connection._rx or connection._fd in ready:
                output = connection.read(65536)
                if output:
                    os.write(1, output)
            if 0 in ready:
                data = os.read(0, 4096)
                if not data or b"\x1d" in data:
                    return
                if control:
                    connection.write(data)
    finally:
        if connection is not None:
            connection.close()
        if previous is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, previous)


def main(argv=None):
    """CLI entry point; options may precede or follow monitor/control/status."""
    parser = argparse.ArgumentParser(description="Serial Party shared text console")
    parser.add_argument("mode", choices=("monitor", "control", "status"))
    parser.add_argument("--config", help="path to the Serial Party JSON configuration")
    parser.add_argument("--board", help="configured board ID or exact device path")
    parser.add_argument("--label", default="operator", help="configured writer label")
    args = parser.parse_args(argv)
    old_handlers = {}

    def stop(_signum, _frame):
        raise SystemExit(0)

    try:
        if args.mode == "status":
            config = load_config(args.config)
            board = get_board(config, args.board)
            return subprocess.call(
                _console_args(config, "monitor") + ["-w", board["id"]], timeout=10
            )
        for signum in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, stop)
        _interactive(args.mode == "control", args.label, args.config, args.board)
    except SerialBusyError as error:
        print(f"Serial control is busy: {error}. No control was taken.", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Serial console unavailable: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
