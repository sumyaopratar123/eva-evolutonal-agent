#!/usr/bin/env python3
"""Key-guard sidecar: keeps LLM API keys OUT of EVA's container.

Runs as a tiny separate container (same eva:latest image, stdlib only) next to
the sandbox. The real API key is passed ONLY to this sidecar; EVA's container
gets no key and talks to http://llm-proxy:8484/... instead. This process
forwards each request to the ONE configured upstream endpoint and injects the
credential header there.

What this protects against: EVA (which has shell + file access inside its
sandbox and can rewrite its own code) reading the key from its environment and
exfiltrating it. The key never enters EVA's address space.

What this does NOT protect against: EVA using the key indirectly - it can still
make model calls through this proxy (that is its purpose). Spend limits belong
on the provider account.

Design constraints:
  - stdlib only (http.server + http.client), so the eva image runs it as-is.
  - NOT an open relay: the upstream is pinned from the sidecar's own env at
    startup; the request path is ignored. EVA cannot use it to reach other hosts.
  - Streaming (SSE) passes through unbuffered so live token streaming keeps
    working; responses are framed connection-close style which urllib handles.
  - The key never appears in logs (only a masked fingerprint at startup).

This file is HOST INFRASTRUCTURE like the compose hardening itself - it is not
part of EVA's genome, is never mounted into EVA's container, and EVA cannot
edit it.
"""
from __future__ import annotations

import http.client
import http.server
import os
import ssl
import sys
import time
import urllib.parse

BIND_HOST = "0.0.0.0"          # container-internal network only; no host port is published
BIND_PORT = int(os.environ.get("LLM_PROXY_PORT", "8484"))

# Request headers forwarded upstream (lowercase). Everything else - including any
# credential EVA sent - is dropped; the real credential is injected below.
_FORWARD_REQ = {"content-type", "accept", "anthropic-version", "anthropic-beta"}
# Extra provider-specific request headers can be whitelisted WITHOUT editing this
# file: EVA_FORWARD_HEADERS="x-goog-user-project,openai-beta" in the host .env.
_FORWARD_REQ |= {h.strip().lower()
                 for h in os.environ.get("EVA_FORWARD_HEADERS", "").split(",")
                 if h.strip()}
# Response headers forwarded back (framing headers are recomputed by close-framing).
_FORWARD_RESP = {"content-type", "retry-after", "x-request-id", "request-id",
                 "anthropic-ratelimit-requests-remaining"}


def _resolve_upstream() -> "tuple[str, dict]":
    """Pin upstream endpoint + auth header from the sidecar's env, mirroring the
    provider selection rules of EVA's adapter factory (adapters.make_adapter).

    New providers with a different auth scheme need NO code change here: set
    EVA_AUTH_HEADER (e.g. x-goog-api-key) and optionally EVA_AUTH_PREFIX in the
    host .env; the raw key is then sent as '<header>: <prefix><key>'."""
    env = os.environ
    provider = (env.get("EVA_PROVIDER") or "openai_chat").strip().lower()
    if provider in ("anthropic", "claude", "anthropic_messages"):
        endpoint = (env.get("EVA_ENDPOINT") or "").strip()
        if not endpoint.rstrip("/").endswith("/messages"):
            endpoint = "https://api.anthropic.com/v1/messages"
        key = (env.get("EVA_API_KEY") or env.get("ANTHROPIC_API_KEY")
               or env.get("LLM_API_KEY") or "")
        default_hdr, default_prefix = "x-api-key", ""
    else:
        endpoint = (env.get("EVA_ENDPOINT") or env.get("LLM_ENDPOINT")
                    or "https://api.openai.com/v1/chat/completions").strip()
        key = (env.get("EVA_API_KEY") or env.get("LLM_API_KEY")
               or env.get("OPENAI_API_KEY") or "")
        default_hdr, default_prefix = "Authorization", "Bearer "
    header = (env.get("EVA_AUTH_HEADER") or "").strip() or default_hdr
    # empty counts as unset (docker compose passes "" for absent optional vars)
    prefix = env.get("EVA_AUTH_PREFIX") or None
    if prefix is None:
        prefix = "Bearer " if header.lower() == "authorization" else (
            default_prefix if header == default_hdr else "")
    return endpoint, {header: prefix + key}


def _mask(secret: str) -> str:
    if not secret:
        return "(missing)"
    return secret[:4] + "..." + secret[-4:] if len(secret) > 8 else "(set)"


UPSTREAM, AUTH = _resolve_upstream()
_U = urllib.parse.urlsplit(UPSTREAM)
TIMEOUT = float(os.environ.get("LLM_TIMEOUT") or 180)


def _log(msg: str) -> None:
    print(f"[llm-proxy] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"   # connection-close framing: simple + stream-safe

    def log_message(self, *args) -> None:   # default logger prints paths; ours below
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        start = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() in _FORWARD_REQ}
        headers.update(AUTH)                       # inject the REAL credential here
        headers["Host"] = _U.netloc
        headers["Content-Length"] = str(len(body))

        try:
            if _U.scheme == "https":
                conn = http.client.HTTPSConnection(
                    _U.netloc, timeout=TIMEOUT, context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(_U.netloc, timeout=TIMEOUT)
            path = _U.path + (f"?{_U.query}" if _U.query else "")
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            _log(f"upstream error: {type(exc).__name__}: {exc}")
            self.send_error(502, "upstream unreachable")
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in _FORWARD_RESP:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        sent = 0
        try:
            while True:                            # unbuffered pass-through (SSE-safe)
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                                   # client went away mid-stream
        finally:
            conn.close()
        _log(f"POST -> {resp.status} in={len(body)}B out={sent}B "
             f"{time.time() - start:.1f}s")


def main() -> None:
    _log(f"upstream={UPSTREAM}")
    cred = next(iter(AUTH.values()), "")
    if cred.startswith("Bearer "):
        cred = cred[len("Bearer "):]
    _log(f"credential={_mask(cred)}")
    srv = http.server.ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    _log(f"listening on {BIND_HOST}:{BIND_PORT} (container network only)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
