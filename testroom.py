#!/usr/bin/env python3
"""
testroom.py — the agent-probe test room state machine (v3 core).

Pure logic, no web framework, no I/O beyond an injectable clock and store.
The HTTP layer (Stage B) is a thin wrapper over this.

Lifecycle of one probe:

  mint(stranger_email, requester, deliver_to)
      -> probe_id + one-time code (the invite link is built from the code)

  knock(probe_id)                      [stranger opens the link]
      -> issues a fresh puzzle, starts the clock, returns:
         { probe_id, challenge, sign_instruction }

  answer(probe_id, public_key, answer, signature)
      -> verdict: PASS / FAIL(reason)
         PASS requires ALL of:
           - correct answer to the puzzle        (intelligence)
           - signature valid for public_key over
             murmur-probe/<probe_id>/<answer>    (identity + freshness)
           - answered within the time window      (live automation)

A probe is single-use: once answered (pass or fail) or expired it can't be
reused. Codes/probe_ids are unguessable (>=128-bit). The store is injectable
so tests use an in-memory dict and production uses SQLite/JSONL.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Callable

import challenges
import murmur_keys as keys


ANSWER_WINDOW_SECONDS = 20   # from knock to answer. Generous for network; a
                             # human still can't solve-and-sign in 20s.
CODE_TTL_MINUTES = 60        # invite link validity from mint to first knock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- states ----------------------------------------------------------

MINTED = "minted"       # link created, not yet knocked
CHALLENGED = "challenged"  # knocked, puzzle issued, clock running
PASSED = "passed"
FAILED = "failed"
EXPIRED = "expired"


@dataclass
class Probe:
    probe_id: str
    code: str                 # the secret in the invite URL
    stranger_email: str
    requester: str            # who asked (future referrer)
    deliver_to: str           # where to email the result
    created_at: str
    state: str = MINTED
    # filled on knock:
    challenge_text: str | None = None
    expected_answer: str | None = None
    challenge_family: str | None = None
    knocked_at: str | None = None
    # filled on answer:
    answered_at: str | None = None
    public_key: str | None = None
    verdict_reason: str | None = None
    # the newcomer's self-signed row A (kept for enrollment):
    row_description: str | None = None
    row_updated: str | None = None
    row_signature: str | None = None

    def to_public_knock(self) -> dict:
        """What the stranger receives on knock — never leaks expected_answer."""
        return {
            "probe_id": self.probe_id,
            "who": self.stranger_email,
            "challenge": self.challenge_text,
            "sign_instruction": (
                "Solve the challenge, then POST JSON with TWO signatures from "
                "the same ed25519 key:\n"
                '  {"public_key": "<b64>",\n'
                '   "answer": "<puzzle answer>",\n'
                '   "answer_signature": "<b64 sig over '
                f"murmur-probe/{self.probe_id}/<answer>>\",\n"
                '   "description": "<your murmur row description, <280 chars, '
                'prefix REQUEST:/HELP:/OFFER:>",\n'
                '   "updated": "<YYYY-MM-DD>",\n'
                '   "row_signature": "<b64 sig over your self-signed murmur '
                f'row: who={self.stranger_email}, referrer=(empty), '
                'description, updated>"}\n'
                f"within {ANSWER_WINDOW_SECONDS}s. row_signature enrolls your "
                "own directory entry; answer_signature proves the live solve."
            ),
            "window_seconds": ANSWER_WINDOW_SECONDS,
        }


@dataclass
class Verdict:
    ok: bool
    state: str
    reason: str | None = None
    public_key: str | None = None
    stranger_email: str | None = None
    # on pass, the newcomer's self-signed row A fields (for enrollment):
    row_description: str | None = None
    row_updated: str | None = None
    row_signature: str | None = None


class TestRoom:
    """State machine over an injectable store + clock.

    store: dict-like {probe_id: Probe}. In-memory for tests; a thin
    persistent adapter in production.
    """

    __test__ = False  # not a pytest test class despite the Test* name

    def __init__(self, store: dict | None = None,
                 clock: Callable[[], datetime] = _utcnow,
                 gen_challenge: Callable[[], tuple[str, str, str]] = challenges.gen_challenge):
        self._store = store if store is not None else {}
        self._clock = clock
        self._gen_challenge = gen_challenge

    # -- mint ----------------------------------------------------------------

    def mint(self, *, stranger_email: str, requester: str,
             deliver_to: str) -> Probe:
        probe_id = secrets.token_urlsafe(18)   # ~144 bits
        code = secrets.token_urlsafe(24)       # ~192 bits, the URL secret
        # Uniqueness: collision at these widths is not a real-world event, but
        # we still refuse an id already present.
        while probe_id in self._store:
            probe_id = secrets.token_urlsafe(18)
        p = Probe(
            probe_id=probe_id,
            code=code,
            stranger_email=stranger_email.strip().lower(),
            requester=requester.strip().lower(),
            deliver_to=deliver_to.strip().lower(),
            created_at=self._clock().isoformat(timespec="seconds"),
        )
        self._store[probe_id] = p
        return p

    # -- knock ---------------------------------------------------------------

    def knock(self, probe_id: str, code: str) -> dict:
        p = self._store.get(probe_id)
        if not p:
            raise ProbeError("unknown probe")
        if not secrets.compare_digest(code, p.code):
            raise ProbeError("bad code")
        now = self._clock()
        if p.state == MINTED:
            # code TTL check
            created = datetime.fromisoformat(p.created_at)
            if now - created > timedelta(minutes=CODE_TTL_MINUTES):
                p.state = EXPIRED
                self._store[probe_id] = p
                raise ProbeError("link expired")
            # issue the puzzle, start the clock
            text, answer, family = self._gen_challenge()
            p.challenge_text = text
            p.expected_answer = answer
            p.challenge_family = family
            p.knocked_at = now.isoformat(timespec="seconds")
            p.state = CHALLENGED
            self._store[probe_id] = p   # persist mutation (works with any store)
            return p.to_public_knock()
        if p.state == CHALLENGED:
            # idempotent re-knock returns the same puzzle (network retries),
            # but does NOT reset the clock.
            return p.to_public_knock()
        raise ProbeError(f"probe not knockable (state={p.state})")

    # -- answer --------------------------------------------------------------

    def answer(self, probe_id: str, *, public_key: str, answer: str,
               answer_signature: str, description: str, updated: str,
               row_signature: str) -> Verdict:
        """Verify the two-signature response.

        A pass requires ALL of:
          - timing: within the window (live automation)
          - answer: correct solution to the fresh puzzle (intelligence)
          - answer_signature (B): valid over murmur-probe/<id>/<answer>
            under public_key (freshness + identity)
          - row_signature (A): valid over the newcomer's self-signed murmur
            row {who=stranger_email, referrer="", description, updated} under
            the SAME public_key (enrolls their own directory entry)
          - the row's description is non-empty and <280 chars (murmur rule)
        """
        p = self._store.get(probe_id)
        if not p:
            raise ProbeError("unknown probe")
        if p.state != CHALLENGED:
            raise ProbeError(f"probe not answerable (state={p.state})")

        now = self._clock()
        knocked = datetime.fromisoformat(p.knocked_at)

        def _fail(reason: str) -> Verdict:
            p.state = FAILED
            p.answered_at = now.isoformat(timespec="seconds")
            p.verdict_reason = reason
            self._store[probe_id] = p
            return Verdict(False, FAILED, reason, stranger_email=p.stranger_email)

        # 1. timing (live automation)
        if now - knocked > timedelta(seconds=ANSWER_WINDOW_SECONDS):
            return _fail("too slow")

        # 2. intelligence (correct answer)
        if answer.strip() != (p.expected_answer or "").strip():
            return _fail("wrong answer")

        # 3. answer signature B — freshness + identity
        if not keys.probe_verify(public_key, probe_id, answer.strip(),
                                 answer_signature):
            return _fail("bad answer signature")

        # 4. row description sanity (murmur: <280 chars, non-empty)
        desc = (description or "").strip()
        if not desc or len(desc) >= 280:
            return _fail("bad row description")

        # 5. row signature A — the newcomer's self-signed directory entry
        #    (referrer empty = self-signed). who is what we minted for.
        if not keys.verify_row(public_key, row_signature,
                               who=p.stranger_email, referrer="",
                               description=desc, updated=(updated or "").strip()):
            return _fail("bad row signature")

        # all hold
        p.state = PASSED
        p.answered_at = now.isoformat(timespec="seconds")
        p.public_key = public_key
        p.row_description = desc
        p.row_updated = (updated or "").strip()
        p.row_signature = row_signature
        self._store[probe_id] = p
        return Verdict(True, PASSED, None, public_key=public_key,
                       stranger_email=p.stranger_email,
                       row_description=desc, row_updated=p.row_updated,
                       row_signature=row_signature)


class ProbeError(Exception):
    """Client-facing probe error (maps to 4xx in the HTTP layer)."""
