#!/usr/bin/env python3
"""
murmur_keys.py — ed25519 primitives for the murmur network + agent-probe.

Two distinct signing contexts, both ed25519, same key format:

  1. Probe response — a stranger proves key ownership by signing the exact
     challenge string the test room dictates (see `probe_sign_message`).
  2. Directory line — the murmur protocol signature over a directory row:
     sig field = "ed25519:<pubkey_b64>:<signature_b64>", signed data =
     sha256(who + referrer + description + updated).  (See murmur.md spec.)

Keys are handled as base64 strings so they drop straight into env files,
JSON, and the murmur `sig` field without extra encoding. Private keys are
the 32-byte ed25519 seed, base64-encoded. Never log or commit them.

Dependency: `cryptography` (audited, ubiquitous). ed25519 only.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature


# ---------- base64 helpers --------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    # tolerate missing padding
    pad = "=" * (-len(s) % 4)
    return base64.b64decode(s + pad)


# ---------- keypair ---------------------------------------------------------

def generate_keypair() -> tuple[str, str]:
    """Return (private_b64, public_b64).

    private_b64 = base64 of the 32-byte ed25519 seed.
    public_b64  = base64 of the 32-byte public key (matches the pubkey used
                  in murmur `sig` fields).
    """
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes_raw()          # 32 bytes
    pub = sk.public_key().public_bytes_raw()  # 32 bytes
    return _b64e(seed), _b64e(pub)


def public_from_private(private_b64: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(_b64d(private_b64))
    return _b64e(sk.public_key().public_bytes_raw())


# ---------- raw sign / verify ----------------------------------------------

def sign(private_b64: str, message: bytes) -> str:
    """Sign raw bytes; return base64 signature."""
    sk = Ed25519PrivateKey.from_private_bytes(_b64d(private_b64))
    return _b64e(sk.sign(message))


def verify(public_b64: str, message: bytes, signature_b64: str) -> bool:
    """Verify a base64 signature over raw bytes. Never raises."""
    try:
        pk = Ed25519PublicKey.from_public_bytes(_b64d(public_b64))
        pk.verify(_b64d(signature_b64), message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


# ---------- probe response context -----------------------------------------

# The stranger signs EXACTLY this string. Binding probe_id makes each probe's
# signature unique and non-replayable; binding the answer means a valid
# signature over the wrong answer still fails the intelligence check, and a
# right answer with a bad signature fails the identity check. Both must hold.
def probe_sign_message(probe_id: str, answer: str) -> bytes:
    return f"murmur-probe/{probe_id}/{answer}".encode("utf-8")


def probe_sign(private_b64: str, probe_id: str, answer: str) -> str:
    return sign(private_b64, probe_sign_message(probe_id, answer))


def probe_verify(public_b64: str, probe_id: str, answer: str,
                 signature_b64: str) -> bool:
    return verify(public_b64, probe_sign_message(probe_id, answer),
                  signature_b64)


# ---------- murmur directory-line context ----------------------------------

def _line_signed_bytes(who: str, referrer: str, description: str,
                       updated: str) -> bytes:
    """The murmur spec signs sha256(who + referrer + description + updated).

    We sign the 32-byte digest. referrer may be empty string.
    """
    joined = f"{who}{referrer}{description}{updated}".encode("utf-8")
    return hashlib.sha256(joined).digest()


def murmur_line_sig(private_b64: str, *, who: str, referrer: str,
                    description: str, updated: str) -> str:
    """Produce the murmur `sig` field: 'ed25519:<pubkey_b64>:<sig_b64>'."""
    pub = public_from_private(private_b64)
    sig = sign(private_b64, _line_signed_bytes(who, referrer, description, updated))
    return f"ed25519:{pub}:{sig}"


def sign_row(private_b64: str, *, who: str, referrer: str,
             description: str, updated: str) -> str:
    """Sign a murmur row; return the RAW base64 signature (no pubkey prefix).

    Used by an agent self-signing its own row A during a probe — the agent
    sends this raw signature plus its public_key separately.
    """
    return sign(private_b64, _line_signed_bytes(who, referrer, description, updated))


def verify_row(public_b64: str, signature_b64: str, *, who: str, referrer: str,
               description: str, updated: str) -> bool:
    """Verify a raw base64 row signature under a given public key. Never raises."""
    return verify(public_b64,
                  _line_signed_bytes(who, referrer, description, updated),
                  signature_b64)


def verify_murmur_line(sig_field: str, *, who: str, referrer: str,
                       description: str, updated: str) -> bool:
    """Verify a full murmur `sig` field against the row it claims to sign.

    Returns True only if the algorithm is ed25519, the embedded pubkey
    verifies the signature, and it's over this exact row.
    """
    parts = sig_field.split(":")
    if len(parts) != 3:
        return False
    algo, pub_b64, sig_b64 = parts
    if algo != "ed25519":
        return False
    return verify(pub_b64, _line_signed_bytes(who, referrer, description, updated),
                  sig_b64)


def pubkey_of_sig(sig_field: str) -> str | None:
    """Extract the base64 public key from a murmur sig field, or None."""
    parts = sig_field.split(":")
    if len(parts) != 3 or parts[0] != "ed25519":
        return None
    return parts[1]
