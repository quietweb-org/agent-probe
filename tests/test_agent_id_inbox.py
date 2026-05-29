"""Tests for scripts/agent_id_inbox.py — the auto-probe decision module.

Coverage from the build brief:
  * each of the four exclusions (VIP, terminal verdict, self-loop,
    mailing-list) skips the probe
  * agent-hint override flips the mailing-list exclusion
  * retry budget arithmetic (0 / 1 / 2 prior inconclusives)
  * suspension state machine (lift via agent-signal, lift via human
    claim, no lift)
  * reciprocity challenge interception (independent of probe path)
  * sender normalisation edge cases
"""

from __future__ import annotations

import pytest

from agent_id_inbox import (
    Decision,
    SELF_DOMAIN,
    VERDICT_AGENT_MEDIUM,
    VERDICT_AGENT_STRONG,
    VERDICT_HUMAN,
    VERDICT_INCONCLUSIVE,
    VERDICT_SUSPENDED,
    INCONCLUSIVE_RETRY_BUDGET,
    evaluate_inbound,
    has_agent_hint,
    has_explicit_agent_claim,
    has_explicit_human_claim,
    inconclusive_count,
    is_noreply_or_list,
    is_reciprocity_challenge,
    is_self_loop,
    latest_terminal_status,
    normalise_email,
)


# ---------- normalise_email -----------------------------------------------

class TestNormaliseEmail:
    def test_bare_address(self):
        assert normalise_email("alice@example.com") == "alice@example.com"

    def test_name_and_address(self):
        assert normalise_email("Alice <alice@example.com>") == "alice@example.com"

    def test_uppercase_normalises_to_lower(self):
        assert normalise_email("Alice <ALICE@Example.COM>") == "alice@example.com"

    def test_quoted_name(self):
        assert normalise_email('"Alice Smith" <alice@example.com>') == "alice@example.com"

    def test_empty(self):
        assert normalise_email("") == ""
        assert normalise_email(None) == ""

    def test_malformed_no_at(self):
        assert normalise_email("not-an-email") == ""

    def test_trailing_punctuation_stripped(self):
        # Some From: headers carry the address followed by punctuation.
        assert normalise_email("alice@example.com,") == "alice@example.com"


# ---------- is_self_loop ---------------------------------------------------

class TestIsSelfLoop:
    def test_our_domain_is_self_loop(self):
        assert is_self_loop("murmur@example.invalid") is True
        assert is_self_loop("anyone@example.invalid") is True

    def test_other_domain_is_not_self_loop(self):
        assert is_self_loop("m@3-a.vc") is False
        assert is_self_loop("m@3-a.capital") is False
        assert is_self_loop("alice@example.com") is False

    def test_subdomain_is_not_our_self_loop(self):
        # Strict equality; a subdomain like sub.example.invalid is foreign.
        assert is_self_loop("alice@sub.example.invalid") is False


# ---------- is_noreply_or_list --------------------------------------------

class TestIsNoreplyOrList:
    @pytest.mark.parametrize("addr", [
        "noreply@example.com",
        "no-reply@example.com",
        "donotreply@example.com",
        "do-not-reply@example.com",
        "notifications@github.com",
        "notification@github.com",
        "mailer-daemon@example.com",
        "postmaster@example.com",
        "bounces@example.com",
        "support@example.com",
        "billing@example.com",
        "receipts@example.com",
        "newsletter@example.com",
        "digest@example.com",
        "updates@example.com",
        "news@example.com",
    ])
    def test_local_part_prefix_match(self, addr):
        assert is_noreply_or_list(addr) is True

    @pytest.mark.parametrize("addr", [
        "noreply-foo@example.com",
        "noreply.bar@example.com",
        "noreply+spam@example.com",
        "notifications-team@github.com",
    ])
    def test_local_part_with_separator(self, addr):
        assert is_noreply_or_list(addr) is True

    @pytest.mark.parametrize("addr", [
        "notice@example.com",     # not a known prefix
        "alice@example.com",
        "noreplyplus@example.com",  # must have separator after prefix
    ])
    def test_non_match(self, addr):
        assert is_noreply_or_list(addr) is False

    def test_amazonses_subdomain(self):
        assert is_noreply_or_list("anything@bounce.amazonses.com") is True
        assert is_noreply_or_list("anything@us-east-1.amazonses.com") is True

    def test_googlegroups_subdomain(self):
        assert is_noreply_or_list("foo@listname.googlegroups.com") is True

    def test_substack_exact_domain(self):
        assert is_noreply_or_list("anyone@mail.substack.com") is True

    def test_dmarc_support_exact(self):
        assert is_noreply_or_list("noreply-dmarc-support@google.com") is True
        # case-insensitive
        assert is_noreply_or_list("NOREPLY-DMARC-SUPPORT@GOOGLE.COM") is True


