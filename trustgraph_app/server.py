"""Small web app on top of TrustGraph: ask questions over your knowledge graph,
add documents and watch the workspace.

    python -m trustgraph_app serve        # http://127.0.0.1:8090
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .client import Settings, TrustGraph, TrustGraphError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    mode: Literal["graph-rag", "document-rag", "agent", "llm"] = "graph-rag"
    flow: str | None = None
    collection: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    state: dict[str, TrustGraph] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["tg"] = TrustGraph(settings)
        yield
        await state["tg"].close()

    app = FastAPI(title="TrustGraph Knowledge Assistant", lifespan=lifespan)

    def tg() -> TrustGraph:
        return state["tg"]

    @app.exception_handler(TrustGraphError)
    async def _tg_error(_, exc: TrustGraphError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status():
        client = tg()
        flows = await client.list_flows()
        docs = await client.list_documents()
        processing = await client.list_processing()
        collections = await client.list_collections()
        return {
            "server": settings.url,
            "workspace": client.workspace,
            "default_flow": settings.flow,
            "default_collection": settings.collection,
            "flows": flows,
            "collections": [c.get("collection") for c in collections],
            "documents": [
                {"id": d["id"], "title": d.get("title") or d["id"], "kind": d.get("kind", ""),
                 "time": d.get("time"),
                 "flows": sorted({p.get("flow") for p in processing if p.get("document-id") == d["id"]})}
                for d in docs
            ],
        }

    @app.post("/api/ask")
    async def ask(body: Ask):
        client = tg()
        flow = body.flow or settings.flow
        started = time.monotonic()
        steps: list[dict] = []
        if body.mode == "graph-rag":
            answer = await client.graph_rag(body.question, flow=flow, collection=body.collection)
        elif body.mode == "document-rag":
            answer = await client.document_rag(body.question, flow=flow, collection=body.collection)
        elif body.mode == "llm":
            answer = await client.text_completion(body.question, flow=flow)
        else:
            parts: list[str] = []
            async for kind, text in client.agent(body.question, flow=flow, collection=body.collection):
                if kind == "answer":
                    parts.append(text)
                elif steps and steps[-1]["kind"] == kind:
                    steps[-1]["text"] += text
                else:
                    steps.append({"kind": kind, "text": text})
            answer = "".join(parts)
        return {"answer": answer, "steps": steps, "mode": body.mode, "flow": flow,
                "seconds": round(time.monotonic() - started, 1)}

    @app.post("/api/documents")
    async def add_document(
        title: str = Form(...),
        text: str = Form(""),
        file: UploadFile | None = File(None),
        process: bool = Form(True),
        flow: str | None = Form(None),
        collection: str | None = Form(None),
    ):
        if file is not None and file.filename:
            content = await file.read(MAX_UPLOAD + 1)
            if len(content) > MAX_UPLOAD:
                raise HTTPException(413, "File is larger than 25 MB")
            kind = "application/pdf" if file.filename.lower().endswith(".pdf") else "text/plain"
        elif text.strip():
            content, kind = text.encode(), "text/plain"
        else:
            raise HTTPException(400, "Provide a file or some text")
        client = tg()
        doc_id = await client.add_document(content, title=title.strip() or "Untitled", kind=kind)
        if process:
            await client.process_document(doc_id, flow=flow or settings.flow, collection=collection)
        return {"id": doc_id, "processing": process}

    @app.post("/api/documents/process")
    async def process(document_id: str = Form(...), flow: str | None = Form(None),
                      collection: str | None = Form(None)):
        await tg().process_document(document_id, flow=flow or settings.flow, collection=collection)
        return {"id": document_id, "processing": True}

    return app
