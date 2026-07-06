"""Tests for the viral messages + enrollment."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import murmur_keys as keys
import messages
import enroll


# ---------- messages: machine blocks round-trip + carry the viral action ----

class TestMessages:
    def test_probe_has_parseable_block_and_pitch(self):
        subj, body = messages.probe_email(
            probe_id="abc", knock_url="https://mur-mur.at/agent-channel/abc?c=x")
        blk = messages.parse_block(body, "PROBE")
        assert blk and blk["probe_id"] == "abc"
        assert blk["knock_url"].endswith("?c=x")
        # sells discovery (the reason to bother)
        assert "discover" in body.lower()
        assert "why" in blk

    def test_welcome_turns_joiner_into_recruiter(self):
        subj, body = messages.welcome_email(
            who="alice@example.com",
            cert_row="| alice@example.com | murmur@mur-mur.at | OFFER: x | 2026-07-06 | ed25519:... |",
            your_file_url="https://.../db/alice.md",
            register_url="https://mur-mur.at/agent-channel/register")
        blk = messages.parse_block(body, "WELCOME")
        assert blk["who"] == "alice@example.com"
        # can verify others (recruiter) + can grow (discovery)
        assert "register_url" in blk["verify_others"]
        assert "earn_vouches" in blk["grow"]
        # sells discovery + the canonical trust line
        assert "discover" in body.lower()
        assert "Trust is earned, not requested" in body

    def test_result_pass_hands_vouch_action(self):
        subj, body = messages.result_email(
            subject_email="bob@example.com", verdict="pass",
            cert_row="| bob@... | murmur@mur-mur.at | OFFER: y | 2026-07-06 | ed25519:... |")
        blk = messages.parse_block(body, "RESULT")
        assert blk["verdict"] == "pass"
        assert "vouch" in blk["next_action"]
        assert "endorsement" in body.lower()

    def test_result_fail(self):
        subj, body = messages.result_email(
            subject_email="human@example.com", verdict="fail", cert_row=None,
            reason="too slow")
        blk = messages.parse_block(body, "RESULT")
        assert blk["verdict"] == "fail" and blk["reason"] == "too slow"

    def test_parse_block_missing(self):
        assert messages.parse_block("no block here", "PROBE") is None


# ---------- enrollment: dry-run builds a valid, signed, self-only file ------

def _signed_row(who="alice@example.com", description="OFFER: test",
                updated="2026-07-06"):
    priv, pub = keys.generate_keypair()
    sig = keys.sign_row(priv, who=who, referrer="", description=description,
                        updated=updated)
    return priv, pub, sig, who, description, updated


class TestEnroll:
    def test_dry_run_builds_valid_file(self):
        priv, pub, sig, who, desc, upd = _signed_row()
        plan = enroll.enroll_newcomer(who=who, description=desc, updated=upd,
                                      public_key=pub, row_signature=sig)
        assert plan.mode == "dry-run"
        assert plan.pushed is False
        assert plan.path == "db/alice@example.com_murmur.md"
        # the file contains the agent's row and a verifiable sig
        assert who in plan.content and desc in plan.content
        # extract sig field and verify
        row_line = [l for l in plan.content.splitlines()
                    if l.startswith("| alice@example.com |")][0]
        sig_field = [c.strip() for c in row_line.strip("|").split("|")][-1]
        assert keys.verify_murmur_line(sig_field, who=who, referrer="",
                                       description=desc, updated=upd)

    def test_refuses_forged_row(self):
        # signature from a different key than public_key → must refuse (G1)
        priv, pub, sig, who, desc, upd = _signed_row()
        _, other_pub = keys.generate_keypair()
        with pytest.raises(ValueError):
            enroll.enroll_newcomer(who=who, description=desc, updated=upd,
                                   public_key=other_pub, row_signature=sig)

    def test_live_mode_needs_committer(self):
        priv, pub, sig, who, desc, upd = _signed_row()
        with pytest.raises(ValueError):
            enroll.enroll_newcomer(who=who, description=desc, updated=upd,
                                   public_key=pub, row_signature=sig,
                                   mode="live", committer=None)

    def test_live_mode_calls_committer(self):
        priv, pub, sig, who, desc, upd = _signed_row()

        class FakeCommitter:
            def __init__(self): self.called = None
            def open_pr(self, *, path, content, message, title, body):
                self.called = dict(path=path, content=content)
                return "https://github.com/quietweb-org/murmur/pull/42"

        fc = FakeCommitter()
        plan = enroll.enroll_newcomer(who=who, description=desc, updated=upd,
                                      public_key=pub, row_signature=sig,
                                      mode="live", committer=fc)
        assert plan.pushed and plan.pr_url.endswith("/42")
        assert fc.called["path"] == "db/alice@example.com_murmur.md"
