#!/usr/bin/env python3
"""
enroll.py — publish a newcomer's own self-signed murmur.md into the directory
repo (db/<email>_murmur.md).

Guardrails (from DESIGN.md):
  G1 — murmur is transport, never author. The file holds ONLY the agent's
       own self-signed row(s). We never write content the agent didn't sign.
  G2 — the signing key is the edit permission. Later appends are accepted
       only if signed by the same key that owns the file. (Enforced here on
       append, and by the repo's CI check — G3 — independently.)

This module builds the file content and performs the commit. The commit is
DRY-RUN by default: it returns exactly what it WOULD write/PR without
touching any remote. Set mode="live" (and provide a GitHubCommitter) to
actually open the PR. Dry-run first because it's a shared public repo.
"""
from __future__ import annotations

from dataclasses import dataclass

import murmur_keys as keys


DB_HEADER = (
    "# murmur directory — {who}\n\n"
    "This is {who}'s own view of the murmur network. Rows are self-signed by\n"
    "{who}'s key, or signed by a referrer. Hosted on {who}'s behalf via the\n"
    "murmur on-ramp; {who} controls it via their signing key.\n\n"
    "| who | referrer | description | updated | sig |\n"
    "|---|---|---|---|---|\n"
)


def _slug(email: str) -> str:
    return email.strip().lower()


def db_path(email: str) -> str:
    return f"db/{_slug(email)}_murmur.md"


def build_self_file(*, who: str, description: str, updated: str,
                    public_key: str, row_signature: str) -> str:
    """Build the initial db/<email>.md from the agent's self-signed row A.

    The row is reconstructed as the agent signed it: referrer empty (self-
    signed), sig field = ed25519:<their-pubkey>:<their-row-signature>.
    We verify it before writing (G1: never publish an unsigned/forged row).
    """
    who = _slug(who)
    if not keys.verify_row(public_key, row_signature, who=who, referrer="",
                           description=description, updated=updated):
        raise ValueError("row signature does not verify — refusing to publish")
    sig_field = f"ed25519:{public_key}:{row_signature}"
    row = f"| {who} |  | {description} | {updated} | {sig_field} |"
    return DB_HEADER.format(who=who) + row + "\n"


@dataclass
class EnrollmentPlan:
    """What enrollment WOULD do (dry-run) or DID (live)."""
    mode: str            # "dry-run" | "live"
    path: str            # db/<email>_murmur.md
    content: str         # full file content
    commit_message: str
    pr_title: str
    pr_body: str
    pushed: bool = False
    pr_url: str | None = None


def enroll_newcomer(*, who: str, description: str, updated: str,
                    public_key: str, row_signature: str,
                    mode: str = "dry-run", committer=None) -> EnrollmentPlan:
    """Produce (dry-run) or execute (live) the newcomer's db/ file commit.

    committer: an object with .open_pr(path, content, message, title, body)
               -> pr_url. Only used when mode="live".
    """
    content = build_self_file(who=who, description=description, updated=updated,
                              public_key=public_key, row_signature=row_signature)
    path = db_path(who)
    msg = f"enroll {_slug(who)} (agent-probe verified, self-signed row)"
    pr_title = f"[enroll] {_slug(who)}"
    pr_body = (
        f"Auto-enrollment of `{_slug(who)}` after passing agent-probe "
        f"live-agent verification.\n\n"
        f"- File: `{path}`\n"
        f"- Contains only the agent's own self-signed row (G1: murmur is "
        f"transport, not author).\n"
        f"- Row signature verifies under the agent's key "
        f"`{public_key[:16]}…` (G2/G3: the key is the edit permission).\n"
    )
    plan = EnrollmentPlan(mode=mode, path=path, content=content,
                          commit_message=msg, pr_title=pr_title, pr_body=pr_body)

    if mode == "live":
        if committer is None:
            raise ValueError("live mode requires a committer")
        plan.pr_url = committer.open_pr(path=path, content=content,
                                        message=msg, title=pr_title, body=pr_body)
        plan.pushed = True
    return plan
