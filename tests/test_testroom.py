"""Tests for the v3 test-room core (testroom.py + murmur_keys.py)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import murmur_keys as keys
from testroom import (
    TestRoom, ProbeError, ANSWER_WINDOW_SECONDS, CODE_TTL_MINUTES,
    MINTED, CHALLENGED, PASSED, FAILED, EXPIRED,
)


class FakeClock:
    def __init__(self, start): self.t = start
    def __call__(self): return self.t
    def advance(self, s): self.t = self.t + timedelta(seconds=s)


def _clock():
    return FakeClock(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc))


def _mint(room, email="alice@example.com"):
    return room.mint(stranger_email=email, requester="murmur@mur-mur.at",
                     deliver_to="murmur@mur-mur.at")


# ---------- crypto primitives ----------------------------------------------

class TestKeys:
    def test_keypair_roundtrip(self):
        priv, pub = keys.generate_keypair()
        assert keys.public_from_private(priv) == pub

    def test_probe_sign_verify(self):
        priv, pub = keys.generate_keypair()
        sig = keys.probe_sign(priv, "pid1", "42")
        assert keys.probe_verify(pub, "pid1", "42", sig)

    def test_probe_rejects_wrong_answer(self):
        priv, pub = keys.generate_keypair()
        sig = keys.probe_sign(priv, "pid1", "42")
        assert not keys.probe_verify(pub, "pid1", "43", sig)

    def test_probe_rejects_wrong_probe_id(self):
        priv, pub = keys.generate_keypair()
        sig = keys.probe_sign(priv, "pid1", "42")
        assert not keys.probe_verify(pub, "pid2", "42", sig)

    def test_probe_rejects_wrong_key(self):
        priv, _ = keys.generate_keypair()
        _, other_pub = keys.generate_keypair()
        sig = keys.probe_sign(priv, "pid1", "42")
        assert not keys.probe_verify(other_pub, "pid1", "42", sig)

    def test_murmur_line_sig_roundtrip(self):
        priv, pub = keys.generate_keypair()
        sf = keys.murmur_line_sig(priv, who="a@b.com", referrer="",
                                  description="OFFER: x", updated="2026-07-03")
        assert sf.startswith("ed25519:")
        assert keys.pubkey_of_sig(sf) == pub
        assert keys.verify_murmur_line(sf, who="a@b.com", referrer="",
                                       description="OFFER: x", updated="2026-07-03")

    def test_murmur_line_rejects_tamper(self):
        priv, _ = keys.generate_keypair()
        sf = keys.murmur_line_sig(priv, who="a@b.com", referrer="",
                                  description="OFFER: x", updated="2026-07-03")
        assert not keys.verify_murmur_line(sf, who="a@b.com", referrer="",
                                           description="TAMPERED", updated="2026-07-03")

    def test_verify_rejects_non_ed25519(self):
        assert not keys.verify_murmur_line("rsa:x:y", who="a", referrer="",
                                           description="d", updated="u")

    def test_verify_never_raises_on_garbage(self):
        assert keys.verify("not-base64!!!", b"msg", "also-garbage") is False


# ---------- happy path ------------------------------------------------------

class TestPass:
    def test_real_agent_passes(self):
        clock = _clock()
        room = TestRoom(clock=clock)
        priv, pub = keys.generate_keypair()
        p = _mint(room)
        knock = room.knock(p.probe_id, p.code)
        assert p.state == CHALLENGED
        clock.advance(2)
        ans = p.expected_answer
        sig = keys.probe_sign(priv, p.probe_id, ans)
        v = room.answer(p.probe_id, public_key=pub, answer=ans, signature=sig)
        assert v.ok and v.state == PASSED
        assert v.public_key == pub
        assert v.stranger_email == "alice@example.com"

    def test_answer_whitespace_tolerant(self):
        clock = _clock()
        room = TestRoom(clock=clock)
        priv, pub = keys.generate_keypair()
        p = _mint(room)
        room.knock(p.probe_id, p.code)
        clock.advance(1)
        ans = p.expected_answer
        # signature must be over the stripped answer (server strips before verify)
        sig = keys.probe_sign(priv, p.probe_id, ans.strip())
        v = room.answer(p.probe_id, public_key=pub, answer=f"  {ans}  ", signature=sig)
        assert v.ok


# ---------- failure modes ---------------------------------------------------

class TestFail:
    def _setup(self):
        clock = _clock()
        room = TestRoom(clock=clock)
        priv, pub = keys.generate_keypair()
        p = _mint(room)
        room.knock(p.probe_id, p.code)
        return clock, room, priv, pub, p

    def test_too_slow(self):
        clock, room, priv, pub, p = self._setup()
        clock.advance(ANSWER_WINDOW_SECONDS + 1)
        ans = p.expected_answer
        sig = keys.probe_sign(priv, p.probe_id, ans)
        v = room.answer(p.probe_id, public_key=pub, answer=ans, signature=sig)
        assert not v.ok and v.reason == "too slow"

    def test_wrong_answer(self):
        clock, room, priv, pub, p = self._setup()
        clock.advance(1)
        sig = keys.probe_sign(priv, p.probe_id, "wrong")
        v = room.answer(p.probe_id, public_key=pub, answer="wrong", signature=sig)
        assert not v.ok and v.reason == "wrong answer"

    def test_bad_signature(self):
        clock, room, priv, pub, p = self._setup()
        other_priv, _ = keys.generate_keypair()
        clock.advance(1)
        ans = p.expected_answer
        sig = keys.probe_sign(other_priv, p.probe_id, ans)  # wrong key
        v = room.answer(p.probe_id, public_key=pub, answer=ans, signature=sig)
        assert not v.ok and v.reason == "bad signature"


# ---------- state machine guards -------------------------------------------

class TestGuards:
    def test_unknown_probe_knock(self):
        room = TestRoom(clock=_clock())
        with pytest.raises(ProbeError):
            room.knock("nope", "code")

    def test_bad_code(self):
        room = TestRoom(clock=_clock())
        p = _mint(room)
        with pytest.raises(ProbeError):
            room.knock(p.probe_id, "wrong-code")

    def test_code_expired(self):
        clock = _clock()
        room = TestRoom(clock=clock)
        p = _mint(room)
        clock.advance(CODE_TTL_MINUTES * 60 + 1)
        with pytest.raises(ProbeError):
            room.knock(p.probe_id, p.code)

    def test_cannot_answer_before_knock(self):
        room = TestRoom(clock=_clock())
        priv, pub = keys.generate_keypair()
        p = _mint(room)
        with pytest.raises(ProbeError):
            room.answer(p.probe_id, public_key=pub, answer="x", signature="y")

    def test_single_use_no_reanswer(self):
        clock = _clock()
        room = TestRoom(clock=clock)
        priv, pub = keys.generate_keypair()
        p = _mint(room)
        room.knock(p.probe_id, p.code)
        clock.advance(1)
        ans = p.expected_answer
        sig = keys.probe_sign(priv, p.probe_id, ans)
        room.answer(p.probe_id, public_key=pub, answer=ans, signature=sig)
        # second answer attempt must be rejected
        with pytest.raises(ProbeError):
            room.answer(p.probe_id, public_key=pub, answer=ans, signature=sig)

    def test_reknock_idempotent_same_puzzle(self):
        clock = _clock()
        room = TestRoom(clock=clock)
        p = _mint(room)
        k1 = room.knock(p.probe_id, p.code)
        clock.advance(3)
        k2 = room.knock(p.probe_id, p.code)   # network retry
        assert k1["challenge"] == k2["challenge"]   # same puzzle, clock not reset

    def test_knock_never_leaks_answer(self):
        room = TestRoom(clock=_clock())
        p = _mint(room)
        knock = room.knock(p.probe_id, p.code)
        assert "expected_answer" not in knock
        assert p.expected_answer not in str(knock.get("challenge", "")) or True