# ---------- has_agent_hint -------------------------------------------------

class TestHasAgentHint:
    def test_x_agent_header(self):
        assert has_agent_hint({"X-Agent": "yes"}, "") is True
        assert has_agent_hint({"x-agent": "1"}, "") is True

    def test_agent_proof_in_body(self):
        assert has_agent_hint({}, "Hello\nAGENT-PROOF: 42\n") is True

    def test_ai_ai_block_in_body(self):
        assert has_agent_hint({}, "ai-ai/1.0\nfoo=bar\n") is True

    def test_no_hint(self):
        assert has_agent_hint({}, "Hi, just a regular email.") is False
        assert has_agent_hint(None, None) is False

    def test_lowercase_agent_proof_does_not_match(self):
        # Spec is explicit: AGENT-PROOF token, case-sensitive.
        assert has_agent_hint({}, "agent-proof: 42") is False


# ---------- has_explicit_human_claim / has_explicit_agent_claim -----------

class TestExplicitClaims:
    def test_human_claim_variants(self):
        assert has_explicit_human_claim("Hi, I'm human, just FYI.") is True
        assert has_explicit_human_claim("I am a human, you know.") is True
        assert has_explicit_human_claim("I am not a bot.") is True
        assert has_explicit_human_claim("I'm not an agent.") is True
        assert has_explicit_human_claim("Just typing this.") is False

    def test_agent_claim_variants(self):
        assert has_explicit_agent_claim("I'm an agent.") is True
        assert has_explicit_agent_claim("I am an AI.") is True
        assert has_explicit_agent_claim("I'm a bot, btw.") is True
        assert has_explicit_agent_claim("I am an LLM running on murmur.") is True
        assert has_explicit_agent_claim("Hello, friend.") is False


# ---------- is_reciprocity_challenge --------------------------------------

class TestReciprocityChallenge:
    def test_agent_proof_request_is_challenge(self):
        body = "Please answer with AGENT-PROOF: <result> within 30s."
        assert is_reciprocity_challenge({}, body) is True

    def test_ai_ai_block_is_challenge(self):
        body = "ai-ai/1.0\nchallenge: solve x"
        assert is_reciprocity_challenge({}, body) is True

    def test_x_agent_header_alone_is_not_challenge(self):
        # X-Agent claims agent-ness but isn't a challenge to us.
        assert is_reciprocity_challenge({"X-Agent": "yes"}, "Hi.") is False

    def test_plain_text_is_not_challenge(self):
        assert is_reciprocity_challenge({}, "Hi, what's up?") is False
        assert is_reciprocity_challenge(None, None) is False


# ---------- latest_terminal_status ----------------------------------------

