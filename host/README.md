# Linux host tooling

Follow [the host installation guide](../docs/host.md). The Python package provides
`serial-party-host validate`, `render`, `install`, `verify`, `rollback`, and the
systemd-only `launch-terminal` command. Nothing uses a physical UART until an
explicit installation activation.

`install-ttyd.py` downloads the upstream **ttyd 1.7.7** binary for Linux aarch64 or
x86_64, checks its published SHA-256 against the pinned digest in this repository,
and installs it only when invoked with `--install` as root. It refuses to overwrite
a different existing executable. It does not start a daemon or open serial ports.

Upstream binary provenance:

- [ttyd 1.7.7 release](https://github.com/tsl0922/ttyd/releases/tag/1.7.7)
- [Release SHA256SUMS](https://github.com/tsl0922/ttyd/releases/download/1.7.7/SHA256SUMS)
- [ttyd MIT license](https://github.com/tsl0922/ttyd/blob/1.7.7/LICENSE)

Prefer your distribution's ttyd package when its help includes `--writable`,
`--base-path`, and `--max-clients`. Debian 12's older ttyd uses different write
semantics; the host installer rejects it instead of accidentally enabling input
on a monitor port.
