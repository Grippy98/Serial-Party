# Home Assistant app and dashboard

Serial Party connects Home Assistant to the terminal services on your Linux
serial host. The app does not install Conserver or open USB adapters itself.
Complete the [host setup](host.md) first.

## Requirements

- Home Assistant **2026.11 or later**, with Supervisor app support, and an
  administrator account. Home Assistant Container alone cannot install this app.
- An amd64 or aarch64 Home Assistant machine. Installation builds the app image
  from this repository; the build needs access to the Python container registry
  and PyPI. Runtime connections stay between Home Assistant and your serial host.
- A Linux serial host running the configured Conserver broker and authenticated
  monitor/control ttyd services for each board.
- Network access from Home Assistant to every configured terminal port. For a
  separate host, choose its LAN address as `listen_host` during host setup and
  restrict those ports to Home Assistant with your firewall. The default host
  binding, `127.0.0.1`, is deliberately unreachable from another machine.

Keep the Conserver ports on loopback. The app talks only to ttyd's HTTP ports;
it does not need direct access to the broker. The upstream HTTP connection uses
Basic authentication, so use a trusted LAN or a protected transport. See the
[access model](security.md).

## Install the app

1. In Home Assistant, open **Settings → Apps → App store**.
2. Open the top-right menu, choose **Repositories**, and add
   `https://github.com/Grippy98/Serial-Party`.
3. Find **Serial Party** in that repository and select **Install**. Wait for the
   local image build to finish.
4. Open the app's **Configuration** tab and set the options below before
   starting it. The empty default password intentionally cannot start a service.
5. Save, start the app, and select **Open web UI**. You may also enable its sidebar
   entry. No Home Assistant Core restart is required to start the app.

This is an app-store repository, not a dashboard resource repository. The card
JavaScript is installed separately in the next section. Home Assistant's
[repository guide](https://contributors.home-assistant.io/apps/guides/repository/)
describes the app-store mechanism.

```yaml
upstream_host: 192.0.2.10
upstream_username: serialparty
upstream_password: "REPLACE_WITH_YOUR_SERVICE_PASSWORD"
boards:
  - id: board-a
    name: Development Board A
    monitor_port: 7681
    control_port: 7682
  - id: sensor-node
    name: Sensor Node
    monitor_port: 7683
    control_port: 7684
```

`192.0.2.10` is a documentation placeholder: replace it with your host's reachable
IP address or DNS name, without `http://`, a port, or a path. All boards in this
app instance use that host and one service credential. The managed host installer
stores its generated credential in the root-only
`/etc/serial-party/web.env`. Enter the username and password privately in the app
options; do not paste the complete environment assignment or publish that file.

Use the same board IDs and port assignments as the host configuration:

| Option | Accepted value |
| --- | --- |
| `upstream_host` | Hostname or IP address; no URL |
| `upstream_username` / `upstream_password` | Nonempty Basic credential matching ttyd |
| `boards` | Between 1 and 16 board objects |
| `id` | 1–32 characters: starts with a lowercase letter, then lowercase letters, digits, `_` or `-` |
| `name` | Printable display name, 1–80 characters |
| `monitor_port` / `control_port` | Unique integer ports, 1024–65535 |

Every terminal port must be distinct, including the two ports for one board.
On the host, these must also differ from the broker ports. Baud rates and USB
adapter paths belong in the host configuration, not these app options.

The app exposes no LAN ports and needs no privileged mode, USB passthrough,
Supervisor API token, Home Assistant API permission, or separate browser login.
Home Assistant authenticates ingress access. Upstream credentials stay in the
server-side proxy and are not returned to the browser.

## Add a live dashboard card

1. Copy [`dashboard/serial-party-card.js`](../dashboard/serial-party-card.js) to
   `/config/www/serial-party-card.js` on Home Assistant. Create `www` if needed.
   Use your normal file editor or file-transfer method.
2. Open **Settings → Dashboards → top-right menu → Resources → Add resource**.
   Enter `/local/serial-party-card.js?v=0.1.0` and choose **JavaScript module**.
   Reload the browser after saving. This follows Home Assistant's
   [resource installation instructions](https://contributors.home-assistant.io/frontend/custom-ui/resources/).
3. Find the installed app's actual slug in its Settings page URL. It ends in
   `_serial_party` and includes a repository prefix. Do not use the example slug
   or copy an `/api/hassio_ingress/…` URL.
4. Edit your dashboard, add a manual card, and insert:

```yaml
type: custom:serial-party-card
app_slug: REPLACE_WITH_INSTALLED_APP_SLUG
board: board-a
title: Development Board A
height: 360
font_size: 12
```

Replace `app_slug` with the actual lowercase slug. A locally installed developer
copy normally uses `local_serial_party`; a repository install has its own prefix.
`board` must match the app options exactly. Add another card with a different
board ID to show multiple boards together. Height defaults to 360 pixels
(accepted range 260–1200); font size defaults to 12 (range 8–32).

The card creates its ingress session at runtime using the current administrator's
Home Assistant connection. It never stores an ingress token, password, or device
address in dashboard configuration. It unloads the observer when the page is
hidden or the card is removed, and resumes read-only when you return. **Reconnect**
reopens a failed feed without requesting control.

**Open controls** opens the stable `/app/<installed-slug>` Home Assistant page.
Select the desired board in the app dropdown. The app starts read-only; **Request
control** attempts to acquire that board's writer without taking it from another
controller. **Return to monitor**, changing boards, or leaving the page closes
that control session. A busy attempt ends and must be retried deliberately.

## Check and troubleshoot

Open a board's read-only view first. Existing output or new boot/status messages
should appear without typing anything. A quiet device can produce a blank
terminal; viewing a console does not send a newline or any probe command.

| Symptom | Check |
| --- | --- |
| App stops immediately | Check its logs for invalid options. Set a nonempty password, valid board IDs, and unique ports. |
| Terminal unavailable | Check the host's ttyd services, LAN binding, firewall, board ports, and matching credential. Do not disable authentication. |
| Unknown board | Match the card's `board` to an app option ID, including case. |
| Administrator required | Use a Home Assistant administrator account; both the app and current card intentionally require it. |
| Custom element not found | Check the resource URL/module type, browser cache, and that the file is under `/config/www/`. |
| Session expired or disconnected | Use **Reconnect** after restoring Home Assistant or host connectivity. |
| Control reports busy | The current writer keeps ownership. Close the failed terminal and retry only after that writer releases it. |

When updating the JavaScript, replace the file and change its resource query
version to refresh browser caches. App updates and host package updates are
separate operations; follow the [migration guide](migration.md) before changing
running host services.

For a hardware-free preview, see the synthetic demo in
[CONTRIBUTING.md](../CONTRIBUTING.md). The screenshots in this repository use that
demo rather than a real Home Assistant session or private board output.
