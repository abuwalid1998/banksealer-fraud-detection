"""Async client for the TrustGraph API gateway.

Signs in with a username and password (``POST /api/v1/auth/login``), opens the
gateway WebSocket (``/api/v1/socket``), authenticates it with the JWT and then
multiplexes requests over it.  Every request is a JSON frame::

    {"id": "...", "service": "graph-rag", "flow": "default", "request": {...}}

and every reply carries the same ``id``.  Streaming services (agent, streaming
RAG) send several replies and mark the last one with ``"complete": true``.

The JWT lasts one hour; the client signs in again and reconnects shortly
before it runs out, or whenever the socket drops.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

import websockets


class TrustGraphError(RuntimeError):
    """The gateway or a TrustGraph service reported an error."""


@dataclass
class Settings:
    url: str
    username: str = ""
    password: str = ""
    token: str = ""          # an API key or JWT, used instead of username/password
    flow: str = "default"
    collection: str = "default"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            url=os.environ.get("TRUSTGRAPH_URL", "").rstrip("/"),
            username=os.environ.get("TRUSTGRAPH_USERNAME", ""),
            password=os.environ.get("TRUSTGRAPH_PASSWORD", ""),
            token=os.environ.get("TRUSTGRAPH_TOKEN", ""),
            flow=os.environ.get("TRUSTGRAPH_FLOW", "default"),
            collection=os.environ.get("TRUSTGRAPH_COLLECTION", "default"),
        )


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def _error_text(err: Any) -> str:
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)
    return str(err)


class TrustGraph:
    REFRESH_MARGIN = 300  # sign in again this many seconds before the JWT expires

    def __init__(self, settings: Settings):
        if not settings.url:
            raise ValueError("TRUSTGRAPH_URL is not set")
        if not settings.token and not (settings.username and settings.password):
            raise ValueError("Set TRUSTGRAPH_USERNAME and TRUSTGRAPH_PASSWORD (or TRUSTGRAPH_TOKEN)")
        self.settings = settings
        self.workspace = ""
        self._jwt = settings.token
        self._jwt_expires = float("inf") if settings.token else 0.0
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._tag = secrets.token_hex(4)
        self._ids = itertools.count(1)
        self._ssl = _ssl_context()

    # ------------------------------------------------------------------ connection
    def _login(self) -> None:
        body = json.dumps({"username": self.settings.username, "password": self.settings.password}).encode()
        req = urllib.request.Request(
            f"{self.settings.url}/api/v1/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ssl) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TrustGraphError("TrustGraph sign-in failed: wrong username or password") from exc
            raise TrustGraphError(f"TrustGraph sign-in failed: HTTP {exc.code}") from exc
        self._jwt = data["jwt"]
        expires = data.get("jwt_expires")
        self._jwt_expires = datetime.fromisoformat(expires).timestamp() if expires else time.time() + 3600

    async def _connect(self) -> None:
        await self._disconnect()
        if not self.settings.token and time.time() > self._jwt_expires - self.REFRESH_MARGIN:
            await asyncio.to_thread(self._login)
        ws_url = self.settings.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        ws = await websockets.connect(
            f"{ws_url}/api/v1/socket",
            ssl=self._ssl if ws_url.startswith("wss://") else None,
            max_size=None,
            ping_interval=20,
        )
        await ws.send(json.dumps({"type": "auth", "token": self._jwt}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if reply.get("type") != "auth-ok":
            await ws.close()
            raise TrustGraphError(f"WebSocket authentication failed: {reply.get('error', reply)}")
        self.workspace = reply.get("default_workspace", "")
        self._ws = ws
        self._reader = asyncio.create_task(self._read_loop(ws))

    async def _disconnect(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _read_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                queue = self._queues.get(msg.get("id", ""))
                if queue:
                    queue.put_nowait(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            for queue in self._queues.values():
                queue.put_nowait({"error": {"message": "connection to TrustGraph closed"}})

    async def _ensure_connected(self) -> None:
        async with self._lock:
            stale = not self.settings.token and time.time() > self._jwt_expires - self.REFRESH_MARGIN
            if self._ws is None or self._ws.state != websockets.State.OPEN or stale:
                await self._connect()

    async def close(self) -> None:
        await self._disconnect()

    async def __aenter__(self) -> "TrustGraph":
        await self._ensure_connected()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ transport
    async def stream(self, service: str, request: dict, flow: str | None = None,
                     timeout: float = 180) -> AsyncIterator[dict]:
        """Send one request and yield each reply's ``response`` until ``complete``."""
        await self._ensure_connected()
        mid = f"{self._tag}-{next(self._ids)}"
        frame: dict[str, Any] = {"id": mid, "service": service, "request": request}
        if flow:
            frame["flow"] = flow
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[mid] = queue
        try:
            await self._ws.send(json.dumps(frame))
            while True:
                msg = await asyncio.wait_for(queue.get(), timeout)
                if msg.get("error"):
                    raise TrustGraphError(f"{service}: {_error_text(msg['error'])}")
                response = msg.get("response") or {}
                if isinstance(response, dict) and response.get("error"):
                    raise TrustGraphError(f"{service}: {_error_text(response['error'])}")
                yield response
                if msg.get("complete", True):
                    return
        except asyncio.TimeoutError as exc:
            raise TrustGraphError(f"{service}: no reply within {timeout:.0f}s") from exc
        finally:
            self._queues.pop(mid, None)

    async def request(self, service: str, request: dict, flow: str | None = None,
                      timeout: float = 60) -> dict:
        """Send one request and return the last reply's ``response``."""
        last: dict = {}
        async for last in self.stream(service, request, flow, timeout):
            pass
        return last

    # ------------------------------------------------------------------ workspace
    async def list_flows(self) -> list[str]:
        return (await self.request("flow", {"operation": "list-flows"})).get("flow-ids", [])

    async def list_blueprints(self) -> list[str]:
        return (await self.request("flow", {"operation": "list-blueprints"})).get("blueprint-names", [])

    async def start_flow(self, flow_id: str, blueprint: str = "everything", description: str = "") -> None:
        await self.request("flow", {"operation": "start-flow", "flow-id": flow_id,
                                    "blueprint-name": blueprint, "description": description or flow_id})

    async def list_documents(self) -> list[dict]:
        resp = await self.request("librarian", {"operation": "list-documents", "user": self.workspace})
        return resp.get("document-metadatas", [])

    async def list_processing(self) -> list[dict]:
        resp = await self.request("librarian", {"operation": "list-processing", "user": self.workspace})
        return resp.get("processing-metadatas", [])

    async def list_collections(self) -> list[dict]:
        resp = await self.request("collection-management",
                                  {"operation": "list-collections", "user": self.workspace})
        return resp.get("collections", [])

    async def add_document(self, content: bytes, title: str, kind: str = "text/plain",
                           comments: str = "", tags: list[str] | None = None) -> str:
        """Store a document in the library and return its id."""
        doc_id = f"https://trustgraph.ai/doc/{uuid.uuid4()}"
        await self.request("librarian", {
            "operation": "add-document",
            "document-metadata": {
                "id": doc_id, "time": int(time.time()), "kind": kind, "title": title,
                "comments": comments, "metadata": [], "user": self.workspace, "tags": tags or [],
            },
            "content": base64.b64encode(content).decode(),
        }, timeout=120)
        return doc_id

    async def process_document(self, document_id: str, flow: str | None = None,
                               collection: str | None = None) -> None:
        """Send a stored document through a flow (chunking, extraction, embeddings)."""
        await self.request("librarian", {
            "operation": "add-processing",
            "processing-metadata": {
                "id": f"urn:processing:{uuid.uuid4()}", "document-id": document_id,
                "time": int(time.time()), "flow": flow or self.settings.flow,
                "user": self.workspace, "collection": collection or self.settings.collection, "tags": [],
            },
        })

    # ------------------------------------------------------------------ question answering
    async def text_completion(self, prompt: str, system: str = "You are a helpful assistant.",
                              flow: str | None = None) -> str:
        resp = await self.request("text-completion", {"system": system, "prompt": prompt},
                                  flow or self.settings.flow, timeout=120)
        return resp.get("response", "")

    async def graph_rag(self, query: str, flow: str | None = None, collection: str | None = None,
                        entity_limit: int = 50, triple_limit: int = 30) -> str:
        resp = await self.request("graph-rag", {
            "query": query, "user": self.workspace, "collection": collection or self.settings.collection,
            "entity-limit": entity_limit, "triple-limit": triple_limit,
        }, flow or self.settings.flow, timeout=180)
        return resp.get("response", "")

    async def document_rag(self, query: str, flow: str | None = None, collection: str | None = None,
                           doc_limit: int = 20) -> str:
        resp = await self.request("document-rag", {
            "query": query, "user": self.workspace, "collection": collection or self.settings.collection,
            "doc-limit": doc_limit,
        }, flow or self.settings.flow, timeout=180)
        return resp.get("response", "")

    async def agent(self, question: str, flow: str | None = None,
                    collection: str | None = None) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(kind, text)`` chunks; kind is ``thought``, ``observation`` or ``answer``."""
        request = {"question": question, "user": self.workspace,
                   "collection": collection or self.settings.collection, "streaming": True}
        async for resp in self.stream("agent", request, flow or self.settings.flow, timeout=300):
            kind = resp.get("message_type") or resp.get("chunk_type") or ""
            if kind == "final-answer":
                kind = "answer"
            if kind in ("thought", "observation", "answer") and resp.get("content"):
                yield kind, resp["content"]
