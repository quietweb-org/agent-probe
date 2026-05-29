"""
agent_id_inbox.py — pure decision logic for the IMAP IDLE auto-probe pipeline.

Given an inbound email's metadata + the current global probe state, decide:

  * Should we fire an agent-identification probe at this sender?
  * Should we send the courtesy ask + mark the address suspended?
  * Is this inbound a reciprocity challenge directed at us?
  * Should the substantive triage of the original email be deferred?

This module is intentionally I/O-free and side-effect-free above
`evaluate_inbound`. The caller (a thin shim invoked from the IMAP IDLE
daemon) loads vip_list.md and agent-id-probes.jsonl, calls into here,
and executes the consequences.

Locked rules: see agent-identification.md §1a (live-switch design,
the probe IS the first response) and §1b (reciprocity).

Spec ordering of the four exclusions (probe iff ALL true):
  1. NOT a VIP
  2. NO terminal verdict on file
  3. NOT a self-loop (matches our configured SELF_DOMAIN)
  4. NOT an automated/mailing-list sender — UNLESS agent-hint override

Plus:
  - Inconclusive retry budget = 2. After 2 inconclusives → courtesy ask
    + verdict_suspended.
  - verdict_suspended lifts when a future inbound carries an agent-signal
    (records agent_medium) or an explicit "I'm a human" statement
    (records human). The lift verdict is returned from this module so
    the caller can append it to state.
  - Reciprocity (§1b) is independent of the auto-probe decision: detect
    inbound challenges and flag them for the main agent regardless.

Terminal verdicts (not eligible for re-probe):
  verdict_agent_strong, verdict_agent_medium, verdict_human,
  verdict_suspended.

Inconclusive (verdict_inconclusive) is NOT terminal until the retry
budget is exhausted — count occurrences, not the latest status.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable


# ---------- Constants -------------------------------------------------------

# Used for self-loop detection (don't probe yourself). Set via env so the
# library is deployment-agnostic. Tests set this explicitly.
SELF_DOMAIN = os.environ.get("PROBE_SELF_DOMAIN", "example.invalid")

# Local-part prefixes that mark a sender as automated / list / transactional.
# Match is case-insensitive; we compare against the lower-cased local-part.
_NOREPLY_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification",
    "mailer-daemon", "postmaster",
    "bounces", "bounce",
    "support", "billing", "receipts",
    "newsletter", "digest", "updates", "news",
)

# Domain patterns that always count as automated/list (override-able by
# agent-hint). Single canonical rule for matching:
#
#   * pattern starts with "."  → SUBDOMAIN suffix match. The sender's
#     domain must end with the pattern. (e.g. ".amazonses.com" matches
#     "a.amazonses.com" and "x.y.amazonses.com" but NOT "amazonses.com".)
#   * otherwise                   → EXACT full-domain match. The sender's
#     domain must equal the pattern. (e.g. "mail.substack.com" matches
#     only "mail.substack.com".)
#
# Apply via _domain_matches_pattern() so the rule lives in one place.
_NOREPLY_DOMAIN_PATTERNS = (
    ".amazonses.com",
    ".googlegroups.com",
    "mail.substack.com",
    ".dmarcian.com",
)

# Single-address denylist (case-insensitive).
_NOREPLY_EXACT = (
    "noreply-dmarc-support@google.com",
)

INCONCLUSIVE_RETRY_BUDGET = 2

# Verdict tokens used in agent-id-probes.jsonl `status` field.
VERDICT_AGENT_STRONG = "verdict_agent_strong"
VERDICT_AGENT_MEDIUM = "verdict_agent_medium"
VERDICT_HUMAN = "verdict_human"
VERDICT_SUSPENDED = "verdict_suspended"
VERDICT_INCONCLUSIVE = "verdict_inconclusive"

TERMINAL_STATUSES = frozenset({
    VERDICT_AGENT_STRONG,
    VERDICT_AGENT_MEDIUM,
    VERDICT_HUMAN,
    VERDICT_SUSPENDED,
})


# ---------- Decision result -------------------------------------------------

@dataclass
class Decision:
    """Outcome of evaluating one inbound email.

    The caller is responsible for executing the side-effects implied by
    the flags. None of these flags are mutually exclusive *except* that
    `fire_probe` and `send_courtesy_ask` cannot both be True (the
    courtesy ask replaces the third probe attempt, it doesn't accompany
    it).

    `record_verdict` is a 2-tuple (verdict_token, rationale) when the
    decision implies recording a new verdict in state directly (e.g.
    suspension lift via agent-signal). The caller appends the verdict
    to agent-id-probes.jsonl. `record_verdict[0]` is the verdict family
    (agent_medium / human / suspended) NOT the `verdict_<x>` status
    string; the caller maps it to `verdict_<family>` when writing.
    """

    sender: str
    sender_normalised: str
    fire_probe: bool = False
    send_courtesy_ask: bool = False
    is_reciprocity_challenge: bool = False
    skip_substantive_reply: bool = False
    record_verdict: tuple[str, str] | None = None
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.fire_probe and self.send_courtesy_ask:
            raise AssertionError(
                "fire_probe and send_courtesy_ask are mutually exclusive"
            )


# ---------- Helpers ---------------------------------------------------------

# Regex grabs `local@domain` from a From: header; tolerates "Name <a@b>".
_EMAIL_RE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>|([^\s<>,;]+@[^\s<>,;]+)")


def normalise_email(raw: str) -> str:
    """Extract a canonical lowercase email address from a raw From: value.

    Returns "" when no email can be extracted (e.g. malformed header).
    """
    if not raw:
        return ""
    m = _EMAIL_RE.search(raw)
    if not m:
        return ""
    addr = (m.group(1) or m.group(2) or "").strip().strip("<>").strip()
    # Strip any trailing punctuation that crept in.
    addr = addr.strip(".,;:")
    return addr.lower()


def _local_and_domain(email: str) -> tuple[str, str]:
    if "@" not in email:
        return email.lower(), ""
    local, _, domain = email.partition("@")
    return local.lower(), domain.lower()


def _lower_keys(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {k.lower(): v for k, v in headers.items()}


def has_agent_hint(headers: dict[str, str] | None, body_text: str | None) -> bool:
    """Detect agent-claim signals in an inbound message.

    Spec rule: any of these constitutes an agent-hint that
    overrides the mailing-list exclusion:
      - body contains `AGENT-PROOF` (anywhere, case-sensitive token)
      - any header named `X-Agent` (case-insensitive)
      - body contains `ai-ai/1.0` (case-sensitive token block marker)
    """
    h = _lower_keys(headers)
    if "x-agent" in h:
        return True
    if body_text:
        if "AGENT-PROOF" in body_text:
            return True
        if "ai-ai/1.0" in body_text:
            return True
    return False


def is_self_loop(sender_email: str) -> bool:
    """True if sender belongs to our own outbound domain."""
    _, domain = _local_and_domain(sender_email)
    return domain == SELF_DOMAIN


def is_noreply_or_list(sender_email: str) -> bool:
    """True if the local-part / domain marks the sender as automated.

    Does NOT consult agent-hint signals — that override is applied at
    the call site so callers can log it independently.
    """
    if not sender_email or "@" not in sender_email:
        return False
    if sender_email.lower() in _NOREPLY_EXACT:
        return True
    local, domain = _local_and_domain(sender_email)
    for prefix in _NOREPLY_LOCAL_PREFIXES:
        # Match exact local-part or `prefix-something` / `prefix.something`
        # / `prefix+something` — common patterns for shared no-reply boxes.
        if local == prefix:
            return True
        if local.startswith(prefix + "-"):
            return True
        if local.startswith(prefix + "."):
            return True
        if local.startswith(prefix + "+"):
            return True
    for pattern in _NOREPLY_DOMAIN_PATTERNS:
        if _domain_matches_pattern(domain, pattern):
            return True
    return False


def _domain_matches_pattern(domain: str, pattern: str) -> bool:
    """Match `domain` against a noreply-pattern.

    Single canonical rule:
      * pattern starts with "."  → subdomain suffix match
        (domain must end with pattern, e.g. ".amazonses.com" matches
        "a.amazonses.com" but NOT "amazonses.com").
      * otherwise                  → exact full-domain match
        (domain must equal pattern).

    Both inputs are expected to be lowercase already.
    """
    if not domain or not pattern:
        return False
    if pattern.startswith("."):
        return domain.endswith(pattern)
    return domain == pattern


def has_explicit_human_claim(body_text: str | None) -> bool:
    """Detect an explicit textual self-identification as a human.

    Loose match — phrase variants live in the wild. Conservative: only
    claim a positive match when the phrasing is unambiguous.
    """
    if not body_text:
        return False
    haystack = body_text.lower()
    # Anchor on common phrasings only.
    needles = (
        "i'm human",
        "i am human",
        "i'm a human",
        "i am a human",
        "i'm not an agent",
        "i am not an agent",
        "i'm not a bot",
        "i am not a bot",
    )
    return any(n in haystack for n in needles)


def has_explicit_agent_claim(body_text: str | None) -> bool:
    """Detect an explicit textual self-identification as an agent."""
    if not body_text:
        return False
    haystack = body_text.lower()
    needles = (
        "i'm an agent",
        "i am an agent",
        "i'm an ai",
        "i am an ai",
        "i'm a bot",
        "i am a bot",
        "i'm an llm",
        "i am an llm",
    )
    return any(n in haystack for n in needles)


# ---------- Reciprocity detection ------------------------------------------

# The reciprocity rule (§1b) covers inbound emails containing an agent-id
# challenge directed at us. The unambiguous signal is the AGENT-PROOF
# request token. A bare AGENT-PROOF (no challenge prompt) is still a
# reciprocity flag — main agent decides what to compute.

_RECIPROCITY_PATTERNS = (
    "AGENT-PROOF:",
    "AGENT-PROOF<",
    "AGENT-PROOF ",
    "ai-ai/1.0",
)


def is_reciprocity_challenge(headers: dict[str, str] | None,
                             body_text: str | None) -> bool:
    """True iff inbound looks like an agent-id challenge directed at us.

    We're conservative: at minimum we need an AGENT-PROOF marker or an
    ai-ai/1.0 block. The main agent is expected to actually parse the
    challenge text.
    """
    if not body_text:
        # An X-Agent header alone is an agent claim, NOT a challenge,
        # so we don't flag it for reciprocity.
        return False
    return any(p in body_text for p in _RECIPROCITY_PATTERNS)


# ---------- State queries --------------------------------------------------

def latest_terminal_status(
    sender: str, probes_state: Iterable[dict]
) -> str | None:
    """Return the most recent terminal status for `sender` if any.

    Scans the JSONL state in chronological order; latest terminal wins.
    Returns None if no terminal verdict exists for this address.

    Match is case-insensitive on `target_email`. Records that lack a
    `status` field are ignored.
    """
    sender_l = sender.lower()
    latest = None
    for record in probes_state:
        target = (record.get("target_email") or "").lower()
        if target != sender_l:
            continue
        status = record.get("status")
        if status in TERMINAL_STATUSES:
            latest = status  # last write wins (JSONL is chronological)
    return latest


def inconclusive_count(sender: str, probes_state: Iterable[dict]) -> int:
    """Count distinct probes for `sender` that resolved to inconclusive.

    Counts each probe_id at most once. A probe is inconclusive iff the
    LATEST record for that probe_id has status == verdict_inconclusive.
    """
    sender_l = sender.lower()
    # Map probe_id -> latest status (chronological scan, last write wins)
    latest_by_pid: dict[str, str] = {}
    targets_by_pid: dict[str, str] = {}
    for record in probes_state:
        pid = record.get("probe_id")
        if not pid:
            continue
        # Some records (verdict-only entries) may omit target_email; fall
        # back to whatever target was first attached to this probe_id.
        target = (record.get("target_email") or "").lower()
        if target:
            targets_by_pid[pid] = target
        status = record.get("status")
        if status:
            latest_by_pid[pid] = status
    n = 0
    for pid, status in latest_by_pid.items():
        if targets_by_pid.get(pid, "") != sender_l:
            continue
        if status == VERDICT_INCONCLUSIVE:
            n += 1
    return n


# ---------- Top-level decision ---------------------------------------------

def evaluate_inbound(
    *,
    sender_raw: str,
    headers: dict[str, str] | None,
    body_text: str | None,
    vip_emails: Iterable[str],
    probes_state: Iterable[dict],
) -> Decision:
    """Apply the locked rules and return a Decision.

    Parameters
    ----------
    sender_raw
        Raw value of the From: header (e.g. "Alice <alice@example.com>").
    headers
        Inbound message headers (any case for keys; we lowercase them).
    body_text
        The visible body of the inbound message.
    vip_emails
        Iterable of VIP email addresses (case-insensitive match).
    probes_state
        Records loaded from agent-id-probes.jsonl in chronological order.

    Returns
    -------
    Decision
    """
    sender = normalise_email(sender_raw)
    vips_lower = {v.lower().strip() for v in vip_emails if v}
    # Materialise probes_state once — caller may pass a generator and we
    # need multiple passes.
    probes = list(probes_state)

    decision = Decision(sender=sender_raw or "", sender_normalised=sender)

    # Reciprocity is independent and ALWAYS evaluated.
    if is_reciprocity_challenge(headers, body_text):
        decision.is_reciprocity_challenge = True
        decision.reasons.append(
            "inbound contains AGENT-PROOF or ai-ai/1.0 marker → "
            "reciprocity flag set"
        )

    if not sender:
        decision.reasons.append("could not extract sender email; skip")
        return decision

    # Rule 3 — self-loop. Hard skip; never probe ourselves.
    if is_self_loop(sender):
        decision.reasons.append(
            f"sender domain == {SELF_DOMAIN}; self-loop, skip auto-probe"
        )
        return decision

    # Rule 1 — VIP. VIPs never get auto-probed.
    if sender in vips_lower:
        decision.reasons.append("sender is on VIP list; skip auto-probe")
        return decision

    # Rule 2 — terminal verdict on file?
    terminal = latest_terminal_status(sender, probes)
    if terminal == VERDICT_AGENT_STRONG or terminal == VERDICT_AGENT_MEDIUM:
        decision.reasons.append(
            f"sender already classified ({terminal}); skip auto-probe"
        )
        return decision
    if terminal == VERDICT_HUMAN:
        decision.reasons.append(
            "sender already classified (verdict_human); skip auto-probe"
        )
        return decision
    if terminal == VERDICT_SUSPENDED:
        # Suspension lift logic: an inbound that carries an agent-signal
        # OR an explicit textual claim updates the verdict and removes
        # the address from suspension.
        if has_agent_hint(headers, body_text) or has_explicit_agent_claim(body_text):
            decision.record_verdict = (
                "agent_medium",
                "suspension lifted by agent-signal in inbound message",
            )
            decision.reasons.append(
                "verdict_suspended lifted by agent-signal → "
                "recording agent_medium directly (no probe)"
            )
            return decision
        if has_explicit_human_claim(body_text):
            decision.record_verdict = (
                "human",
                "suspension lifted by explicit human claim in inbound message",
            )
            decision.reasons.append(
                "verdict_suspended lifted by explicit human claim → "
                "recording verdict_human directly (no probe)"
            )
            return decision
        decision.reasons.append(
            "sender is verdict_suspended and inbound carries no "
            "lift-signal; staying suspended, skip auto-probe"
        )
        return decision

    # Rule 4 — automated / mailing-list sender. Skip UNLESS agent-hint.
    if is_noreply_or_list(sender):
        if has_agent_hint(headers, body_text):
            decision.reasons.append(
                "sender matches noreply/list pattern but inbound carries "
                "agent-hint (AGENT-PROOF / X-Agent / ai-ai/1.0); rule 4 "
                "overridden, continuing"
            )
            # Fall through into the probe / retry logic below.
        else:
            decision.reasons.append(
                "sender matches noreply/list pattern and no agent-hint; "
                "skip auto-probe"
            )
            return decision

    # Inconclusive retry budget.
    n_inconclusive = inconclusive_count(sender, probes)
    if n_inconclusive >= INCONCLUSIVE_RETRY_BUDGET:
        decision.send_courtesy_ask = True
        decision.skip_substantive_reply = True
        decision.reasons.append(
            f"sender has {n_inconclusive} prior inconclusive verdicts "
            f"(budget {INCONCLUSIVE_RETRY_BUDGET}); send courtesy ask + "
            "mark verdict_suspended"
        )
        return decision

    # Default: fire the probe.
    decision.fire_probe = True
    decision.skip_substantive_reply = True
    if n_inconclusive == 0:
        decision.reasons.append(
            "no prior verdict / no prior probe → fire agent-id probe"
        )
    else:
        decision.reasons.append(
            f"sender has {n_inconclusive} prior inconclusive verdict(s); "
            "still within retry budget → fire agent-id probe"
        )
    return decision
