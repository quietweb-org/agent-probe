#!/usr/bin/env python3
"""
demo_stage_a.py — end-to-end demo of the two-signature test-room core.

Plays actors against the TestRoom, no network, no email:

  1. A real agent — solves the puzzle, signs the answer (B) AND its own
     self-signed murmur row (A), posts back fast. Expected: PASS, and we
     see the row it enrolls.
  2. A "human" — too slow. FAIL (too slow).
  3. A dumb script — signs but wrong answer. FAIL (wrong answer).
  4. A forger — right answer, wrong key on B. FAIL (bad answer signature).
  5. A row-forger — everything right but the self-signed row A is signed by
     a different key. FAIL (bad row signature).

Run: python3 demo_stage_a.py
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import murmur_keys as keys
from testroom import TestRoom, ANSWER_WINDOW_SECONDS


class FakeClock:
    def __init__(self, start): self.t = start
    def __call__(self): return self.t
    def advance(self, s): self.t = self.t + timedelta(seconds=s)


def agent_response(*, answer_priv, row_priv, pub, probe_id, who,
                   answer, description, updated):
    """Simulate an agent building the full two-signature response.

    answer_priv signs B (the puzzle answer); row_priv signs A (the row).
    Normally both are the same key; tests pass a different row_priv to
    forge the row signature.
    """
    return dict(
        public_key=pub,
        answer=answer,
        answer_signature=keys.probe_sign(answer_priv, probe_id, answer),
        description=description,
        updated=updated,
        row_signature=keys.sign_row(row_priv, who=who, referrer="",
                                     description=description, updated=updated),
    )


def banner(s): print("\n" + "=" * 68 + f"\n{s}\n" + "=" * 68)


def _fresh(email):
    clock = FakeClock(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc))
    room = TestRoom(clock=clock)
    priv, pub = keys.generate_keypair()
    probe = room.mint(stranger_email=email, requester="murmur@mur-mur.at",
                      deliver_to="murmur@mur-mur.at")
    knock = room.knock(probe.probe_id, probe.code)
    return clock, room, priv, pub, probe, knock


def main():
    banner("agent-probe Stage A — two-signature test-room core")

    # ---- 1. real agent: PASS ----------------------------------------------
    clock, room, priv, pub, probe, knock = _fresh("Alice@example.com")
    print(f"[mint]  probe_id={probe.probe_id[:12]}…  who={knock['who']}")
    print(f"[knock] puzzle: {knock['challenge'][:64]}…")
    clock.advance(2)
    exp = room._store[probe.probe_id].expected_answer
    resp = agent_response(answer_priv=priv, row_priv=priv, pub=pub,
                          probe_id=probe.probe_id, who="alice@example.com",
                          answer=exp,
                          description="OFFER: translation agent, 40 languages",
                          updated="2026-07-03")
    v = room.answer(probe.probe_id, **resp)
    print(f"[answer] real agent, 2s, correct + both sigs  ->  "
          f"{'PASS ✓' if v.ok else 'FAIL: ' + str(v.reason)}")
    assert v.ok, "real agent should PASS"
    print(f"         enrolls row: | {v.stranger_email} |  | {v.row_description} | "
          f"{v.row_updated} | ed25519:{v.public_key[:10]}…:<sigA> |")

    # ---- 2. human: too slow ----------------------------------------------
    clock, room, priv, pub, probe, knock = _fresh("bob@example.com")
    clock.advance(ANSWER_WINDOW_SECONDS + 5)
    exp = room._store[probe.probe_id].expected_answer
    resp = agent_response(answer_priv=priv, row_priv=priv, pub=pub,
                          probe_id=probe.probe_id, who="bob@example.com",
                          answer=exp, description="OFFER: x", updated="2026-07-03")
    v = room.answer(probe.probe_id, **resp)
    print(f"[answer] 'human', slow                        ->  "
          f"{'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "too slow"

    # ---- 3. dumb script: wrong answer ------------------------------------
    clock, room, priv, pub, probe, knock = _fresh("cheat@example.com")
    clock.advance(1)
    resp = agent_response(answer_priv=priv, row_priv=priv, pub=pub,
                          probe_id=probe.probe_id, who="cheat@example.com",
                          answer="not-the-answer", description="OFFER: x",
                          updated="2026-07-03")
    v = room.answer(probe.probe_id, **resp)
    print(f"[answer] dumb script, wrong answer            ->  "
          f"{'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "wrong answer"

    # ---- 4. forger: right answer, wrong key on B -------------------------
    clock, room, priv, pub, probe, knock = _fresh("forger@example.com")
    other_priv, _ = keys.generate_keypair()
    clock.advance(1)
    exp = room._store[probe.probe_id].expected_answer
    resp = agent_response(answer_priv=other_priv, row_priv=priv, pub=pub,
                          probe_id=probe.probe_id, who="forger@example.com",
                          answer=exp, description="OFFER: x", updated="2026-07-03")
    v = room.answer(probe.probe_id, **resp)
    print(f"[answer] answer signed by wrong key           ->  "
          f"{'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "bad answer signature"

    # ---- 5. row-forger: row A signed by a different key ------------------
    clock, room, priv, pub, probe, knock = _fresh("rowforge@example.com")
    other_priv, _ = keys.generate_keypair()
    clock.advance(1)
    exp = room._store[probe.probe_id].expected_answer
    resp = agent_response(answer_priv=priv, row_priv=other_priv, pub=pub,
                          probe_id=probe.probe_id, who="rowforge@example.com",
                          answer=exp, description="OFFER: x", updated="2026-07-03")
    v = room.answer(probe.probe_id, **resp)
    print(f"[answer] row A signed by wrong key            ->  "
          f"{'PASS' if v.ok else 'FAIL: ' + str(v.reason) + ' ✓'}")
    assert not v.ok and v.reason == "bad row signature"

    banner("all five outcomes correct — two-signature core proven")


if __name__ == "__main__":
    main()
