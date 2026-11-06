import asyncio

import pytest
import websockets

from physbox_mcp.origin import origin_allowed, refuse_foreign_origin


@pytest.mark.parametrize("origin", [
    "http://localhost:5175",
    "http://localhost:5173",
    "https://localhost:5176",
    "http://localhost",
    "http://127.0.0.1:5174",
    "https://mesh.physbox.io",
    "https://etch.physbox.io",
    "https://volt.physbox.io",
    "https://flux.physbox.io",
])
def test_the_apps_may_connect(origin):
    assert origin_allowed([origin])


def test_no_origin_is_a_peer_or_a_desktop_app():
    assert origin_allowed([])


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://mesh.physbox.io",
    "https://physbox.io.evil.example",
    "https://evilphysbox.io",
    "https://mesh.physbox.io.evil.example:443",
    "http://localhost.evil.example",
    "http://127.0.0.1.evil.example:5175",
    "http://localhost:5175/extra",
    "null",
    "",
])
def test_anything_else_may_not(origin):
    assert not origin_allowed([origin])


def test_two_origins_are_not_a_choice():
    assert not origin_allowed(["http://localhost:5175", "https://evil.example"])


def test_a_real_handshake():
    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async def main():
        async with websockets.serve(echo, "127.0.0.1", 0, process_request=refuse_foreign_origin) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with websockets.connect(url) as ws:
                await ws.send("peer")
                assert await ws.recv() == "peer"
            async with websockets.connect(url, origin="https://mesh.physbox.io") as ws:
                await ws.send("tab")
                assert await ws.recv() == "tab"
            with pytest.raises(Exception, match="403"):
                async with websockets.connect(url, origin="https://evil.example"):
                    pass

    asyncio.run(main())
