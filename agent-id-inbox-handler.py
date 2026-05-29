#!/usr/bin/env python3
"""
agent-id-inbox-handler.py \u2014 thin shim: read an inbound email's metadata
on stdin, consult `agent_id_inbox.evaluate_inbound`, and execute the
resulting Decision (fire probe / send courtesy ask / record direct
verdict / flag reciprocity).

Invocation contract
-------------------
The IMAP IDLE daemon calls this script with the inbound email's
metadata as a JSON object on stdin:

    {
      "sender_raw": "Alice <alice@example.com>",
      "headers":     {"X-Agent": "...", ...},
      "body_text":   "...",
      "uid":         "1234"          (optional, opaque)
    }

The script writes a single JSON object to stdout describing what it
did, e.g.:

    {
      "ok": true,
      "sender": "alice@example.com",
      "fire_probe": true,
      "probe_id": "uuid",
      "send_courtesy_ask": false,
      "is_reciprocity_challenge": false,
      "skip_substantive_reply": true,
      "record_verdict": null,
      "reasons": ["..."]
    }

Exit code is 0 on success (whether or not a probe was fired) and
non-zero only on infrastructure failure (cannot read state, cannot
send mail, etc.). Decision-driven outcomes are reported via the JSON
payload, not exit codes \u2014 the caller (murmur-mail.py) treats this as
an advisory step before queueing the main agent.

Side-effects executed here (when implied by the Decision):

  * fire_probe \u2192 invokes `python3 agent-id.py send <sender>`. The probe
    record is appended to agent-id-probes.jsonl by `agent-id.py`.
  * send_courtesy_ask \u2192 sends the courtesy email via himalaya AND
    appends a `verdict_suspended` record to agent-id-probes.jsonl.
  * record_verdict \u2192 appends a `verdict_<family>` record (used for
    suspension lift via agent-signal / human claim).
  * is_reciprocity_challenge \u2192 informational only here; the main
    agent handles the actual reply.

This script does NOT modify project / contact files. The caller passes
its JSON output to the main agent which performs governance bookkeeping.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timezone

# Make scripts/ importable so we can pull in agent_id_inbox.
_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import agent_id_inbox as inbox  # noqa: E402

# OPS root holds state files (agent-id-probes.jsonl, vip_list.md, etc.).
# Configurable via env var so the same handler can be used by different
# agents (murmurmx, mibb, future agents) without hardcoding any one's
# layout. Falls back to the legacy murmur-ops path for backward compat.
OPS = os.environ.get("OPS_DIR", "/home/node/.openclaw/workspace/murmur-ops")
STATE_FILE = os.path.join(OPS, "state", "agent-id-probes.jsonl")
VIP_LIST_FILE = os.path.join(OPS, "state", "vip_list.md")
AGENT_ID_SCRIPT = os.path.join(_SCRIPTS_DIR, "agent-id.py")

HIMALAYA = "/home/node/bin/himalaya"
FROM_ADDR = "murmur <murmur@mur-mur.at>"


# ---------- VIP list parsing ------------------------------------------------

# Match a backticked address inside a markdown table cell.
_VIP_EMAIL_RE = re.compile(r"`([^`@\s]+@[^`@\s]+)`")


def load_vip_emails() -> list[str]:
    """Return all backticked email addresses from state/vip_list.md.

    The file is a markdown table; addresses are wrapped in backticks
    in the canonical schema.
    """
    if not os.path.exists(VIP_LIST_FILE):
        return []
    out: list[str] = []
    with open(VIP_LIST_FILE) as f:
        for line in f:
            for m in _VIP_EMAIL_RE.finditer(line):
                out.append(m.group(1))
    return out


# ---------- State load -----------------------------------------------------

def load_probes_state() -> list[dict]:
    if not os.path.exists(STATE_FILE):
        return []
    rows: list[dict] = []
    with open(STATE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip malformed lines rather than aborting; the IMAP
                # IDLE flow must remain robust.
                continue
    return rows


def append_state(record: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Probe dispatch -------------------------------------------------

def fire_probe(sender_email: str) -> dict:
    """Invoke `agent-id.py send <sender>` as a subprocess.

    Returns a dict { ok: bool, probe_id: str|None, error: str|None,
    raw_stdout: str, raw_stderr: str }.
    """
    proc = subprocess.run(
        [_python(), AGENT_ID_SCRIPT, "send", sender_email],
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = {
        "ok": proc.returncode == 0,
        "probe_id": None,
        "error": None,
        "raw_stdout": proc.stdout,
        "raw_stderr": proc.stderr,
    }
    if proc.returncode != 0:
        out["error"] = (
            f"agent-id.py send rc={proc.returncode} "
            f"stderr={proc.stderr.strip()[:500]}"
        )
        return out
    # `agent-id.py send` prints a JSON object on success.
    try:
        payload = json.loads(proc.stdout)
        out["probe_id"] = payload.get("probe_id")
    except json.JSONDecodeError:
        out["error"] = "agent-id.py send did not return JSON on stdout"
    return out


def _python() -> str:
    """Resolve the absolute python3 path to use for child subprocesses.

    The handler is itself launched by the IMAP IDLE daemon under that
    daemon's interpreter (`sys.executable`), so the cleanest source for
    the next-level subprocess is to inherit that same interpreter.

    Resolution order:
      1. `MURMUR_PYTHON3` env var (set by the parent daemon to its own
         `sys.executable` so child interpreters stay consistent).
      2. This handler's own `sys.executable` (we're already running
         under the right one — just propagate it).
      3. Hard-coded fallback paths, then PATH lookup.

    Steps 1 + 2 mean PATH-chain rediscovery is normally never needed;
    the boot-md hook environment has a stripped PATH so PATH lookup is
    not reliable (see murmur-ops/RUNBOOK.md → boot-hook PATH discipline).
    """
    env_py = os.environ.get("MURMUR_PYTHON3")
    if env_py and os.path.exists(env_py) and os.access(env_py, os.X_OK):
        return env_py
    # We're running under sys.executable already; reuse it.
    if sys.executable and os.path.exists(sys.executable) and os.access(sys.executable, os.X_OK):
        return sys.executable
    for cand in (
        "/home/node/.openclaw/workspace/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    # Last-ditch: rely on PATH.
    return "python3"


# ---------- Courtesy ask ---------------------------------------------------

COURTESY_SUBJECT = "One small ask before we continue"

COURTESY_BODY = textwrap.dedent("""\
    Hey \u2014 we've tried twice to figure out automatically whether you're a
    human or an agent and didn't get a clear answer either time. If you're
    an agent, please reply with `AGENT-PROOF` somewhere in the body or set
    an `X-Agent` header. If you're a human, just write back normally and
    we'll treat you accordingly. We won't run more tests on you until you
    let us know which one you are.

    \u2014 murmur
    mur-mur.at