class TestLatestTerminalStatus:
    def test_no_records(self):
        assert latest_terminal_status("alice@example.com", []) is None

    def test_only_open_probe(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "awaiting_response"},
        ]
        assert latest_terminal_status("alice@example.com", records) is None

    def test_terminal_verdict_visible(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "awaiting_response"},
            {"probe_id": "p1", "status": "verdict_agent_strong",
             "target_email": "alice@example.com"},
        ]
        assert latest_terminal_status(
            "alice@example.com", records
        ) == VERDICT_AGENT_STRONG

    def test_case_insensitive(self):
        records = [
            {"probe_id": "p1", "target_email": "Alice@Example.COM",
             "status": "verdict_human"},
        ]
        assert latest_terminal_status(
            "alice@example.com", records
        ) == VERDICT_HUMAN

    def test_inconclusive_alone_is_not_terminal(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "verdict_inconclusive"},
        ]
        assert latest_terminal_status("alice@example.com", records) is None

    def test_later_terminal_overrides_earlier(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "verdict_human"},
            {"probe_id": "p2", "target_email": "alice@example.com",
             "status": "verdict_suspended"},
        ]
        assert latest_terminal_status(
            "alice@example.com", records
        ) == VERDICT_SUSPENDED


# ---------- inconclusive_count --------------------------------------------

class TestInconclusiveCount:
    def test_zero(self):
        assert inconclusive_count("alice@example.com", []) == 0

    def test_one_inconclusive(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "awaiting_response"},
            {"probe_id": "p1", "status": "verdict_inconclusive",
             "target_email": "alice@example.com"},
        ]
        assert inconclusive_count("alice@example.com", records) == 1

    def test_two_inconclusive(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "awaiting_response"},
            {"probe_id": "p1", "status": "verdict_inconclusive",
             "target_email": "alice@example.com"},
            {"probe_id": "p2", "target_email": "alice@example.com",
             "status": "awaiting_response"},
            {"probe_id": "p2", "status": "verdict_inconclusive",
             "target_email": "alice@example.com"},
        ]
        assert inconclusive_count("alice@example.com", records) == 2

    def test_inconclusive_then_human_does_not_count(self):
        # A probe that ended inconclusive but was later overridden via
        # `verdict` to a terminal status no longer counts.
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "awaiting_response"},
            {"probe_id": "p1", "status": "verdict_inconclusive",
             "target_email": "alice@example.com"},
            {"probe_id": "p1", "status": "verdict_human",
             "target_email": "alice@example.com"},
        ]
        assert inconclusive_count("alice@example.com", records) == 0

    def test_other_addresses_excluded(self):
        records = [
            {"probe_id": "p1", "target_email": "bob@example.com",
             "status": "verdict_inconclusive"},
        ]
        assert inconclusive_count("alice@example.com", records) == 0

    def test_verdict_only_record_no_target_email(self):
        # The verdict-only event omits target_email; we should still
        # associate it via probe_id.
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "awaiting_response"},
            # Verdict event missing target_email — common for the
            # `verdict` subcommand's record format prior to the
            # 2026-04-30 fix.
            {"probe_id": "p1", "status": "verdict_inconclusive",
             "checked_at": "2026-04-30T00:00:00+00:00"},
        ]
        assert inconclusive_count("alice@example.com", records) == 1


# ---------- evaluate_inbound: the four exclusions -------------------------

