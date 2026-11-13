# Changelog

## 0.1.0

Initial alpha release:

- Configurable shared serial consoles for up to 16 boards on one Linux host.
- Read-only observers and immediate, non-forcing writer acquisition.
- Python text-console client and interactive command-line tools.
- Guarded systemd installation with per-device access rules and rollback.
- Home Assistant ingress app with explicit control and a board selector.
- Read-only dashboard card, synthetic screenshot examples, and integration tests.

The reusable host installer is new. Existing installations require a coordinated
migration; installing this repository does not automatically migrate them.
