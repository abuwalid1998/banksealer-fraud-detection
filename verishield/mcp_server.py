"""Optional, scoped MCP server for VeriShield (BRD 15.5).

It speaks the Model Context Protocol over stdio (JSON-RPC 2.0) and exposes approved,
read-oriented tools. Every tool calls the same governed REST API as the web UI, authenticated
with an API key whose scopes limit what it can do. There is no SQL access, no secret retrieval
and no payment block/release tool.

    python -m verishield.mcp_server --url http://localhost:8000 --api-key vs_xxx.yyy

Claude Desktop / Claude Code configuration example::

    {"mcpServers": {"verishield": {"command": "python",
        "args": ["-m", "verishield.mcp_server", "--url", "http://localhost:8000"],
        "env": {"VERISHIELD_API_KEY": "vs_xxx.yyy"}}}}

Recommended API-key scopes: read:operations, read:aml, reports:generate, policies:simulate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2024-11-05"

TOOLS = [
    {"name": "list_connectors", "description": "List configured connectors/channels with status and counters.",
     "inputSchema": {"type": "object", "properties": {}}, "call": ("GET", "/api/v1/connectors")},
    {"name": "get_connector_health", "description": "Health, last test, last event and recent log of one connector.",
     "inputSchema": {"type": "object", "properties": {"connector_id": {"type": "string"}}, "required": ["connector_id"]},
     "call": ("GET", "/api/v1/connectors/{connector_id}")},
    {"name": "get_ingestion_failures", "description": "Pending dead-letter records (rejected/duplicate events), masked.",
     "inputSchema": {"type": "object", "properties": {"category": {"type": "string", "enum": ["VALIDATION", "DUPLICATE", "PROCESSING"]}}},
     "call": ("GET", "/api/v1/dead-letters")},
    {"name": "get_system_health", "description": "Platform health, latency percentiles and incident summary.",
     "inputSchema": {"type": "object", "properties": {}}, "call": ("GET", "/api/v1/system/health")},
    {"name": "get_model_status", "description": "Active/shadow fraud model, undertrained profiles and outcome measures.",
     "inputSchema": {"type": "object", "properties": {}}, "call": ("GET", "/api/v1/models/status")},
    {"name": "get_aml_queue_summary", "description": "Open AML alerts (most recent first).",
     "inputSchema": {"type": "object", "properties": {"severity": {"type": "string"}}},
     "call": ("GET", "/api/v1/alerts?status=ACTIVE&page_size=50")},
    {"name": "get_case_summary", "description": "Summary of AML cases (status, priority, SLA).",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}},
     "call": ("GET", "/api/v1/cases?page_size=50")},
    {"name": "search_transactions", "description": "Search transactions (masked technical view for API clients).",
     "inputSchema": {"type": "object", "properties": {"search": {"type": "string"}, "decision": {"type": "string"}}},
     "call": ("GET", "/api/v1/transactions?page_size=25")},
    {"name": "preview_policy", "description": "Read the active decision policy (thresholds, actions, rules).",
     "inputSchema": {"type": "object", "properties": {}}, "call": ("GET", "/api/v1/policies")},
    {"name": "simulate_policy", "description": "Simulate a policy version against stored decisions (no side effects).",
     "inputSchema": {"type": "object", "properties": {"policy_id": {"type": "string"}, "days": {"type": "integer"}},
                     "required": ["policy_id"]},
     "call": ("POST", "/api/v1/policies/{policy_id}/simulate")},
    {"name": "generate_report", "description": "Generate a permitted report (e.g. system_failures, channel_health, model_health). Returns the report job.",
     "inputSchema": {"type": "object", "properties": {"type": {"type": "string"}, "format": {"type": "string", "enum": ["pdf", "csv", "xlsx"]}},
                     "required": ["type"]},
     "call": ("POST", "/api/v1/reports")},
]


class Client:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method: str, path: str, args: dict) -> object:
        path = path.format(**{k: urllib.parse.quote(str(v), safe="") for k, v in args.items()})
        body = None
        if method == "GET":
            extra = {k: v for k, v in args.items() if "{" + k + "}" not in path and v not in (None, "")}
            if extra:
                path += ("&" if "?" in path else "?") + urllib.parse.urlencode(extra)
        else:
            body = json.dumps({k: v for k, v in args.items() if k != "policy_id"}).encode()
        req = urllib.request.Request(self.url + path, data=body, method=method,
                                     headers={"X-API-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - operator-configured URL
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            return {"error": exc.code, "detail": exc.read().decode()[:500]}


def handle(client: Client, msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "verishield", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}
    elif method == "tools/call":
        tool = next((t for t in TOOLS if t["name"] == params.get("name")), None)
        if tool is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        data = client.call(*tool["call"], params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)[:60000]}],
                  "isError": isinstance(data, dict) and "error" in data}
    elif method == "ping":
        result = {}
    elif mid is None:
        return None          # notification
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method} not supported"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default=os.environ.get("VERISHIELD_URL", "http://localhost:8000"))
    ap.add_argument("--api-key", default=os.environ.get("VERISHIELD_API_KEY", ""))
    args = ap.parse_args()
    if not args.api_key:
        print("an API key is required (--api-key or VERISHIELD_API_KEY)", file=sys.stderr)
        return 2
    client = Client(args.url, args.api_key)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(client, json.loads(line))
        except Exception as exc:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
