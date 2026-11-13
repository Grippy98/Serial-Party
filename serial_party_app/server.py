"""Ingress-only ttyd proxy; upstream authentication stays on the server."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import re
from pathlib import Path

import aiohttp
from aiohttp import web, WSMsgType

STATIC = Path(__file__).parent / "static"
INGRESS_IP = "172.30.32.2"
LOG = logging.getLogger("serial_party")
BOARD_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ERROR_STYLE = """html { min-height:100%; background:#1b1f25; color:#f1f5f9; color-scheme:dark; }
body { box-sizing:border-box; min-height:100vh; margin:0; padding:clamp(24px,6vw,64px); display:flex; align-items:center; background:#1b1f25; color:#f1f5f9; font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
main { max-width:600px; } h1 { margin:12px 0; font-size:clamp(22px,4vw,28px); line-height:1.3; }
p { margin:12px 0; } .status { color:#ffd484; font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; }
.help { color:#b4c0ce; }"""


def terminal_unavailable(status=502):
    # Fixed HTML/CSS only: no exception, upstream response or request is rendered.
    style_hash = base64.b64encode(hashlib.sha256(ERROR_STYLE.encode()).digest()).decode()
    body = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Serial console unavailable</title><style>""" + ERROR_STYLE + """</style></head>
<body><main><div class="status">Disconnected</div><h1>Serial console unavailable</h1>
<p>The selected serial console could not be reached.</p>
<p class="help">Refresh or reopen this view when the service is ready.
No serial session was opened.</p></main></body></html>"""
    return web.Response(status=status, text=body, content_type="text/html", headers={
        "Content-Security-Policy": f"default-src 'none'; style-src 'sha256-{style_hash}'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
    })


def validate_options(options):
    host = options["upstream_host"]
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("Invalid upstream host")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        labels = host.rstrip(".").split(".")
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            raise ValueError("Use an IP address or DNS hostname for the upstream host") from None
    result = {"upstream_host": host}
    for field in ("upstream_username", "upstream_password"):
        value = options[field]
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
            raise ValueError("Upstream credentials must be configured")
        result[field] = value
    if ":" in result["upstream_username"]:
        raise ValueError("Invalid upstream username")
    boards = options["boards"]
    if not isinstance(boards, list) or not 1 <= len(boards) <= 16:
        raise ValueError("Configure between 1 and 16 boards")
    result["boards"] = []
    ids, ports = set(), set()
    for board in boards:
        if not isinstance(board, dict):
            raise ValueError("Invalid board configuration")
        board_id, name = board.get("id"), board.get("name")
        if not isinstance(board_id, str) or not BOARD_ID.fullmatch(board_id) or board_id in ids:
            raise ValueError("Board IDs must be unique lowercase letters, numbers, hyphens or underscores")
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or not name.isprintable():
            raise ValueError("Board names must contain 1 to 80 readable characters")
        clean = {"id": board_id, "name": name.strip()}
        ids.add(board_id)
        for field in ("monitor_port", "control_port"):
            value = board.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535 or value in ports:
                raise ValueError("Every board port must be a unique integer from 1024 to 65535")
            ports.add(value)
            clean[field] = value
        result["boards"].append(clean)
    return result


def create_app(options, *, allowed_peer=INGRESS_IP):
    """allowed_peer is a test seam, never an app option or request header."""
    options = validate_options(options)
    auth_header = aiohttp.encode_basic_auth(options["upstream_username"], options["upstream_password"])
    token = auth_header.removeprefix("Basic ")
    host = options["upstream_host"]
    host = f"[{host}]" if ":" in host else host
    boards = {board["id"]: board for board in options["boards"]}
    upstreams = {board_id: {
        mode: f"http://{host}:{board[f'{mode}_port']}/{mode}/"
        for mode in ("monitor", "control")
    } for board_id, board in boards.items()}

    @web.middleware
    async def ingress_only(request, handler):
        # Inspect the real TCP peer, never a spoofable forwarding header.
        if request.remote != allowed_peer:
            raise web.HTTPForbidden(text="Home Assistant ingress is required.")
        response = await handler(request)
        if not response.prepared:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    app = web.Application(middlewares=[ingress_only], client_max_size=65536)
    session = None
    active_sockets = set()

    async def session_lifecycle(_):
        nonlocal session
        session = aiohttp.ClientSession(
            headers={"Authorization": auth_header}, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=10, sock_connect=5),
        )
        yield
        await session.close()

    app.cleanup_ctx.append(session_lifecycle)

    async def close_sessions(_):
        for socket in tuple(active_sockets):
            await socket.close(code=1001, message=b"Service stopping")

    app.on_shutdown.append(close_sessions)

    async def static_page(request):
        path = request.match_info.get("file", "index.html")
        if path not in {"index.html", "style.css", "app.js"}:
            raise web.HTTPNotFound()
        response = web.FileResponse(STATIC / path)
        if path == "index.html":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "frame-src 'self'; frame-ancestors 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'none'"
            )
        return response

    async def board_list(_):
        return web.json_response([{"id": board["id"], "name": board["name"]} for board in boards.values()])

    def target(request):
        board_id, mode = request.match_info["board"], request.match_info["mode"]
        if board_id not in boards:
            return None
        return board_id, mode, upstreams[board_id][mode]

    async def proxy_http(request):
        selected = target(request)
        if selected is None:
            return terminal_unavailable(status=404)
        board_id, mode, upstream_url = selected
        tail = request.match_info["tail"]
        if tail == "token":
            # ttyd normally discloses its Basic credential here for WS auth.
            # Inject it upstream instead; never expose it to this client.
            return web.json_response({"token": ""})
        if tail not in {"", "favicon.ico"}:
            raise web.HTTPNotFound()
        try:
            async with session.get(upstream_url + tail, allow_redirects=False) as response:
                if response.status != 200:
                    return terminal_unavailable()
                body = await response.read()
                headers = {"Content-Type": response.headers.get("Content-Type", "text/html; charset=utf-8")}
                return web.Response(body=body, headers=headers)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Do not log request objects or exception text containing auth.
            LOG.warning("%s %s terminal connection unavailable", board_id, mode)
            return terminal_unavailable()

    async def proxy_ws(request):
        selected = target(request)
        if selected is None:
            return terminal_unavailable(status=404)
        board_id, mode, upstream_url = selected
        try:
            upstream = await session.ws_connect(
                upstream_url + "ws", protocols=("tty",),
                heartbeat=30, max_msg_size=8 * 1024 * 1024,
                headers={"Origin": f"http://{host}:{boards[board_id][f'{mode}_port']}"},
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOG.warning("%s %s terminal websocket unavailable", board_id, mode)
            return terminal_unavailable()

        downstream = web.WebSocketResponse(protocols=("tty",), heartbeat=30, max_msg_size=65536)
        tasks = []
        try:
            await downstream.prepare(request)
            active_sockets.add(downstream)
            first = await asyncio.wait_for(downstream.receive(), timeout=10)
            if first.type not in (WSMsgType.TEXT, WSMsgType.BINARY) or len(first.data) > 16384:
                await downstream.close(code=1008, message=b"Invalid terminal handshake")
                return downstream
            try:
                payload = json.loads(first.data)
                if not isinstance(payload, dict):
                    raise ValueError()
                # Accept only terminal geometry; discard client-supplied auth.
                initial = {key: int(payload.get(key, 0)) for key in ("columns", "rows")}
                if any(not 0 <= value <= 65535 for value in initial.values()):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeDecodeError):
                await downstream.close(code=1008, message=b"Invalid terminal handshake")
                return downstream
            initial["AuthToken"] = token
            await upstream.send_bytes(json.dumps(initial).encode())

            async def copy(source, target, *, from_browser=False):
                async for message in source:
                    if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                        break
                    data = message.data
                    prefix = data[:1] if isinstance(data, bytes) else data[:1].encode()
                    if from_browser:
                        # ttyd input=0, resize=1, pause=2, resume=3. No re-auth.
                        allowed = {b"1", b"2", b"3"} if mode == "monitor" else {b"0", b"1", b"2", b"3"}
                        if prefix not in allowed:
                            continue
                    if message.type == WSMsgType.TEXT:
                        await target.send_str(data)
                    else:
                        await target.send_bytes(data)

            tasks = [asyncio.create_task(copy(downstream, upstream, from_browser=True)),
                     asyncio.create_task(copy(upstream, downstream))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            LOG.info("%s %s serial session disconnected", board_id, mode)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            if downstream.prepared:
                await downstream.close(code=1001, message=b"Serial session closed")
            active_sockets.discard(downstream)
        return downstream

    app.router.add_get("/", static_page)
    app.router.add_get("/static/{file}", static_page)
    app.router.add_get("/api/boards", board_list)
    app.router.add_get("/boards/{board}/{mode:monitor|control}/ws", proxy_ws)
    app.router.add_get("/boards/{board}/{mode:monitor|control}/{tail:.*}", proxy_http)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        configured = json.loads(Path("/data/options.json").read_text())
        application = create_app(configured)
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Configure valid upstream addresses and credentials in the app options.") from None
    web.run_app(application, host="0.0.0.0", port=8099, access_log=None, print=None)