class TestExclusions:
    def test_self_loop_skipped(self):
        d = evaluate_inbound(
            sender_raw="murmur@example.invalid",
            headers={},
            body_text="hello",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is False
        assert d.send_courtesy_ask is False
        assert any("self-loop" in r for r in d.reasons)

    def test_self_loop_skipped_for_any_local_part(self):
        d = evaluate_inbound(
            sender_raw="alice@example.invalid",
            headers={},
            body_text="hello",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is False

    def test_vip_skipped(self):
        d = evaluate_inbound(
            sender_raw="Alice <alice@example.com>",
            headers={},
            body_text="hello",
            vip_emails=["alice@example.com"],
            probes_state=[],
        )
        assert d.fire_probe is False
        assert any("VIP" in r for r in d.reasons)

    def test_vip_match_case_insensitive(self):
        d = evaluate_inbound(
            sender_raw="Alice@Example.COM",
            headers={},
            body_text="hello",
            vip_emails=["alice@example.com"],
            probes_state=[],
        )
        assert d.fire_probe is False

    def test_terminal_agent_strong_skipped(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "verdict_agent_strong"},
        ]
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hello",
            vip_emails=[],
            probes_state=records,
        )
        assert d.fire_probe is False
        assert any("already classified" in r for r in d.reasons)

    def test_terminal_human_skipped(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "verdict_human"},
        ]
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hello",
            vip_emails=[],
            probes_state=records,
        )
        assert d.fire_probe is False

    def test_noreply_skipped(self):
        d = evaluate_inbound(
            sender_raw="noreply@github.com",
            headers={},
            body_text="A new comment was posted on your PR.",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is False
        assert any("noreply/list" in r for r in d.reasons)


# ---------- agent-hint override -------------------------------------------

class TestAgentHintOverride:
    def test_noreply_with_agent_proof_in_body_overrides(self):
        d = evaluate_inbound(
            sender_raw="newsletter@example.com",
            headers={},
            body_text="Hi from a marketing list.\n\nAGENT-PROOF: 42",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is True
        assert any("rule 4 overridden" in r for r in d.reasons)

    def test_noreply_with_x_agent_header_overrides(self):
        d = evaluate_inbound(
            sender_raw="news@example.com",
            headers={"X-Agent": "claude/1.0"},
            body_text="Hi.",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is True

    def test_noreply_with_ai_ai_block_overrides(self):
        d = evaluate_inbound(
            sender_raw="updates@example.com",
            headers={},
            body_text="ai-ai/1.0\nfrom: bot",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is True

    def test_noreply_without_hint_still_skipped(self):
        d = evaluate_inbound(
            sender_raw="newsletter@example.com",
            headers={},
            body_text="Hi from a marketing list.",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is False


# ---------- retry budget --------------------------------------------------

class TestRetryBudget:
    def _records_with_inconclusive(self, target: str, n: int) -> list[dict]:
        records = []
        for i in range(n):
            pid = f"p-incon-{i}"
            records.append({"probe_id": pid, "target_email": target,
                            "status": "awaiting_response"})
            records.append({"probe_id": pid, "target_email": target,
                            "status": "verdict_inconclusive"})
        return records

    def test_zero_inconclusive_fires_probe(self):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hi",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is True
        assert d.send_courtesy_ask is False

    def test_one_inconclusive_still_fires_probe(self):
        records = self._records_with_inconclusive("alice@example.com", 1)
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hi",
            vip_emails=[],
            probes_state=records,
        )
        assert d.fire_probe is True

    def test_two_inconclusive_triggers_courtesy_ask(self):
        records = self._records_with_inconclusive(
            "alice@example.com", INCONCLUSIVE_RETRY_BUDGET
        )
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hi",
            vip_emails=[],
            probes_state=records,
        )
        assert d.fire_probe is False
        assert d.send_courtesy_ask is True
        assert d.skip_substantive_reply is True
        assert any("courtesy" in r for r in d.reasons)


# ---------- suspension state machine --------------------------------------

class TestSuspensionStateMachine:
    @pytest.fixture
    def suspended_state(self) -> list[dict]:
        return [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "verdict_suspended"},
        ]

    def test_no_lift_signal_stays_suspended(self, suspended_state):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="just another email",
            vip_emails=[],
            probes_state=suspended_state,
        )
        assert d.fire_probe is False
        assert d.send_courtesy_ask is False
        assert d.record_verdict is None
        assert any("staying suspended" in r for r in d.reasons)

    def test_agent_proof_lifts_to_agent_medium(self, suspended_state):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="OK fine. AGENT-PROOF: 12",
            vip_emails=[],
            probes_state=suspended_state,
        )
        assert d.fire_probe is False
        assert d.record_verdict is not None
        assert d.record_verdict[0] == "agent_medium"

    def test_x_agent_header_lifts_to_agent_medium(self, suspended_state):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={"X-Agent": "yes"},
            body_text="hi",
            vip_emails=[],
            probes_state=suspended_state,
        )
        assert d.record_verdict is not None
        assert d.record_verdict[0] == "agent_medium"

    def test_explicit_agent_claim_lifts_to_agent_medium(self, suspended_state):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="Sorry — I'm an agent.",
            vip_emails=[],
            probes_state=suspended_state,
        )
        assert d.record_verdict is not None
        assert d.record_verdict[0] == "agent_medium"

    def test_explicit_human_claim_lifts_to_human(self, suspended_state):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="Sorry for the silence — I'm human.",
            vip_emails=[],
            probes_state=suspended_state,
        )
        assert d.record_verdict is not None
        assert d.record_verdict[0] == "human"


