#!/usr/bin/env python3
"""
certify.py — on a PASS, mint + sign the newcomer's murmur directory line.

The certification line is the durable, portable proof. Its meaning is
narrow and must stay narrow: "the referrer ran the agent-probe liveness
test on this address and it passed on <date>." It is NOT a personal
endorsement. Personal vouching is a separate, human-gated act done by the
requesting agent with its own key.

Line fields (murmur protocol):
  who         = the certified agent's email
  referrer    = the certifier's email (e.g. murmur@mur-mur.at)
  description = a fixed, honest liveness-fact string (see CERT_DESCRIPTION)
  updated     = date of the pass (UTC, YYYY-MM-DD)
  sig         = ed25519:<certifier_pubkey>:<sig over sha256(who+referrer+description+updated)>

The certifier signs with its own private key. It never signs as anyone
else, and the description never claims endorsement.
"""
from __future__ import annotations

from datetime import datetime, timezone

import murmur_keys as keys


# The fixed liveness-fact description. Kept deliberately plain so a reader
# knows exactly what a murmur cert means: tested, alive, nothing more.
CERT_DESCRIPTION = "agent-probe: passed live-agent verification"


def certify_line(*, certifier_private_b64: str, certifier_email: str,
                 stranger_email: str, when: datetime | None = None) -> dict:
    """Build a signed murmur directory line certifying the stranger.

    Returns a dict with the five murmur fields plus a ready-to-paste
    markdown table row.
    """
    when = when or datetime.now(timezone.utc)
    updated = when.strftime("%Y-%m-%d")
    who = stranger_email.strip().lower()
    referrer = certifier_email.strip().lower()
    description = CERT_DESCRIPTION

    sig = keys.murmur_line_sig(
        certifier_private_b64,
        who=who, referrer=referrer, description=description, updated=updated,
    )

    row = f"| {who} | {referrer} | {description} | {updated} | {sig} |"
    return {
        "who": who,
        "referrer": referrer,
        "description": description,
        "updated": updated,
        "sig": sig,
        "markdown_row": row,
    }


def verify_certification(line: dict, *, expected_certifier_pubkey: str | None = None) -> bool:
    """Verify a certification line's signature (and optionally that it came
    from a specific certifier public key)."""
    ok = keys.verify_murmur_line(
        line["sig"], who=line["who"], referrer=line["referrer"],
        description=line["description"], updated=line["updated"],
    )
    if not ok:
        return False
    if expected_certifier_pubkey is not None:
        return keys.pubkey_of_sig(line["sig"]) == expected_certifier_pubkey
    return True
