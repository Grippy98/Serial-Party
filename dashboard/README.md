# Read-only dashboard card

Requires Home Assistant **2026.11 or later** and the running Serial Party app.
Copy `serial-party-card.js` into Home Assistant's `www/` directory and add it as
a JavaScript module dashboard resource, for example:

```yaml
url: /local/serial-party-card.js?v=0.1.0
type: module
```

Add a card for each board:

```yaml
type: custom:serial-party-card
app_slug: REPLACE_WITH_INSTALLED_APP_SLUG
board: board-a
title: Development Board
height: 360
font_size: 12
```

Use the **actual installed app slug**, visible in the app's Settings URL. A local
app usually has the slug `local_serial_party`; repository-installed apps have a
repository prefix. The slug is not an IP address, ingress URL or token.

| Field | Meaning |
| --- | --- |
| `app_slug` | Required installed app slug |
| `board` | Required board ID from the app options |
| `title` | Optional plain-text title, 1–120 characters |
| `height` | Optional card height in pixels, 260–1200; default 360 |
| `font_size` | Optional terminal font size, 8–32; default 12 |

The card uses Home Assistant's authenticated ingress session API and refreshes
the session every minute. It discovers the current ingress path at runtime.
No service credentials or ingress tokens belong in the dashboard configuration.
Only the fixed `/boards/<id>/monitor/` endpoint is embedded. **Open controls**
opens the full app; select the desired board there.

The card requires an administrator account, matching the app's default access
policy. Leaving or hiding the page disconnects the observer. Returning resumes
the read-only view. Use **Reconnect** after a connection error. Frequent Home
Assistant state updates do not create extra terminal connections.

The DOM terminal renderer avoids canvas/WebGL pixel scaling issues. Board names
and configured titles are inserted as text, never HTML.

Run the dependency-free lifecycle/configuration tests from the repository root:

```sh
node dashboard/tests/test-card.cjs
```

The session protocol matches [Home Assistant frontend 20261028.5](https://github.com/home-assistant/frontend/blob/20261028.5/src/data/hassio/ingress.ts),
the frontend bundled with Core 2026.11.1.