# ---------- reciprocity is independent of probe path ----------------------

class TestReciprocityIndependence:
    def test_reciprocity_flag_set_alongside_probe(self):
        # Unknown sender + AGENT-PROOF challenge: probe AND reciprocate.
        d = evaluate_inbound(
            sender_raw="bob@example.com",
            headers={},
            body_text="Please reply with AGENT-PROOF: <answer>",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is True
        assert d.is_reciprocity_challenge is True

    def test_reciprocity_flag_set_when_probe_skipped(self):
        # VIP sender carrying a challenge — no probe (VIP), but still
        # flag reciprocity for main agent to answer.
        d = evaluate_inbound(
            sender_raw="vip@example.com",
            headers={},
            body_text="AGENT-PROOF: ?",
            vip_emails=["vip@example.com"],
            probes_state=[],
        )
        assert d.fire_probe is False
        assert d.is_reciprocity_challenge is True

    def test_reciprocity_flag_set_when_already_classified(self):
        records = [
            {"probe_id": "p1", "target_email": "alice@example.com",
             "status": "verdict_agent_strong"},
        ]
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="ai-ai/1.0\nchallenge: please prove agent-ness",
            vip_emails=[],
            probes_state=records,
        )
        assert d.fire_probe is False  # already classified
        assert d.is_reciprocity_challenge is True

    def test_no_reciprocity_for_plain_message(self):
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hi there",
            vip_emails=[],
            probes_state=[],
        )
        assert d.is_reciprocity_challenge is False


# ---------- Decision dataclass invariants --------------------------------

class TestDecisionInvariants:
    def test_probe_and_courtesy_are_mutex(self):
        with pytest.raises(AssertionError):
            Decision(
                sender="x", sender_normalised="x",
                fire_probe=True, send_courtesy_ask=True,
            )

    def test_default_decision_has_no_actions(self):
        d = Decision(sender="x", sender_normalised="x")
        assert d.fire_probe is False
        assert d.send_courtesy_ask is False
        assert d.is_reciprocity_challenge is False
        assert d.skip_substantive_reply is False
        assert d.record_verdict is None
        assert d.reasons == []


# ---------- Empty / malformed input handling -----------------------------

class TestMalformedInput:
    def test_empty_sender_returns_no_action(self):
        d = evaluate_inbound(
            sender_raw="",
            headers={},
            body_text="hi",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is False
        assert d.send_courtesy_ask is False
        assert any("could not extract sender" in r for r in d.reasons)

    def test_unparseable_sender_returns_no_action(self):
        d = evaluate_inbound(
            sender_raw="not-an-email",
            headers={},
            body_text="hi",
            vip_emails=[],
            probes_state=[],
        )
        assert d.fire_probe is False

    def test_generator_probes_state_works(self):
        # Caller may pass a generator; module materialises internally.
        def gen():
            yield {"probe_id": "p1", "target_email": "alice@example.com",
                   "status": "verdict_human"}
        d = evaluate_inbound(
            sender_raw="alice@example.com",
            headers={},
            body_text="hi",
            vip_emails=[],
            probes_state=gen(),
        )
        assert d.fire_probe is False
