"""Offline contract tests. All serial terminals are in-process loopback mocks."""
import asyncio
import copy
import json
import unittest

from aiohttp import encode_basic_auth, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from server import create_app, validate_options


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.password = "test-only-password"
        self.auth = encode_basic_auth("test-only-user", self.password)
        self.targets, self.sockets, self.requests = {}, [], []
        boards = []
        for board_id, name in (("board-a", "Development Board"), ("sensor-node", "Sensor Node")):
            board = {"id": board_id, "name": name}
            for mode in ("monitor", "control"):
                key = (board_id, mode)
                target = {"events": asyncio.Queue(), "closed": asyncio.Event(), "socket": None, "status": 200}
                self.targets[key] = target
                mock = web.Application()

                async def http(request, key=key, target=target):
                    self.assertEqual(request.headers.get("Authorization"), self.auth)
                    self.assertEqual(request.path, f"/{key[1]}/")
                    self.assertEqual(request.query_string, "")
                    self.requests.append(key)
                    return web.Response(status=target["status"], text=f"mock {key[0]} {key[1]}", content_type="text/html")

                async def websocket(request, key=key, target=target):
                    self.assertEqual(request.headers.get("Authorization"), self.auth)
                    self.assertEqual(request.path, f"/{key[1]}/ws")
                    ws = web.WebSocketResponse(protocols=("tty",))
                    await ws.prepare(request)
                    target["socket"] = ws
                    try:
                        initial = json.loads((await ws.receive()).data)
                        self.assertEqual(initial["AuthToken"], self.auth[6:])
                        self.assertEqual(set(initial), {"columns", "rows", "AuthToken"})
                        await target["events"].put(initial)
                        await ws.send_bytes(f"0mock output: {key[0]} {key[1]}".encode())
                        async for message in ws:
                            if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                                await target["events"].put(message.data)
                    finally:
                        target["closed"].set()
                    return ws

                mock.router.add_get(f"/{mode}/ws", websocket)
                mock.router.add_get(f"/{mode}/", http)
                target["server"] = TestServer(mock)
                await target["server"].start_server()
                board[f"{mode}_port"] = target["server"].port
            boards.append(board)
        self.options = {"upstream_host": "127.0.0.1", "upstream_username": "test-only-user", "upstream_password": self.password, "boards": boards}
        self.client = TestClient(TestServer(create_app(self.options, allowed_peer="127.0.0.1")))
        await self.client.start_server()

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()
        for target in self.targets.values():
            await target["server"].close()

    async def connect(self, board, mode):
        target = self.targets[(board, mode)]
        ws = await self.client.ws_connect(f"/boards/{board}/{mode}/ws", protocols=("tty",))
        self.sockets.append(ws)
        await ws.send_json({"columns": 80, "rows": 24, "AuthToken": "ignored-client-value", "discard": "this"})
        await asyncio.wait_for(target["events"].get(), 2)
        self.assertEqual((await ws.receive()).data, f"0mock output: {board} {mode}".encode())
        return ws, target

    async def test_each_board_and_mode_uses_only_its_configured_port(self):
        for board, mode in self.targets:
            response = await self.client.get(f"/boards/{board}/{mode}/?arg=ignored")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), f"mock {board} {mode}")
        self.assertEqual(self.requests, list(self.targets))

    async def test_public_board_list_has_only_ids_and_names(self):
        response = await self.client.get("/api/boards")
        self.assertEqual(await response.json(), [{"id": "board-a", "name": "Development Board"}, {"id": "sensor-node", "name": "Sensor Node"}])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_credentials_never_reach_http_browser_responses(self):
        paths = ["/", "/api/boards", "/static/app.js", "/static/style.css"]
        for board, mode in self.targets:
            paths.extend((f"/boards/{board}/{mode}/", f"/boards/{board}/{mode}/token"))
        for path in paths:
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            body = await response.text()
            self.assertNotIn(self.password, body)
            self.assertNotIn(self.auth[6:], body)
        response = await self.client.get("/boards/board-a/monitor/token")
        self.assertEqual(await response.json(), {"token": ""})

    async def test_readonly_input_is_discarded_on_each_board(self):
        for board in ("board-a", "sensor-node"):
            ws, target = await self.connect(board, "monitor")
            await ws.send_bytes(b"0MUST_NOT_REACH_MOCK")
            await ws.send_bytes(b'1{"columns":90,"rows":30}')
            self.assertEqual(await asyncio.wait_for(target["events"].get(), 2), b'1{"columns":90,"rows":30}')
            self.assertTrue(target["events"].empty())
            await ws.close()
            await asyncio.wait_for(target["closed"].wait(), 2)
        self.assertIsNone(self.targets[("board-a", "control")]["socket"])
        self.assertIsNone(self.targets[("sensor-node", "control")]["socket"])

    async def test_control_input_and_disconnect_stay_on_selected_board(self):
        ws, target = await self.connect("sensor-node", "control")
        await ws.send_bytes(b"0MOCK_INPUT")
        self.assertEqual(await asyncio.wait_for(target["events"].get(), 2), b"0MOCK_INPUT")
        self.assertIsNone(self.targets[("board-a", "control")]["socket"])
        await ws.close()
        await asyncio.wait_for(target["closed"].wait(), 2)

    async def test_upstream_loss_closes_browser_socket(self):
        ws, target = await self.connect("board-a", "control")
        await target["socket"].close()
        self.assertIn((await asyncio.wait_for(ws.receive(), 2)).type, (WSMsgType.CLOSE, WSMsgType.CLOSED))

    async def test_unavailable_upstream_has_readable_error_without_affecting_other_board(self):
        await self.targets[("board-a", "monitor")]["server"].close()
        for path in ("/boards/board-a/monitor/", "/boards/board-a/monitor/ws"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 502)
            self.assertEqual(response.content_type, "text/html")
            self.assertIn("Disconnected", await response.text())
            self.assertIn("style-src 'sha256-", response.headers["Content-Security-Policy"])
        self.assertEqual((await self.client.get("/boards/sensor-node/monitor/")).status, 200)

    async def test_http_upstream_errors_use_readable_page(self):
        for status in (401, 503):
            self.targets[("board-a", "monitor")]["status"] = status
            response = await self.client.get("/boards/board-a/monitor/")
            self.assertEqual(response.status, 502)
            self.assertEqual(response.content_type, "text/html")
            self.assertIn("Serial console unavailable", await response.text())

    async def test_unknown_paths_cannot_proxy_any_upstream(self):
        for path in ("/boards/missing/monitor/", "/boards/missing/monitor/token", "/boards/board-a/admin/", "/monitor/", "/data/options.json"):
            self.assertEqual((await self.client.get(path)).status, 404)
        self.assertEqual(self.requests, [])

    async def test_spoofed_forwarding_headers_do_not_bypass_ingress_guard(self):
        client = TestClient(TestServer(create_app(self.options)))
        await client.start_server()
        try:
            response = await client.get("/api/boards", headers={"X-Forwarded-For": "172.30.32.2", "X-Remote-User-Id": "pretend"})
            self.assertEqual(response.status, 403)
        finally:
            await client.close()


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.options = {"upstream_host": "192.0.2.10", "upstream_username": "serialparty", "upstream_password": "test-only-password", "boards": [
            {"id": "board-a", "name": "Board A", "monitor_port": 7681, "control_port": 7682},
            {"id": "board-b", "name": "Board B", "monitor_port": 7683, "control_port": 7684},
        ]}

    def test_valid_hostnames_and_ipv6(self):
        for host in ("serial-host.local", "2001:db8::10", "192.0.2.10"):
            self.options["upstream_host"] = host
            self.assertEqual(validate_options(self.options)["upstream_host"], host)

    def test_rejects_duplicate_or_invalid_ids_and_ports(self):
        for field, value in (("id", "board-a"), ("id", "../board"), ("id", "Board-B"), ("id", "1board"), ("id", "b" * 33), ("monitor_port", 7681), ("monitor_port", True), ("monitor_port", "7683"), ("control_port", 1023), ("control_port", 65536)):
            options = copy.deepcopy(self.options)
            options["boards"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_options(options)
        self.options["boards"][0]["control_port"] = 7681
        with self.assertRaises(ValueError):
            validate_options(self.options)

    def test_rejects_more_than_sixteen_boards(self):
        self.options["boards"] = [{"id": f"board-{i}", "name": f"Board {i}", "monitor_port": 8000 + i * 2, "control_port": 8001 + i * 2} for i in range(17)]
        with self.assertRaises(ValueError):
            validate_options(self.options)

    def test_rejects_missing_boards_or_unsafe_host(self):
        for host in ("https://serial-host", "host/path", "user@host", "host with spaces"):
            self.options["upstream_host"] = host
            with self.assertRaises(ValueError):
                validate_options(self.options)
        self.options["upstream_host"] = "192.0.2.10"
        self.options["boards"] = []
        with self.assertRaises(ValueError):
            validate_options(self.options)


if __name__ == "__main__":
    unittest.main()
