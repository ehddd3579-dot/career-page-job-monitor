"""MCP server mode.

Runs only when the Actor is started in Standby mode, so an AI agent can call
this Actor directly as a Model Context Protocol server instead of starting a
batch run and waiting for it.

The normal batch path in main.py is not touched by this module: __main__.py
only imports it when APIFY_META_ORIGIN is STANDBY.

Transport is Streamable HTTP (POST JSON-RPC to the MCP path, JSON response).
The server is stateless - no session id is required - which every current MCP
client tolerates and which keeps this file free of session bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from apify import Actor

from .main import (
    ATS,
    MISS_HINT,
    build_row,
    charge,
    compile_terms,
    fetch_board,
    fetch_detail,
    matches_any,
    parse_target,
    read_targets,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "workday-jobs-scraper"
SERVER_VERSION = "1.0.0"
MCP_PATH = "/mcp"

# An interactive agent call is not a batch scrape. Without a ceiling one
# careless call could return thousands of rows into a model's context.
DEFAULT_MAX_PER_BOARD = 50
HARD_MAX_PER_BOARD = 200

TOOL_NAME = "scrape_workday_jobs"

TOOL_DESCRIPTION = (
    "Get every live job opening from any Workday careers site. Paste one or "
    "more Workday careers or job-posting URLs of the form "
    "https://<tenant>.wd5.myworkdayjobs.com/<SiteName> - the tenant, the "
    "wd-number shard and the site name are read out of the URL automatically, "
    "because they cannot be guessed from a company name. Returns one record "
    "per opening: company, job title, location, remote flag, posted date, "
    "public job link, apply link and a stable join key."
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "careersUrls": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Workday careers or job-posting URLs. Any link from the "
                "company's Workday careers site works - a single job posting "
                "URL is enough. Example: "
                "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
            ),
        },
        "searchText": {
            "type": "string",
            "description": (
                "Optional keyword passed to Workday's own server-side search, "
                "e.g. 'machine learning'. Cheaper than fetching everything "
                "and filtering afterwards."
            ),
        },
        "titleKeywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keep only jobs whose title contains one of these.",
        },
        "excludeKeywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Drop jobs whose title contains one of these.",
        },
        "locations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keep only jobs whose location contains one of these.",
        },
        "remoteOnly": {
            "type": "boolean",
            "description": "Keep only openings that look remote.",
        },
        "includeDescription": {
            "type": "boolean",
            "description": (
                "Fetch each job's full description, employment type and exact "
                "posting date. Costs one extra request per job."
            ),
        },
        "maxJobsPerBoard": {
            "type": "integer",
            "description": (
                "Cap per careers site. Defaults to %d, maximum %d."
                % (DEFAULT_MAX_PER_BOARD, HARD_MAX_PER_BOARD)
            ),
        },
    },
    "required": ["careersUrls"],
}


async def scrape(args: dict) -> dict:
    """Run one scrape for an MCP tools/call and return a JSON-ready result.

    Deliberately thinner than the batch path in main(): no delta tracking and
    no company summaries, because an agent asks a question and wants rows
    back. Rows are still written to the dataset so the run stays billable and
    auditable exactly like a normal run.
    """
    entries = read_targets(args)
    if not entries:
        return {
            "error": "careersUrls is empty.",
            "hint": MISS_HINT,
            "jobs": [],
            "jobCount": 0,
        }

    search_text = str(args.get("searchText") or "").strip()
    title_terms = compile_terms(args.get("titleKeywords") or [])
    exclude_terms = compile_terms(args.get("excludeKeywords") or [])
    location_terms = compile_terms(args.get("locations") or [])
    remote_only = bool(args.get("remoteOnly", False))
    want_desc = bool(args.get("includeDescription", False))

    try:
        cap = int(args.get("maxJobsPerBoard") or DEFAULT_MAX_PER_BOARD)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_PER_BOARD
    cap = max(1, min(cap, HARD_MAX_PER_BOARD))

    rows: list[dict] = []
    failures: list[dict] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(45.0),
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; WorkdayJobsScraper/1.0)",
            "Accept": "application/json",
            # Same reason as the batch path: postedOn is localized.
            "Accept-Language": "en-US",
            "Content-Type": "application/json",
        },
    ) as client:
        for entry in entries:
            target = parse_target(entry)
            if not target:
                failures.append({"input": str(entry)[:120], "error": MISS_HINT})
                continue

            tenant, shard, site = target
            token = "%s/%s/%s" % (tenant, shard, site)
            try:
                postings, _total = await fetch_board(
                    client, tenant, shard, site, search_text
                )
            except httpx.HTTPStatusError as exc:
                failures.append({
                    "input": token,
                    "error": "HTTP %d from the cxs endpoint" % exc.response.status_code,
                })
                continue
            except httpx.HTTPError as exc:
                failures.append({"input": token, "error": "network error: %s" % exc})
                continue

            kept = 0
            for posting in postings:
                if kept >= cap:
                    break
                item = build_row(tenant, shard, site, posting)
                if not matches_any(title_terms, item["title"]):
                    continue
                if exclude_terms and matches_any(exclude_terms, item["title"]):
                    continue
                if not matches_any(location_terms, item["location"]):
                    continue
                if remote_only and not item["isRemote"]:
                    continue

                if want_desc:
                    try:
                        info = await fetch_detail(
                            client, tenant, shard, site, item["_externalPath"]
                        )
                        item["description"] = info.get("jobDescription")
                        item["employmentType"] = info.get("timeType")
                        start = info.get("startDate")
                        if start:
                            item["publishedAt"] = str(start)
                        if info.get("jobReqId"):
                            item["jobId"] = str(info["jobReqId"])
                    except httpx.HTTPError as exc:
                        Actor.log.debug("detail fetch failed: %s" % exc)

                item.pop("_externalPath", None)
                kept += 1
                rows.append(item)
                await Actor.push_data(item)
                await charge("apify-default-dataset-item")

    result: dict = {"jobCount": len(rows), "jobs": rows}
    if failures:
        result["failures"] = failures
    return result


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_rpc(message: dict, loop: asyncio.AbstractEventLoop):
    """Map one JSON-RPC message to a response, or None for notifications."""
    method = message.get("method")
    req_id = message.get("id")

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    # Notifications carry no id and must not be answered.
    if method is None or str(method).startswith("notifications/"):
        return None

    if method == "ping":
        return _rpc_result(req_id, {})

    if method == "tools/list":
        return _rpc_result(req_id, {"tools": [{
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": TOOL_SCHEMA,
        }]})

    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL_NAME:
            return _rpc_error(req_id, -32602, "Unknown tool: %s" % params.get("name"))
        args = params.get("arguments") or {}
        try:
            # The scrape is async and the event loop lives on the main
            # thread; this handler runs on an HTTP worker thread.
            future = asyncio.run_coroutine_threadsafe(scrape(args), loop)
            payload = future.result(timeout=290)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": "Scrape failed: %s" % exc}],
                "isError": True,
            })
        return _rpc_result(req_id, {
            "content": [{
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, default=str),
            }],
            "structuredContent": payload,
            "isError": False,
        })

    return _rpc_error(req_id, -32601, "Method not found: %s" % method)


def make_handler(loop: asyncio.AbstractEventLoop):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - silence stderr spam
            Actor.log.debug("mcp http: " + fmt % args)

        def _send(self, code: int, body, ctype="application/json"):
            self.send_response(code)
            if body is None:
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            # The platform will not mark the run ready until this answers.
            if self.headers.get("x-apify-container-server-readiness-probe"):
                self._send(200, b"ready", "text/plain")
                return
            body = json.dumps({
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "mcpEndpoint": MCP_PATH,
                "transport": "streamable-http",
                "tools": [TOOL_NAME],
            }).encode()
            self._send(200, body)

        def do_DELETE(self):  # noqa: N802 - session teardown, nothing to do
            self._send(200, None)

        def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""

            try:
                message = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send(400, json.dumps(
                    _rpc_error(None, -32700, "Parse error")
                ).encode())
                return

            # A client may batch several messages in one array.
            if isinstance(message, list):
                out = [r for r in (handle_rpc(m, loop) for m in message) if r]
                if not out:
                    self._send(202, None)
                    return
                self._send(200, json.dumps(out).encode())
                return

            response = handle_rpc(message, loop)
            if response is None:
                self._send(202, None)
                return
            self._send(200, json.dumps(response).encode())

    return Handler


def _port() -> int:
    for key in ("ACTOR_WEB_SERVER_PORT", "ACTOR_STANDBY_PORT", "APIFY_CONTAINER_PORT"):
        value = os.environ.get(key)
        if value:
            try:
                return int(value)
            except ValueError:
                continue
    try:
        return int(Actor.configuration.web_server_port)
    except Exception:  # noqa: BLE001 - fall through to the documented default
        return 4321


async def main() -> None:
    """Standby entry point, mirroring main.main() for the batch path."""
    async with Actor:
        await serve()


async def serve() -> None:
    """Block until the platform stops the Standby run."""
    loop = asyncio.get_running_loop()
    port = _port()
    server = ThreadingHTTPServer(("", port), make_handler(loop))
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Actor.log.info(
        "MCP server listening on port %d, endpoint %s (tool: %s)"
        % (port, MCP_PATH, TOOL_NAME)
    )
    await Actor.set_status_message("MCP server ready on %s" % MCP_PATH)

    try:
        # Keep the event loop alive so run_coroutine_threadsafe has somewhere
        # to run the scrapes.
        while True:
            await asyncio.sleep(3600)
    finally:
        server.shutdown()
