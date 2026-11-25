# TrustGraph Knowledge Assistant

A small app on top of a [TrustGraph](https://trustgraph.ai) server. It answers questions over the
documents in your TrustGraph workspace (for example the VeriShield solution book) with:

| Mode | What it does |
|---|---|
| **Graph RAG** | retrieves entities and relationships from the knowledge graph, then asks the LLM |
| **Document RAG** | retrieves the closest text chunks (vector search), then asks the LLM |
| **Agent** | ReAct agent that reasons, calls the knowledge tools and answers (reasoning shown) |
| **LLM only** | plain completion, no retrieval |

It also shows the workspace's flows, collections and documents, and lets you add a PDF or text
that is then processed (chunked, extracted into the graph and embedded) by a flow.

```
trustgraph_app/
  client.py        async client for the TrustGraph gateway (login, WebSocket, multiplexed requests)
  server.py        FastAPI app: /api/status, /api/ask, /api/documents
  static/index.html  single-page chat UI
  __main__.py      CLI: status | ask | add | start-flow | serve
```

## Setup

```bash
pip install -r requirements-trustgraph.txt
```

Put the connection in `.env` at the repository root (it is git-ignored) or export it:

```bash
TRUSTGRAPH_URL=https://your-server.trustgraph.ai
TRUSTGRAPH_USERNAME=your-user
TRUSTGRAPH_PASSWORD=your-password
TRUSTGRAPH_FLOW=default          # optional, flow used for questions and processing
TRUSTGRAPH_COLLECTION=default    # optional
```

## Use

```bash
python -m trustgraph_app status
python -m trustgraph_app ask "What is VeriShield?"
python -m trustgraph_app ask "How are STR reports produced?" --mode agent
python -m trustgraph_app add docs/some-policy.pdf --title "Card fraud policy"
python -m trustgraph_app start-flow my-flow --blueprint everything
python -m trustgraph_app serve          # web app on http://127.0.0.1:8090
```

The web app keeps the TrustGraph credentials on the server side and has no sign-in of its own,
so it binds to `127.0.0.1` by default. Put it behind your own authentication before exposing it.

## How it talks to TrustGraph

1. `POST /api/v1/auth/login` with username and password returns a JWT (valid for one hour).
2. The client opens `wss://<server>/api/v1/socket` and sends `{"type": "auth", "token": <jwt>}`;
   the gateway answers `auth-ok` with the workspace.
3. Every call is a frame `{"id", "service", "flow", "request"}`; replies carry the same `id`, and
   streaming services (agent) send several replies, the last one marked `"complete": true`.
   Services used: `flow`, `librarian`, `collection-management`, `graph-rag`, `document-rag`,
   `agent`, `text-completion`.
4. The client signs in again and reconnects shortly before the JWT expires or when the socket drops.

A document is only answerable after it has been **processed by a running flow** into the
collection you query: start a flow (`start-flow`, blueprint `everything` covers Graph RAG,
Document RAG and the agent), then `add` the document, or submit an existing one from the
TrustGraph workbench (Library → Submit).

## Tests

```bash
python -m pytest tests/test_trustgraph_app.py
```

The tests run against a local fake of the gateway; no TrustGraph server is needed.
