# Linux serial host

Serial Party runs on the Linux machine physically connected to your USB serial
adapters. The first release supports systemd on Debian 12+/Ubuntu, Python 3.11+,
and up to 16 boards per host. Each UART has one Conserver owner. Every observer
and writer connects through that broker.

The packaged host installer supports a **fresh installation**, not in-place
replacement of an existing deployment. A running serial console must first be
released by its human or agent owner. See [migration](migration.md) before moving
a board from another service. Installation never kills an unknown UART owner.

## 1. Install prerequisites and the package

Install these distro packages on the serial host. If Conserver is already in
use, coordinate its consoles before changing packages or services.

```sh
sudo apt-get update
sudo apt-get install conserver-server conserver-client ttyd acl psmisc iproute2 python3-venv
```

The distro may start `conserver-server.service`. Serial Party uses its own service
and refuses activation while the distro service is active. On a **new installation
with no existing consoles**, disable that unused distro service:

```sh
sudo systemctl disable --now conserver-server.service
```

Do not run that command against an existing console system until its users have
released their sessions. The installer makes no changes to the distro service.

Clone this repository and install into the path used by the generated units:

```sh
git clone https://github.com/Grippy98/Serial-Party.git
cd Serial-Party
sudo python3 -m venv /opt/serial-party/venv
sudo /opt/serial-party/venv/bin/python -m pip install .
```

Check your ttyd capabilities:

```sh
ttyd --help
```

It must support `--writable`, `--base-path`, and `--max-clients`; ttyd 1.7.7 is the
pinned known-compatible release. If the distro version is older, the optional
helper downloads the official aarch64 or x86_64 release and verifies its pinned
SHA-256 before installation:

```sh
sudo python3 host/install-ttyd.py --install
```

For this helper use `--ttyd /usr/local/bin/ttyd` in the render/install commands
below. If your compatible distro executable is `/usr/bin/ttyd`, use that path
instead. No upstream binary is included in this repository.

## 2. Describe your boards

```sh
cp examples/config.json config.local.json
ls -l /dev/serial/by-id/
```

Edit `config.local.json`: remove the second example if you have one board, choose
short stable IDs and friendly names, copy each adapter's exact stable path, and
set its baud rate and two unused TCP ports. Do not put passwords in this file.

`listen_host` controls only HTTP terminals. Keep `127.0.0.1` for a local reverse
proxy, or set the serial host's LAN address when the Home Assistant app runs on
another machine. Allow access to terminal ports only from Home Assistant and
trusted management clients. The Conserver broker always binds `127.0.0.1`,
regardless of this setting. An example LAN address is `192.0.2.10`; replace it
with your actual host address.

The two broker ports and every monitor/control port must be distinct. IDs use
lowercase letters, digits, `_` and `-`, starting with a letter. At most 16 boards
fit in the single configured Conserver child group. Managed installation requires
USB `/dev/serial/by-id/` links with discoverable USB identities. Generic `/dev/`
paths are accepted for configuration/rendering and isolated PTY testing, but
are deliberately refused by the managed physical-device installer.

## 3. Validate and review without opening serial ports

```sh
/opt/serial-party/venv/bin/serial-party-host validate --config config.local.json
/opt/serial-party/venv/bin/serial-party-host render --config config.local.json --output preview --ttyd /usr/local/bin/ttyd
```

Review the generated `preview/etc/` tree. It contains the Conserver configuration,
systemd units, exact-device udev rules, and log rotation. The preview contains no
credentials. Rendering does not contact the broker, open UARTs, start services,
or change device permissions. Render into a new or empty directory each time.

## 4. Coordinate an idle period, then activate

Tell every agent and operator to close raw serial readers and writers, including
`screen`, `minicom`, pyserial scripts, and existing serial brokers. Monitoring
through a second direct raw-device reader is unsafe too: it can consume bytes
the other reader needed. Ensure no automated job will reopen the UART during
activation.

Only after that handoff:

```sh
sudo /opt/serial-party/venv/bin/serial-party-host install --config config.local.json --ttyd /usr/local/bin/ttyd --activate --confirm-idle
```

