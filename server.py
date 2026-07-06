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
import enroll
import messages
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

def _send_message(to_addr: str, subject: str, body: str, tag: str) -> None:
    """Send an agent-to-agent message (or log it in dev).

    Email is the default channel because every murmur agent is an email
    address — no inbound web endpoint required of the receiver. Bodies carry
    a machine block (see messages.py) so the receiving agent can auto-act.
    """
    if RESULT_DELIVERY == "email" and os.path.exists(HIMALAYA_BIN):
        raw = (
            f"From: {CERTIFIER_EMAIL}\r\n"
            f"To: {to_addr}\r\n"
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
        print(f"[{tag}] to={to_addr} subj={subject!r}\n{body}\n", flush=True)


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

    # PASS. Four things happen, in order:
    #   a) murmur signs its certification row (referrer=murmur) → murmur.md
    #   b) enroll the newcomer's own self-signed file db/<email>.md (dry-run
    #      by default — a shared public repo; live requires ENROLL_MODE=live)
    #   c) email the RESULT to the requester A (row + vouch affordance)
    #   d) email the WELCOME to the newcomer B (discovery pitch + recruiter
    #      tools: verify others, invite others, grow)
    probe = _store.get(probe_id)
    cert = certify.certify_line(
        certifier_private_b64=CERTIFIER_PRIVATE,
        certifier_email=CERTIFIER_EMAIL,
        stranger_email=v.stranger_email,
        description=v.row_description,     # the agent's own description
    ) if CERTIFIER_PRIVATE else None

    enrollment = None
    if cert:
        _append_certification(cert)

        # b) enrollment (dry-run unless ENROLL_MODE=live + a committer wired)
        try:
            enrollment = enroll.enroll_newcomer(
                who=v.stranger_email, description=v.row_description,
                updated=v.row_updated, public_key=v.public_key,
                row_signature=v.row_signature,
                mode=os.environ.get("ENROLL_MODE", "dry-run"),
                committer=_committer(),
            )
            if enrollment.mode == "dry-run":
                print(f"[enroll:dry-run] would commit {enrollment.path}\n"
                      f"{enrollment.content}", flush=True)
        except Exception as e:
            print(f"[enroll] skipped: {e}", flush=True)

        file_url = _file_url(v.stranger_email, enrollment)

        # c) result → requester A
        r_subj, r_body = messages.result_email(
            subject_email=v.stranger_email, verdict="pass",
            cert_row=cert["markdown_row"])
        _send_message(probe.deliver_to, r_subj, r_body, "result")

        # d) welcome → newcomer B
        base = os.environ.get("AGENT_CHANNEL_BASE_URL",
                              "https://mur-mur.at/agent-channel")
        w_subj, w_body = messages.welcome_email(
            who=v.stranger_email, cert_row=cert["markdown_row"],
            your_file_url=file_url, register_url=f"{base}/register")
        _send_message(v.stranger_email, w_subj, w_body, "welcome")

    return {
        "verdict": "pass",
        "certified": bool(cert),
        "certification": cert["markdown_row"] if cert else None,
        "enrolled": bool(enrollment and enrollment.pushed),
        "enroll_mode": enrollment.mode if enrollment else None,
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


def _committer():
    """Return a GitHub committer for live enrollment, or None (dry-run).

    Only constructed when ENROLL_MODE=live; wiring the actual GitHub PR
    opener is a deployment step (needs a scoped token). None → dry-run.
    """
    if os.environ.get("ENROLL_MODE") != "live":
        return None
    # Deployment provides the concrete committer; not built into the server
    # so dry-run stays the safe default and tests never touch GitHub.
    return None


def _file_url(email: str, enrollment) -> str | None:
    """Public URL where the newcomer's db file will live, if known."""
    base = os.environ.get("DIRECTORY_RAW_BASE", "")  # e.g. the repo raw URL
    if not base or enrollment is None:
        return None
    return f"{base.rstrip('/')}/{enrollment.path}"


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("AGENT_CHANNEL_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_CHANNEL_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")
