"""Command line for the TrustGraph knowledge assistant.

    python -m trustgraph_app status
    python -m trustgraph_app ask "What is VeriShield?" [--mode graph-rag|document-rag|agent|llm]
    python -m trustgraph_app add path/to/file.pdf [--title "..."] [--no-process]
    python -m trustgraph_app start-flow my-flow [--blueprint everything]
    python -m trustgraph_app serve [--host 127.0.0.1] [--port 8090]

Connection settings come from the environment (or a .env file in the
current directory): TRUSTGRAPH_URL, TRUSTGRAPH_USERNAME, TRUSTGRAPH_PASSWORD,
optionally TRUSTGRAPH_TOKEN, TRUSTGRAPH_FLOW, TRUSTGRAPH_COLLECTION.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .client import Settings, TrustGraph, TrustGraphError


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=VALUE lines from .env without overriding variables already set."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


async def cmd_status(tg: TrustGraph, _args) -> None:
    print(f"Workspace:   {tg.workspace}")
    print(f"Flows:       {', '.join(await tg.list_flows()) or '(none running)'}")
    print(f"Blueprints:  {', '.join(await tg.list_blueprints())}")
    print(f"Collections: {', '.join(c['collection'] for c in await tg.list_collections())}")
    processing = await tg.list_processing()
    print("Documents:")
    for doc in await tg.list_documents():
        flows = sorted({p["flow"] for p in processing if p.get("document-id") == doc["id"]})
        state = f"submitted to {', '.join(flows)}" if flows else "not submitted to a flow"
        print(f"  - {doc.get('title') or doc['id']} ({doc.get('kind')}) - {state}")


async def cmd_ask(tg: TrustGraph, args) -> None:
    if args.mode == "graph-rag":
        print(await tg.graph_rag(args.question, flow=args.flow))
    elif args.mode == "document-rag":
        print(await tg.document_rag(args.question, flow=args.flow))
    elif args.mode == "llm":
        print(await tg.text_completion(args.question, flow=args.flow))
    else:
        last = None
        async for kind, text in tg.agent(args.question, flow=args.flow):
            if kind != last:
                print(f"\n[{kind}] ", end="")
                last = kind
            print(text, end="", flush=True)
        print()


async def cmd_add(tg: TrustGraph, args) -> None:
    path = Path(args.path)
    kind = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain"
    doc_id = await tg.add_document(path.read_bytes(), title=args.title or path.stem, kind=kind)
    print(f"Added {doc_id}")
    if not args.no_process:
        await tg.process_document(doc_id, flow=args.flow)
        print(f"Sent for processing in flow '{args.flow or tg.settings.flow}'")


async def cmd_start_flow(tg: TrustGraph, args) -> None:
    await tg.start_flow(args.flow_id, blueprint=args.blueprint)
    print(f"Started flow '{args.flow_id}' from blueprint '{args.blueprint}'")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m trustgraph_app")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show flows, collections and documents")
    p = sub.add_parser("ask", help="ask a question")
    p.add_argument("question")
    p.add_argument("--mode", choices=["graph-rag", "document-rag", "agent", "llm"], default="graph-rag")
    p.add_argument("--flow")
    p = sub.add_parser("add", help="add a PDF or text file to the library")
    p.add_argument("path")
    p.add_argument("--title")
    p.add_argument("--flow")
    p.add_argument("--no-process", action="store_true")
    p = sub.add_parser("start-flow", help="start a processing flow")
    p.add_argument("flow_id")
    p.add_argument("--blueprint", default="everything")
    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default=os.environ.get("TRUSTGRAPH_APP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("TRUSTGRAPH_APP_PORT", "8090")))
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    handler = {"status": cmd_status, "ask": cmd_ask, "add": cmd_add, "start-flow": cmd_start_flow}[args.command]

    async def run() -> None:
        async with TrustGraph(Settings.from_env()) as tg:
            await handler(tg, args)

    try:
        asyncio.run(run())
    except (TrustGraphError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
