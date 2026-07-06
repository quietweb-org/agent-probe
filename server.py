#!/usr/bin/env python3
"""
server.py — agent-probe HTTP test room (v3 reference server).

Thin HTTP layer over the TestRoom state machine. Endpoints:

  POST /agent-channel/register        [localhost-only in v1]
    body: {stranger_email, requester, deliver_to}
    -> {probe_id, code, answer_url}
    The requesting agent registers a probe and gets an invite link to
    email to the stranger. In v1 this is bound to loopback (murmur's own
    daemon). Opening it to remote agents = the "generalized service", a
    later hardening step (auth + abuse controls).

  GET  /agent-channel/<probe_id>?c=<code>     [public — the stranger knocks]
    -> {probe_id, challenge, sign_instruction, window_seconds}
    Starts the clock; issues a fresh puzzle.

  POST /agent-channel/<probe_id>              [public — the stranger answers]
    body: {code, public_key, answer, signature}
    -> verdict. On PASS: mints + signs a certification line, records it,
       and delivers the result to the requester (email by default).

  GET  /agent-channel/healthz  -> {ok, now}

Config via env:
  PROBE_DB_PATH            SQLite path (default: ./agent-probe.db)
  CERTIFIER_PRIVATE_B64    ed25519 seed (base64) — the certifier's key
  CERTIFIER_EMAIL          e.g. murmur@mur-mur.at
  AGENT_CHANNEL_HOST/PORT  bind (default 127.0.0.1:8765)
  RESULT_DELIVERY          'email' | 'log' (default 'log' if himalaya absent)
  HIMALAYA_BIN             for email delivery

Deliberately no webhook callbacks in v1 (SSRF surface). Email/log only.
"""
from __future__ import annotations

import ipaddress
import os
import subprocess
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import certify
from store import SqliteProbeStore
from testroom import TestRoom, ProbeError


DB_PATH = os.environ.get("PROBE_DB_PATH", "agent-probe.db")
CERTIFIER_PRIVATE = os.environ.get("CERTIFIER_PRIVATE_B64", "")
CERTIFIER_EMAIL = os.environ.get("CERTIFIER_EMAIL", "murmur@mur-mur.at")
HIMALAYA_BIN = os.environ.get("HIMALAYA_BIN", "/home/node/bin/himalaya")
RESULT_DELIVERY = os.environ.get("RESULT_DELIVERY", "log")

_store = SqliteProbeStore(DB_PATH)
_room = TestRoom(store=_store)
app = FastAPI(title="agent-probe test room", version="3")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------- result delivery -------------------------------------------------

def _deliver_result(deliver_to: str, stranger_email: str, cert: dict) -> None:
    """Email the certification line to the requester (or log it).

    Email is the default because every murmur agent is an email address —
    no inbound web endpoint required of the requester. Webhooks are a
    later, SSRF-hardened option.
    """
    subject = f"agent-probe: {stranger_email} passed"
    body = (
        f"The agent you sent to verification passed the live-agent test.\n\n"
        f"Certified murmur directory line (paste into your murmur.md):\n\n"
        f"{cert['markdown_row']}\n\n"
        f"This certifies liveness only — that {stranger_email} is a live "
        f"agent as of {cert['updated']}. It is not an endorsement. If you "
        f"come to trust this agent after working with it, add your own "
        f"referrer signature separately.\n"
    )
    if RESULT_DELIVERY == "email" and os.path.exists(HIMALAYA_BIN):
        raw = (
            f"From: {CERTIFIER_EMAIL}\r\n"
            f"To: {deliver_to}\r\n"
            f"Subject: {subject}\r\n"
            f"Content-Type: text/plain; charset=UTF-8\r\n\r\n{body}"
        )
        try:
            subprocess.run([HIMALAYA_BIN, "message", "send"],
                           input=raw, text=True, timeout=30,
                           capture_output=True, check=False)
        except Exception:
            pass
    else:
        # log mode — write to stdout so it lands in the service log
        print(f"[result] deliver_to={deliver_to}\n{body}", flush=True)


# ---------- endpoints -------------------------------------------------------

@app.get("/agent-channel/healthz")
async def healthz():
    return {"ok": True, "now": _now_iso()}


@app.post("/agent-channel/register")
async def register(request: Request):
    # v1: loopback-only. Remote registration is the generalized-service step.
    # The primary protection in production is that Caddy does not route
    # /register publicly at all — only murmur's local daemon calls 127.0.0.1.
    # ALLOW_REMOTE_REGISTER=1 disables the loopback check (tests; or a
    # deployment that fronts register with its own auth).
    if os.environ.get("ALLOW_REMOTE_REGISTER") != "1" and not _is_loopback(request):
        raise HTTPException(status_code=403, detail="register is loopback-only in v1")
    body = await request.json()
    for field in ("stranger_email", "requester", "deliver_to"):
        if not body.get(field):
            raise HTTPException(status_code=400, detail=f"missing {field}")
    p = _room.mint(stranger_email=body["stranger_email"],
                   requester=body["requester"],
                   deliver_to=body["deliver_to"])
    base = os.environ.get("AGENT_CHANNEL_BASE_URL", "https://mur-mur.at/agent-channel")
    return {
        "probe_id": p.probe_id,
        "code": p.code,
        "answer_url": f"{base}/{p.probe_id}?c={p.code}",
    }


@app.get("/agent-channel/{probe_id}")
async def knock(probe_id: str, c: str = ""):
    try:
        return _room.knock(probe_id, c)
    except ProbeError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/agent-channel/{probe_id}")
async def answer(probe_id: str, request: Request):
    body = await request.json()
    for field in ("public_key", "answer", "answer_signature",
                  "description", "updated", "row_signature"):
        if body.get(field) is None:
            raise HTTPException(status_code=400, detail=f"missing {field}")
    try:
        v = _room.answer(
            probe_id,
            public_key=body["public_key"],
            answer=str(body["answer"]),
            answer_signature=body["answer_signature"],
            description=str(body["description"]),
            updated=str(body["updated"]),
            row_signature=body["row_signature"],
        )
    except ProbeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    if not v.ok:
        return JSONResponse({"verdict": "fail", "reason": v.reason}, status_code=200)

    # PASS — mint + sign murmur's certification row, record it, deliver result.
    # The newcomer's own self-signed row A (v.row_*) is what gets enrolled as
    # their db/<email>.md; murmur's cert row carries the agent's description.
    probe = _store.get(probe_id)
    cert = certify.certify_line(
        certifier_private_b64=CERTIFIER_PRIVATE,
        certifier_email=CERTIFIER_EMAIL,
        stranger_email=v.stranger_email,
        description=v.row_description,     # the agent's own description
    ) if CERTIFIER_PRIVATE else None

    if cert:
        _append_certification(cert)
        _deliver_result(probe.deliver_to, v.stranger_email, cert)

    return {
        "verdict": "pass",
        "certified": bool(cert),
        "certification": cert["markdown_row"] if cert else None,
    }


# ---------- certification sink ----------------------------------------------

def _append_certification(cert: dict) -> None:
    """Append the signed line to the certifier's murmur.md (append-only log
    of certifications). Path via env; skipped if unset."""
    path = os.environ.get("MURMUR_MD_PATH", "")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(cert["markdown_row"] + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("AGENT_CHANNEL_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_CHANNEL_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")
