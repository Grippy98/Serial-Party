# Contributing

Serial Party joins an existing UART session; it must not compete for the adapter
or unexpectedly take its writer. Read [AGENTS.md](AGENTS.md), the
[access model](docs/security.md), and [migration guide](docs/migration.md) before
changing ownership, installation, proxying, or authentication behavior.

## Local development

Use Python 3.11 or newer and Node.js 18 or newer. From a checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

The host/client package has no third-party Python runtime dependency. The test
extra installs the same pinned aiohttp version as the Home Assistant app. The
dashboard card is plain JavaScript and needs no bundler or CDN.

The Home Assistant app builds independently from `serial_party_app/Dockerfile`.
It uses an official multiarchitecture Python image and a pinned aiohttp wheel.
Its `config.yaml` advertises amd64/aarch64 and ingress access; there is no
prebuilt `image` setting or separate `build.yaml` required. Keep the app schema,
`server.py` validation, host configuration, examples, and card validation aligned.

## Tests

Run these from the repository root:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=serial_party_app .venv/bin/python -m unittest discover -s serial_party_app/tests -v
node dashboard/tests/test-card.cjs
```

The Python client tests use temporary PTYs and a subprocess protocol fixture.
Where Conserver's `conserver` and `console` binaries are installed, they also
exercise a real broker with simulated boards and high loopback ports. These
integration tests skip when the binaries are absent. CI must require them:

```sh
SERIAL_PARTY_REQUIRE_CONSERVER=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

`SERIAL_PARTY_TEST_CONSERVER` and `SERIAL_PARTY_TEST_CONSOLE` select test binary
paths when necessary. These overrides do not affect the installed application's
broker selection. See [the CI workflow](.github/workflows/test.yml) for the Linux
package and container checks used by this repository.

Host tests validate configuration, rendered service files, guarded install and
rollback behavior using mocks. They do not prove a particular USB adapter's
permissions, disconnection behavior, or systemd installation works on hardware.
Proxy tests use only loopback mock terminals and check credentials, WebSockets,
cross-board routing, read-only enforcement, disconnect cleanup, and error pages.
Card tests use a mock DOM and Home Assistant connection to check authentication,
configuration, session expiry, and hidden/disconnected lifecycle behavior.

Do not point tests at physical serial adapters or live Home Assistant services.
Do not send a newline, status command, reset, break, or power event merely to
check connectivity. A passive observation must stay passive.

For an app image check without Home Assistant:

```sh
docker build -t serial-party-app:test serial_party_app
```

Use a local test tag. Building an image does not authorize deploying it or
restarting any broker. Test both supported CPU architectures when modifying
the base image or Python dependency constraints.

## Synthetic UI demo and screenshots

The demo runs the real app UI and proxy against synthetic terminal pages. It
also serves the actual dashboard card with a simulated Home Assistant ingress
adapter. No Home Assistant login, USB adapter, service credential, or board is
needed:

```sh
.venv/bin/python serial_party_app/tests/demo_server.py --port 8815
```

Open either page:

- App: `http://127.0.0.1:8815/`
- Dashboard cards: `http://127.0.0.1:8815/demo/dashboard`

The server and its mock upstreams bind only to loopback. The deliberately public
demo credential is for mock servers only. Stop the process with Ctrl-C when
finished. Use a fresh browser context for screenshots and retain the visible
**simulated boards/output** labeling. Never substitute real logs, local network
addresses, account names, tokens, camera views, or private dashboard screenshots.

For browser changes, check narrow and wide layouts, readable error states, board
selection, monitor/control transitions, and page hide/return. In a synthetic
session, changing boards while viewing controls must close the old frame and
return to read-only. A dashboard card must never navigate to a control endpoint.
The demo's static output is a visual fixture; real terminal behavior still needs
the protocol and proxy tests above.

## Change conventions

- Keep one physical UART owner and one polite writer per board. Busy clients
  must disconnect immediately rather than waiting to be promoted.
- Preserve multiple independent observers and independent board routing.
- Validate IDs, paths, labels, ports, and options before building subprocess
  arguments. Use argument arrays and avoid shell interpolation.
- Keep upstream Basic credentials and ingress sessions out of browser source,
  saved card configuration, logs, screenshots, and examples. Do not weaken the
  real ingress-peer check to make a local preview work; use the test fixture.
- Use PTYs and mock upstreams for new regression tests. Test behavior and failure
  recovery, including cleanup after rejected or interrupted acquisition.
- Keep release versions and examples consistent across `pyproject.toml`, app
  `config.yaml`, and resource URL examples. Describe compatibility changes and
  whether a host migration is required.
- Preserve the repository's [GPLv3 license](LICENSE) and attribute any imported
  code under its compatible license.

Pull requests should explain the concrete behavior change, tests run, and any
limitations that still need hardware validation. Include synthetic reproduction
steps for bugs; exclude private credentials, backups, and packet captures.