The installer refuses occupied UARTs, duplicate links to one physical device,
occupied ports, existing managed files/unit names, an active or enabled distro broker, and
unsupported ttyd versions. It creates a private timestamped baseline under
`/var/lib/serial-party/backups/` before changes, and records each adapter's USB
identity, owner, mode and ACL. On an activation error it attempts a narrow rollback
and reports the baseline path if human intervention is needed.

Raw UARTs become mode `0600`, owned by the `serialparty` service account. Exact
udev rules preserve that ownership after reconnect. Web terminals run as
`www-data` with a private `/dev` and cannot open raw adapters. The installer
starts and enables only its `serial-party-*.service` units.

A random HTTP credential is generated directly into
`/etc/serial-party/web.env`, root-owned mode `0600`. Installation output never
prints it. Read that file locally with root privileges when configuring the Home
Assistant app. Remove the `SERIAL_PARTY_WEB_CREDENTIAL=` prefix first, then enter
the part before `:` as `username` and the part after it as `password`. Do not
commit the file or paste it into logs. This credential
is shared by the board terminals on this host; it is unrelated to Linux login
passwords. ttyd accepts the credential as a process argument, so the host itself
must be trusted. The launcher sets ttyd to error-only logging (`-d 1`), because
ttyd's default NOTICE startup messages include the reversible Base64 credential.
Keep that setting when troubleshooting; verbose terminal startup logs are not
safe to publish. Terminal HTTP uses Basic authentication; use a trusted LAN or a
TLS reverse proxy, not a public port forward.

Configure the [Home Assistant app](../serial_party_app/README.md) with matching board IDs and
ports. Agents use SSH to run the packaged client **on the serial host**:

```sh
/opt/serial-party/venv/bin/serial-party-console --board board-a monitor
/opt/serial-party/venv/bin/serial-party-console --board board-a control --label operator
```

## Verify and inspect

```sh
sudo /opt/serial-party/venv/bin/serial-party-host verify
systemctl status serial-party-conserver.service
systemctl status serial-party-board-a-monitor.service
journalctl -u serial-party-conserver.service -n 50
```

`verify` checks unit state, physical-device ownership, absence of named user ACLs,
Conserver ownership of each raw UART, loopback-only broker ports, and expected
terminal listeners. It does not send input to the board. Per-board RX and session
activity logs are in `/var/log/serial-party/`; target echo may include commands,
but these are not independent bidirectional TX audit logs. Rotation retains seven
files with a 10 MB threshold checked by the host's logrotate schedule.

## Rollback, upgrades and adding boards

The first release does not perform a live upgrade. Prepare and validate a new
configuration before the maintenance window. Ask every writer to disconnect and
pause all agents, schedulers, and users that might start a new control connection.
Keep that pause in place until rollback/reinstallation finishes. `--confirm-idle`
confirms this coordinated maintenance window, not just an instantaneous idle
check. Then roll back using the exact backup path printed at
installation:

```sh
sudo /opt/serial-party/venv/bin/serial-party-host rollback --backup /var/lib/serial-party/backups/TIMESTAMP --confirm-idle
```

Rollback checks for and refuses an active or pending Conserver writer, unexpected raw-device
owners, changed USB identities, and modified managed files. It stops only its
own units, removes only files whose hashes match the installation baseline,
reloads udev, and restores original owner/mode/ACL for every configured adapter.
All adapters must be present for this automatic restore. Unknown devices are
never substituted for missing adapters. An error is reported without resetting
the board, killing an unknown process, or taking over another serial service.
The deployment lock excludes other management commands; it does **not** lock out
new clients. A client connecting after the writer check can be disconnected when
services stop. The required pause of all launchers prevents that race. This is a
coordinated maintenance tool, not a hot-swap or an access-control boundary against
other trusted local users.

The service account, captured logs, backup directory, empty configuration directory,
and Python environment are
retained. The prior system's separate broker service is not restarted for you.
After rollback, install the desired package version and run the fresh-install
steps with the revised configuration; this is also how to add or remove boards.
Update the Home Assistant board list and dashboard card to match.

Root users and other trusted host accounts can bypass application-level ownership
rules. See [security](security.md) and [agent access](agents.md) for that boundary.
