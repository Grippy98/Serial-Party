# Working on Serial Party

- Preserve the read-only default and immediate, non-forcing writer acquisition.
  A busy client must close, not wait for automatic promotion.
- Keep Conserver on loopback. Never put service credentials or ingress sessions
  in card configuration, client-side source, examples, screenshots, or logs.
- Use pseudo-terminals and simulated upstreams in automated tests. Do not use
  physical boards, live Home Assistant instances, or power controls for tests.
- Validate board IDs, device paths, labels, and port mappings before rendering
  configuration or starting subprocesses. Avoid shell interpolation.
- Preserve rollback metadata before changing managed host files or UART access.
- Check cross-board routing and observer cleanup when changing either client or
  UI code. No default operation should send a probe/newline to a target.
- Keep screenshots reproducible with synthetic names and serial output. Never
  publish private dashboards, device identifiers, credentials, or camera views.
