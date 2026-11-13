"""Subprocess protocol fixture; never opens a serial device or network socket.

Tests copy this into a temporary executable. This is intentionally not a
replacement for the real-Conserver integration tests in test_client.py.
"""

import fcntl
import json
import os
from pathlib import Path
import select
import sys
import time


root = Path(os.environ["SERIAL_PARTY_TEST_DIRECTORY"])
board = sys.argv[-1]
label = sys.argv[sys.argv.index("-l") + 1]
if "-i" in sys.argv:
    users = []
    for record in root.glob("client-*"):
        try:
            value = json.loads(record.read_text())
            os.kill(value["pid"], 0)
        except (ValueError, KeyError, OSError):
            continue
        if value["board"] == board:
            state = "w" if value["writer"] else "r"
            users.append(f"{state}@{value['label']}@localhost@0" + ("" if value["writer"] else "@ro"))
    fields = [board, "localhost,1,2", "/", "/dev/fixture,115200n,3", ",".join(users),
              "up", "rw", "log", "1", "autoup", "", "", "", "", ""]
    print(":".join(fields))
    sys.exit(0)
lock = (root / f"{board}.lock").open("ab")
rx_path = root / f"{board}.rx"
tx_path = root / f"{board}.tx"
rx_offset = rx_path.stat().st_size if rx_path.exists() else 0
writer = False
pending = bytearray()
version_count = 0
record = root / f"client-{os.getpid()}"


def save_state():
    record.write_text(json.dumps({"pid": os.getpid(), "board": board, "label": label, "writer": writer}))


save_state()

if (root / "no-handshake").exists():
    time.sleep(20)
    sys.exit(0)

os.write(1, b"[Enter `^]^^?' for help]\r\n[spying]")
try:
    while True:
        ready = select.select([0], [], [], 0.01)[0]
        if ready:
            data = os.read(0, 65536)
            if not data:
                break
            pending.extend(data)
            while pending:
                if pending[0] == 0x1D:
                    if len(pending) < 3:
                        break
                    command = bytes(pending[:3])
                    del pending[:3]
                    if command == b"\x1d\x1ev":
                        version_count += 1
                        if version_count == 1 and (root / "startup-board-data").exists():
                            os.write(1, (root / "startup-board-data").read_bytes())
                        if version_count > 1 and (root / "flush-data").exists():
                            os.write(1, (root / "flush-data").read_bytes())
                        os.write(1, b"\r\n[version `8.2.7']\r\n")
                    elif command == b"\x1d\x1ea":
                        if (root / "acquire-board-data").exists():
                            os.write(1, (root / "acquire-board-data").read_bytes())
                        if label == "monitor":
                            os.write(1, b"\r\n[read-only]\r\n")
                        else:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                writer = True
                                save_state()
                                os.write(1, b"\r\n[attached]\r\n")
                            except BlockingIOError:
                                os.write(1, b"\r\n[no, owner@fixture is attached]\r\n")
                    else:
                        raise RuntimeError("Unexpected broker escape")
                else:
                    end = pending.find(0x1D)
                    end = len(pending) if end < 0 else end
                    data = bytes(pending[:end])
                    del pending[:end]
                    if writer:
                        with tx_path.open("ab") as output:
                            output.write(data)
        if rx_path.exists():
            with rx_path.open("rb") as source:
                source.seek(rx_offset)
                data = source.read()
            if data:
                rx_offset += len(data)
                os.write(1, data)
finally:
    lock.close()
    record.unlink(missing_ok=True)
