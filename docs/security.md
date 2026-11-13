# Access model

Serial Party is intended for trusted local networks and authenticated Home
Assistant users. It is not an internet-facing console gateway.

## Three layers of read-only enforcement

The dashboard loads only a board's monitor route. The Home Assistant proxy drops
terminal input messages on that route, ttyd's monitor process does not enable
writes, and the Conserver monitor account has read-only access. Hiding a button
in the browser is not the access control.

Write access is an explicit request. The client begins as an observer, requests
the writer politely, and disconnects if it is busy. It neither forces takeover
nor remains queued to acquire the writer after its owner exits.

Before returning a writable connection, the client also checks the broker's
separate status response. Text printed by a board cannot establish ownership.
If multiple sessions use the same writer label on one board, ownership is
ambiguous and the request fails closed. Use distinct configured labels for
independent controllers.

## Network boundaries

- Conserver binds only to `127.0.0.1`. Local clients use its main and secondary
  ports. Its session labels are not individual user credentials.
- ttyd has separate authenticated HTTP ports for each board's monitor and
  controller. Bind these to the intended host address and restrict access to the
  Home Assistant machine using your firewall.
- Basic authentication over HTTP does not encrypt credentials or console data.
  Use a trusted LAN, protected tunnel, or a TLS reverse proxy as appropriate.
- The Home Assistant app exposes no host ports and requests no Supervisor API,
  Home Assistant API, USB, UART, or privileged container access. It accepts its
  HTTP requests only from the real Supervisor ingress peer.
- Browser access requires Home Assistant authentication and administrator access.
  Dashboard sessions use the same ingress session/cookie mechanism as the native
  app page. Session values are never placed in saved dashboard configuration.

The proxy keeps the upstream Basic credential server-side. ttyd's token response
is replaced with an empty browser token; the proxy injects the actual credential
only in the upstream WebSocket handshake. Do not enable unauthenticated direct
terminal ports as a workaround for ingress problems.

## Host access and logging

The managed host installer restricts physical UART access to the broker and
records the previous ownership, mode, and ACL for rollback. This protects against
ordinary competing processes. Root can override these permissions: agents with
sudo access must follow the broker-only policy.

Anyone authorized to view a console can see what the target prints, including
secrets echoed by applications. Logs contain target output and session events;
they are not a complete independent transcript of transmitted bytes. Protect the
log directory and choose retention appropriate for your environment.

## Reporting a problem

Use the repository's GitHub issues for reproducible bugs with synthetic or
redacted logs. Do not post access tokens, passwords, real console credentials,
packet captures, or private Home Assistant backups. Describe a sensitive issue
without including exploit details or secrets until a private reporting channel
has been agreed with the maintainer.
