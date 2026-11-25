"""Tests for trustgraph_app against a local fake of the TrustGraph WebSocket gateway."""
import asyncio
import json
import socket
import threading

import pytest

websockets = pytest.importorskip("websockets")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from trustgraph_app.client import Settings, TrustGraph, TrustGraphError  # noqa: E402
from trustgraph_app.server import create_app  # noqa: E402

TOKEN = "test-token"
received: list[dict] = []


async def gateway(ws):
    auth = json.loads(await ws.recv())
    if auth.get("token") != TOKEN:
        await ws.send(json.dumps({"type": "auth-failed", "error": "bad token"}))
        return
    await ws.send(json.dumps({"type": "auth-ok", "default_workspace": "alice"}))
    async for raw in ws:
        msg = json.loads(raw)
        received.append(msg)
        mid, svc, req = msg["id"], msg["service"], msg["request"]

        async def reply(response, complete=True):
            await ws.send(json.dumps({"id": mid, "response": response, "complete": complete}))

        if svc == "flow":
            await reply({"flow-ids": ["default"], "blueprint-names": ["everything"]})
        elif svc == "librarian" and req["operation"] == "list-documents":
            await reply({"document-metadatas": [{"id": "doc1", "title": "Book", "kind": "application/pdf"}]})
        elif svc == "librarian" and req["operation"] == "list-processing":
            await reply({"processing-metadatas": [{"document-id": "doc1", "flow": "default"}]})
        elif svc == "librarian":
            await reply({})
        elif svc == "collection-management":
            await reply({"collections": [{"collection": "default"}]})
        elif svc == "graph-rag":
            await reply({"response": f"graph answer to: {req['query']}"})
        elif svc == "text-completion":
            await ws.send(json.dumps({"id": mid, "error": {"message": "model offline"}}))
        elif svc == "agent":
            await reply({"message_type": "thought", "content": "Let me "}, complete=False)
            await reply({"message_type": "thought", "content": "look."}, complete=False)
            await reply({"message_type": "observation", "content": "Found it."}, complete=False)
            await reply({"message_type": "final-answer", "content": "42"}, complete=True)


@pytest.fixture(scope="module")
def gateway_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    loop = asyncio.new_event_loop()
    started = threading.Event()

    async def run():
        async with websockets.serve(gateway, "127.0.0.1", port):
            started.set()
            await asyncio.Future()

    thread = threading.Thread(target=loop.run_until_complete, args=(run(),), daemon=True)
    thread.start()
    started.wait(5)
    yield f"http://127.0.0.1:{port}"


def settings(url, token=TOKEN):
    return Settings(url=url, token=token)


def test_client_requests_and_streaming(gateway_url):
    async def run():
        async with TrustGraph(settings(gateway_url)) as tg:
            assert tg.workspace == "alice"
            assert await tg.list_flows() == ["default"]
            assert await tg.graph_rag("hi") == "graph answer to: hi"
            chunks = [c async for c in tg.agent("q")]
            assert chunks == [("thought", "Let me "), ("thought", "look."),
                              ("observation", "Found it."), ("answer", "42")]
            with pytest.raises(TrustGraphError, match="model offline"):
                await tg.text_completion("x")
            # concurrent requests are routed by id
            a, b = await asyncio.gather(tg.graph_rag("a"), tg.graph_rag("b"))
            assert (a, b) == ("graph answer to: a", "graph answer to: b")

    asyncio.run(run())
    rag = [m for m in received if m["service"] == "graph-rag"][0]
    assert rag["flow"] == "default" and rag["request"]["user"] == "alice"


def test_bad_token_is_reported(gateway_url):
    async def run():
        async with TrustGraph(settings(gateway_url, token="wrong")):
            pass

    with pytest.raises(TrustGraphError, match="authentication failed"):
        asyncio.run(run())


def test_settings_require_credentials():
    with pytest.raises(ValueError):
        TrustGraph(Settings(url="https://x"))


def test_web_app(gateway_url):
    with TestClient(create_app(settings(gateway_url))) as client:
        assert "Knowledge Assistant" in client.get("/").text
        status = client.get("/api/status").json()
        assert status["workspace"] == "alice"
        assert status["documents"] == [{"id": "doc1", "title": "Book", "kind": "application/pdf",
                                         "time": None, "flows": ["default"]}]

        r = client.post("/api/ask", json={"question": "What?", "mode": "graph-rag"}).json()
        assert r["answer"] == "graph answer to: What?"

        r = client.post("/api/ask", json={"question": "q", "mode": "agent"}).json()
        assert r["answer"] == "42"
        assert r["steps"] == [{"kind": "thought", "text": "Let me look."},
                              {"kind": "observation", "text": "Found it."}]

        r = client.post("/api/ask", json={"question": "q", "mode": "llm"})
        assert r.status_code == 502 and "model offline" in r.json()["detail"]

        r = client.post("/api/documents", data={"title": "Note", "text": "hello"})
        assert r.status_code == 200 and r.json()["processing"] is True
        ops = [m["request"]["operation"] for m in received if m["service"] == "librarian"]
        assert ops[-2:] == ["add-document", "add-processing"]

        assert client.post("/api/documents", data={"title": "Empty"}).status_code == 400