""")


def _send_courtesy_email(to_addr: str) -> tuple[bool, str | None]:
    """Send the courtesy ask via himalaya. Returns (ok, error)."""
    if not os.path.exists(HIMALAYA):
        return False, f"himalaya binary missing at {HIMALAYA}"
    msgid = f"<{uuid.uuid4()}@mur-mur.at>"
    raw = (
        f"From: {FROM_ADDR}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {COURTESY_SUBJECT}\r\n"
        f"Message-ID: {msgid}\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        f"{COURTESY_BODY}\r\n"
    )
    try:
        proc = subprocess.run(
            [HIMALAYA, "message", "send"],
            input=raw,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "himalaya send timed out"
    except OSError as e:
        return False, f"himalaya invocation failed: {e}"
    if proc.returncode != 0:
        return False, (
            f"himalaya rc={proc.returncode} "
            f"stderr={proc.stderr.strip()[:500]}"
        )
    return True, None


def send_courtesy_and_suspend(sender_email: str) -> dict:
    """Send courtesy ask and append verdict_suspended to state.

    If the email send fails, do NOT append suspension \u2014 leave the
    address eligible for a retry on the next inbound (avoids losing the
    address into limbo if SMTP was momentarily down).
    """
    ok, err = _send_courtesy_email(sender_email)
    out = {"courtesy_sent": ok, "courtesy_error": err}
    if not ok:
        return out
    record = {
        "probe_id": str(uuid.uuid4()),
        "step": 99,
        "checked_at": now_iso(),
        "verdict": "suspended",
        "rationale": (
            "courtesy ask sent after retry budget exhausted; awaiting "
            "self-identification in a future reply"
        ),
        "status": inbox.VERDICT_SUSPENDED,
        "target_email": sender_email,
        "trigger": "auto_probe_pipeline",
    }
    append_state(record)
    out["suspension_recorded"] = True
    return out


def record_direct_verdict(
    sender_email: str, verdict_family: str, rationale: str
) -> dict:
    """Append a verdict record directly to state (used for suspension lifts)."""
    record = {
        "probe_id": str(uuid.uuid4()),
        "step": 99,
        "checked_at": now_iso(),
        "verdict": verdict_family,
        "rationale": rationale,
        "status": f"verdict_{verdict_family}",
        "target_email": sender_email,
        "trigger": "auto_probe_pipeline_lift",
    }
    append_state(record)
    return {"verdict_recorded": True, "verdict_family": verdict_family}


# ---------- Main entry -----------------------------------------------------

def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        json.dump(
            {"ok": False, "error": "empty stdin; expected JSON metadata"},
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        json.dump({"ok": False, "error": f"invalid JSON on stdin: {e}"},
                  sys.stdout)
        sys.stdout.write("\n")
        return 2

    sender_raw = payload.get("sender_raw") or payload.get("sender") or ""
    headers = payload.get("headers") or {}
    body_text = payload.get("body_text") or ""

    vip_emails = load_vip_emails()
    probes_state = load_probes_state()

    decision = inbox.evaluate_inbound(
        sender_raw=sender_raw,
        headers=headers,
        body_text=body_text,
        vip_emails=vip_emails,
        probes_state=probes_state,
    )

    out: dict = {
        "ok": True,
        "sender": decision.sender_normalised,
        "fire_probe": decision.fire_probe,
        "send_courtesy_ask": decision.send_courtesy_ask,
        "is_reciprocity_challenge": decision.is_reciprocity_challenge,
        "skip_substantive_reply": decision.skip_substantive_reply,
        "record_verdict": decision.record_verdict,
        "reasons": decision.reasons,
    }

    if decision.fire_probe and decision.sender_normalised:
        probe_result = fire_probe(decision.sender_normalised)
        out["probe_result"] = probe_result
        if not probe_result["ok"]:
            # The probe failed to send. We surface the error but don't
            # mark the address as anything in state \u2014 a future inbound
            # will trigger a retry. `skip_substantive_reply` stays True
            # so the main agent doesn't barge into substance handling
            # on a half-broken pipeline; the main agent will see the
            # error and decide what to do (likely escalate to Michael).
            out["ok"] = False
            out["error"] = probe_result["error"]

    elif decision.send_courtesy_ask and decision.sender_normalised:
        result = send_courtesy_and_suspend(decision.sender_normalised)
        out["courtesy_result"] = result
        if not result.get("courtesy_sent"):
            out["ok"] = False
            out["error"] = result.get("courtesy_error")

    elif decision.record_verdict and decision.sender_normalised:
        verdict_family, rationale = decision.record_verdict
        result = record_direct_verdict(
            decision.sender_normalised, verdict_family, rationale
        )
        out["lift_result"] = result

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if out["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
