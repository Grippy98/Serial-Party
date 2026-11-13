# Serial Party app

An ingress-only Home Assistant app for serial consoles hosted on a separate
machine. The host owns the serial devices through a shared console broker;
opening this app never opens a second reader on a physical serial device.

Requires **Home Assistant 2026.11 or later**, with Supervisor apps support.
The app image supports amd64 and aarch64. It needs no Supervisor API access,
Home Assistant API access, host networking, published ports or device privileges.

## Configure

Set the host address and a service credential matching the host's ttyd services.
The example address below is reserved for documentation and must be replaced.

```yaml
upstream_host: 192.0.2.10
upstream_username: serialparty
upstream_password: "YOUR_SERVICE_PASSWORD"
boards:
  - id: board-a
    name: Development Board
    monitor_port: 7681
    control_port: 7682
  - id: sensor-node
    name: Sensor Node
    monitor_port: 7683
    control_port: 7684
```

All boards use the same host and HTTP Basic credential. Every port must be a
unique integer from 1024 to 65535, including monitor/control ports on the same board.
IDs must start with a lowercase letter and contain only lowercase
letters, digits, hyphens or underscores, with a maximum of 32 characters.
Names are display labels. Configure between 1 and 16 boards.

For each board, its monitor ttyd service uses `--base-path /monitor` and omits
`-W`; the control service uses `--base-path /control` and `-W`. Both use the
configured HTTP Basic credential. Set `disableLeaveAlert=true` on both, and
`disableReconnect=true` on control. The control command must acquire the broker's
writer **without forcing**, fail when busy, and release it on disconnect.

The app rewrites `/boards/<id>/monitor/` and `/boards/<id>/control/` to these
service paths. Unknown boards cannot select arbitrary hosts or ports.

## Use

The initial view is read-only. Choose a board in the dropdown. **Request control**
opens its separate control session. **Return to monitor**, changing boards, or
leaving the page unloads that session and releases the writer. The backend's
single-writer policy preserves the current controller.

The dashboard card in `dashboard/` embeds only a read-only monitor. Its
**Open controls** link opens this app; choose the board using the dropdown.
The link does not rely on undocumented app-route deep linking.

Home Assistant authenticates ingress requests. The app accepts only the actual
Supervisor peer `172.30.32.2`; forwarding headers cannot bypass this guard.
The browser receives neither the upstream password nor ttyd's Basic token.
The proxy supplies Basic authentication and injects the token into the initial
upstream WebSocket message. Monitor input is also discarded by the proxy.
`/api/boards` returns display IDs/names only.

## Test locally

From this directory, in a virtual environment with `requirements.txt` installed:

```sh
python -m unittest discover -s tests -v
```

Tests use loopback mock servers; they never contact physical serial devices.

For screenshots or a UI preview, run `python tests/demo_server.py` and open
`http://127.0.0.1:8815/`. The dashboard example is at
`http://127.0.0.1:8815/demo/dashboard`. Both use **simulated boards and output**,
not a Home Assistant login or hardware connection. The demo listens only on
loopback. Its fixed demo credential is deliberately public and is not suitable
for a real service. Stop it with Ctrl-C when finished.

Primary references: [HA ingress](https://contributors.home-assistant.io/apps/guides/ingress/),
[ttyd 1.7.7 protocol](https://github.com/tsl0922/ttyd/blob/1.7.7/src/protocol.c).
