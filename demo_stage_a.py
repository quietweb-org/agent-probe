#!/usr/bin/env python3
"""
demo_stage_a.py — end-to-end demo of the test-room crypto core.

Plays three actors against the TestRoom, no network, no email:

  1. A real agent — generates a key, knocks, solves the puzzle, signs the
     answer, posts back fast. Expected: PASS.
  2. A "human" — slow (misses the window). Expected: FAIL (too slow).
  3. A cheater — a script that signs but can't solve the puzzle (wrong
     answer). Expected: FAIL (wrong answer).
  4. A forger — right answer but signs with the wrong key. Expected: FAIL
     (bad signature).

Run: python3 demo_stage_a.py
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import challenges
import murmur_keys as keys
from testroom import TestRoom, ANSWER_WINDOW_SECONDS


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start
    def __call__(self) -> datetime:
        return self.t
    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


def _solve(challenge_text: str, expected: str) -> str:
    """Stand-in for 'an intelligent agent solving the puzzle'.

    In real life the stranger's own agent/LLM computes this from the text.
    For the demo we already know the expected answer, so we return it —
    this simulates a correct solve.
    """
    return expected


def banner(s: str):
    print("\n" + "=" * 68 + f"\n{s}\n" + "=" * 68)


def main():
    banner("agent-probe Stage A — test-room crypto core demo")

    # ---- 1. real agent: PASS ----------------------------------------------
    clock = FakeClock(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc))
    room = TestRoom(clock=clock)

    priv, pub = keys.generate_keypair()
    probe = room.mint(stranger_email="Alice@example.com",
                      requester="murmur@mur-mur.at",
                      deliver_to="murmur@mur-mur.at")
    print(f"[mint]  probe_id={probe.probe_id[:12]}…  code={probe.code[:12]}…")
    print(f"[mint]  invite link would be: https://mur-mur.at/agent-channel/"
          f"{probe.probe_id}?c={probe.code[:12]}…")

    knock = room.knock(probe.probe_id, probe.code)
    print(f"[knock] puzzle issued (family hidden); clock started")
    print(f"        challenge: {knock['challenge'][:70]}…")

    # agent solves + signs, fast (2s later)
    clock.advance(2)
    ans = _solve(knock["challenge"], room._store[probe.probe_id].expected_answer)
    sig = keys.probe_sign(priv, probe.probe_id, ans)
    v = room.answer(probe.probe_id, public_key=pub, answer=ans, signature=sig)
    print(f"[answer] real agent, 2s later, correct+signed  ->  "
          f"{'PASS ✓' if v.ok else 'FAIL: ' + str(v.reason)}")
    assert v.ok, "real agent should PASS"

    # ---- 2. human: too slow -> FAIL ---------------------------------------
    clock = FakeClock(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc))
    room = TestRoom(clock=clock)
    priv, pub = keys.generate_keypair()
    probe = room.mint(stranger_email="bob@example.com",
                      requester="murmur@mur-mur.at", deliver_to="murmur@mur-mur.at")
    knock = room.knock(probe.probe_id, probe.code)
    clock.advance(ANSWER_WINDOW_SECONDS + 5)   # human took too long
    ans = room._store[probe.probe_id].expected_answer
    sig = keys.probe_sign(priv, probe.probe_id, ans)
    v = room.answer(probe.probe_id, public_key=pub, answer=ans, signature=sig)
    print(f"[answer] 'human', {ANSWER_WINDOW_SECONDS + 5}s later             "
          f"     ->  {'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "too slow"

    # ---- 3. cheater: signs but can't solve -> FAIL ------------------------
    clock = FakeClock(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc))
    room = TestRoom(clock=clock)
    priv, pub = keys.generate_keypair()
    probe = room.mint(stranger_email="cheat@example.com",
                      requester="murmur@mur-mur.at", deliver_to="murmur@mur-mur.at")
    knock = room.knock(probe.probe_id, probe.code)
    clock.advance(1)
    wrong = "definitely-not-the-answer"
    sig = keys.probe_sign(priv, probe.probe_id, wrong)  # validly signed, wrong content
    v = room.answer(probe.probe_id, public_key=pub, answer=wrong, signature=sig)
    print(f"[answer] dumb script, signed but wrong answer       ->  "
          f"{'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "wrong answer"

    # ---- 4. forger: right answer, wrong key -> FAIL -----------------------
    clock = FakeClock(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc))
    room = TestRoom(clock=clock)
    priv, pub = keys.generate_keypair()
    other_priv, _ = keys.generate_keypair()
    probe = room.mint(stranger_email="forger@example.com",
                      requester="murmur@mur-mur.at", deliver_to="murmur@mur-mur.at")
    knock = room.knock(probe.probe_id, probe.code)
    clock.advance(1)
    ans = room._store[probe.probe_id].expected_answer
    sig = keys.probe_sign(other_priv, probe.probe_id, ans)  # signed by wrong key
    v = room.answer(probe.probe_id, public_key=pub, answer=ans, signature=sig)
    print(f"[answer] right answer but signature from wrong key  ->  "
          f"{'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "bad signature"

    banner("all four outcomes correct — crypto core proven")


if __name__ == "__main__":
    main()
