# CLI and agent access

Run clients on the serial host, usually through an authenticated SSH connection.
Conserver's listener stays on loopback; do not expose it directly to the network.

## Observe without claiming the keyboard

```sh
serial-party-console --config /etc/serial-party/config.json --board board-a monitor
serial-party-console --board board-a status
```

Monitoring is passive. It does not send a newline, probe a shell, reset a target,
or change boot state. A quiet console can be healthy.

## Request control

```sh
serial-party-console --board board-a control --label operator
```

Only a configured writer label may request control. A competing writer causes an
immediate failure: the client closes instead of waiting to be promoted later.
Use **Ctrl-]** to exit and release control. Never use Conserver's force options.

Writer ownership is per board. Controlling `board-a` does not reserve `board-b`.
Labels identify sessions for coordination; they are not authenticated identities.
Use SSH and operating-system access controls to decide who may run clients.

## Python

```python
from serial_party import Serial, SerialBusyError

# Read-only, even if someone else controls the board.
with Serial("board-a", read_only=True, timeout=0.2) as monitor:
    output = monitor.read(4096)

# A deliberate writer session; this example acquires and releases without input.
try:
    with Serial("board-a", label="codex", timeout=0.2, write_timeout=2) as console:
        output = console.read(4096)
except SerialBusyError:
    print("Console is busy; retry deliberately later.")
```

The board's configured baud rate is used when `baudrate` is omitted. An explicit
conflicting rate is rejected: clients cannot silently reconfigure an active UART.
Select a board by its ID or by its exact configured device path. Omitting the
selector is supported only when the configuration has one board.

Supported operations are `read`, `write`, `flush`, `close`, and context-manager
use. This is not a complete pySerial replacement. In particular, it does not
provide DTR/RTS manipulation, baud changes, binary transfers, or arbitrary device
opening. Byte `0x1d` is reserved and cannot be written as target data.

`flush()` establishes a broker-processing boundary. Wait for the target's own
prompt or response when command completion matters.

## Instructions for automation tools

Use this contract in a project instruction file:

> Use Serial Party for all console access. Select the intended board explicitly.
> Start as a read-only observer when inspecting state. Acquire a polite writer
> only for an authorized target operation and release it promptly. If busy, stop
> the attempt and retry later; never force, queue, use raw pySerial, or bypass
> ownership with sudo. Identify the current prompt before sending commands or
> credentials. Do not reset, change boot media, or cycle power merely to test
> connectivity. Board power management is outside this console interface.

## Migrating a small pySerial script

For scripts that use only the supported text-console subset, replace the import
and select a configured board:

```python
import serial_party as serial

with serial.Serial("board-a", timeout=0.2, write_timeout=2, label="hermes") as port:
    response = port.read(4096)
```

Pause and close the original raw-port script before testing the replacement.
Keep the original script and inspect the change; a matching import alone does
not make unsupported pySerial methods safe or available.
