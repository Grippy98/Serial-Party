"""Loopback-only UI demo with simulated boards/output; no hardware or HA needed."""
import argparse
from html import escape
from pathlib import Path
import sys

from aiohttp import encode_basic_auth, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_app

DEMO_AUTH = encode_basic_auth("demo", "demo-not-a-secret")
REPOSITORY = Path(__file__).resolve().parents[2]


def terminal_page(name, mode):
    lines = [
        "SIMULATED OUTPUT — screenshot demo; no device connection",
        "",
        "[    0.000000] Serial Party demo console",
        f"[    0.012000] Board: {name}",
        "[    0.024000] Bootloader: loading demonstration image",
        "[    0.143000] Kernel: hardware initialization complete",
        "[    0.284000] Filesystem: mounted demo root",
        "[    0.412000] System services: ready",
        "[    0.580000] Sensor task: sample interval 1000 ms",
        "[    1.580000] Sample: temperature=23.4 C  humidity=46%",
        "[    2.580000] Sample: temperature=23.4 C  humidity=46%",
        "[    3.580000] Health: all demo checks passed",
        "",
        "demo-board login:",
        "",
        "View: read-only observer" if mode == "monitor" else "View: simulated control — no input is sent anywhere",
    ]
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Simulated serial output</title>
<style>html,body{margin:0;min-height:100%;background:#1b1f25;color:#f1f5f9}pre{margin:0;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.label{color:#98ddba}</style></head><body><pre>""" + escape("\n".join(lines)) + "</pre></body></html>"


async def make_app():
    runners, boards = [], []
    for board_id, name in (("board-a", "Development Board A"), ("sensor-node", "Sensor Node")):
        board = {"id": board_id, "name": name}
        for mode in ("monitor", "control"):
            mock = web.Application()

            async def terminal(request, name=name, mode=mode):
                if request.headers.get("Authorization") != DEMO_AUTH:
                    raise web.HTTPUnauthorized()
                return web.Response(text=terminal_page(name, mode), content_type="text/html")

            mock.router.add_get(f"/{mode}/", terminal)
            runner = web.AppRunner(mock, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            runners.append(runner)
            board[f"{mode}_port"] = site._server.sockets[0].getsockname()[1]
        boards.append(board)

    app = web.Application()
    proxy = create_app({"upstream_host": "127.0.0.1", "upstream_username": "demo", "upstream_password": "demo-not-a-secret", "boards": boards}, allowed_peer="127.0.0.1")
    app.add_subapp("/api/hassio_ingress/demo", proxy)

    async def landing(_):
        raise web.HTTPFound("/api/hassio_ingress/demo/")

    async def favicon(_):
        return web.Response(status=204)

    async def dashboard(_):
        return web.Response(text="""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Serial Party dashboard demo</title>
<style>body{margin:0;padding:32px;background:#10141a;color:#f1f5f9;font:15px/1.5 system-ui}h1{margin:0;font-size:26px}p{color:#b4c0ce}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:20px;margin-top:24px}ha-card{border:1px solid #38424e;border-radius:12px}a{color:#8bd7f7}@media(max-width:600px){body{padding:16px}}</style>
<script src="/demo/card.js"></script></head><body><h1>Serial Party</h1>
<p>Dashboard example · simulated boards and output · no hardware connected</p>
<div class="cards" id="cards"></div><p><a href="/api/hassio_ingress/demo/">Open the app demo</a></p>
<script src="/demo/dashboard.js"></script></body></html>""", content_type="text/html")

    async def card(_):
        return web.FileResponse(REPOSITORY / "dashboard" / "serial-party-card.js")

    async def adapter(_):
        return web.Response(text="""const hass={connected:true,user:{id:'demo-admin',is_admin:true},callWS:async request=>{
 if(request.type==='supervisor/ingress/info')return{state:'started',ingress_url:'/api/hassio_ingress/demo/'};
 if(request.type==='supervisor/ingress/session')return{session:'demo_session'};
 if(request.type==='supervisor/ingress/validate_session')return null;
 throw Error('Unsupported demo API');
}};
for(const [board,title] of [['board-a','Development Board A'],['sensor-node','Sensor Node']]){
 const card=document.createElement('serial-party-card');card.setConfig({type:'custom:serial-party-card',app_slug:'demo_serial_party',board,title,height:420});card.hass=hass;document.getElementById('cards').appendChild(card);
}
""", content_type="application/javascript")

    async def cleanup(_):
        for runner in runners:
            await runner.cleanup()

    app.router.add_get("/", landing)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/demo/dashboard", dashboard)
    app.router.add_get("/demo/card.js", card)
    app.router.add_get("/demo/dashboard.js", adapter)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8815)
    args = parser.parse_args()
    print(f"Simulated app: http://127.0.0.1:{args.port}/")
    print(f"Simulated dashboard: http://127.0.0.1:{args.port}/demo/dashboard")
    web.run_app(make_app(), host="127.0.0.1", port=args.port, access_log=None, print=None)
