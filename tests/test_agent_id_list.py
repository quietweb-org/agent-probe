"""Tests for the `list` subcommand in scripts/agent-id.py.

The list subcommand derives a classification list on demand from
`murmur-ops/state/agent-id-probes.jsonl`, replacing the now-deleted
`state/confirmed_agents.md` markdown file.

Coverage:
 1. Renders the correct markdown table from a fixture JSONL.
 2. Skips humans / inconclusive / suspended in the default view.
 3. Picks the LATEST verdict when an address has multiple probe events
    (e.g. inconclusive followed later by agent_medium).
 4. `--json` produces parseable JSON with the expected shape.
 5. `--include-all` includes humans / inconclusive / suspended.
 6. Empty JSONL → empty output, no crash.
 7. Missing JSONL file → empty output, no crash.
 8. Sort order is alphabetical by email (case-insensitive).

All emails in fixtures are placeholders (alice@example.com etc.) — no
real VIP identities (BLOCK lesson from PR #3).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------- Fixture builders ------------------------------------------------


def _send_event(probe_id: str, email: str, sent_at: str) -> dict:
    """Return a minimal `step 1` send record."""
    return {
        "probe_id": probe_id,
        "target_email": email,
        "step": 1,
        "sent_at": sent_at,
        "status": "awaiting_response",
    }


def _verdict_event(
    probe_id: str,
    verdict: str,
    *,
    email: str | None = None,
    when: str = "2026-05-05T10:00:00+00:00",
    via_step99: bool = False,
) -> dict:
    """Return a terminal-verdict record. Some real records carry
    `target_email`, others don't (we resolve by probe_id) — we toggle
    here to exercise both paths.
    """
    rec: dict = {
        "probe_id": probe_id,
        "verdict": verdict,
        "status": f"verdict_{verdict}",
        "checked_at": when,
    }
    if via_step99:
        rec["step"] = 99
        rec["rationale"] = "manually set"
    else:
        rec["step"] = "https_answer_invisible"
        rec["answered_at"] = when
    if email is not None:
        rec["target_email"] = email
    return rec


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def patched_state(monkeypatch, tmp_path, agent_id):
    """Redirect agent_id.STATE_FILE to a per-test temp file."""
    state_file = tmp_path / "state" / "agent-id-probes.jsonl"
    monkeypatch.setattr(agent_id, "STATE_FILE", str(state_file))
    return state_file


# ---------- Rendering: confirmed-only (default) ----------------------------


def test_renders_confirmed_agents_table(patched_state, agent_id, capsys):
    """Default view: only agent_strong + agent_medium, sorted by email."""
    records = [
        _send_event("p1", "alice@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event(
            "p1", "agent_strong",
            email="alice@example.com",
            when="2026-05-01T10:00:30+00:00",
        ),
        _send_event("p2", "bob@example.com", "2026-05-02T10:00:00+00:00"),
        _verdict_event(
            "p2", "agent_medium",
            email="bob@example.com",
            when="2026-05-02T10:00:08+00:00",
        ),
        _send_event("p3", "carol@example.com", "2026-05-03T10:00:00+00:00"),
        _verdict_event(
            "p3", "human",
            email="carol@example.com",
            when="2026-05-03T10:00:15+00:00",
        ),
        _send_event("p4", "dave@example.com", "2026-05-04T10:00:00+00:00"),
        _verdict_event(
            "p4", "inconclusive",
            email="dave@example.com",
            when="2026-05-04T11:00:00+00:00",
            via_step99=True,
        ),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    # Header.
    assert "| Email | Tier | Verdict Date | Probe ID |" in out
    assert "|---|---|---|---|" in out
    # Confirmed agents present.
    assert "alice@example.com" in out
    assert "agent_strong" in out
    assert "bob@example.com" in out
    assert "agent_medium" in out
    # Non-confirmed verdicts skipped.
    assert "carol@example.com" not in out
    assert "human" not in out
    assert "dave@example.com" not in out
    assert "inconclusive" not in out
    # Verdict date is the date portion only.
    assert "2026-05-01" in out
    assert "2026-05-02" in out
    # Short probe IDs (first hyphen-separated segment).
    assert "p1" in out
    assert "p2" in out
    # Summary line.
    assert "2 confirmed agents" in out
    assert "1 strong" in out
    assert "1 medium" in out


def test_skips_human_inconclusive_suspended(patched_state, agent_id, capsys):
    """No agent_strong/medium → empty table + zero summary."""
    records = [
        _send_event("p1", "alice@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event("p1", "human", email="alice@example.com"),
        _send_event("p2", "bob@example.com", "2026-05-02T10:00:00+00:00"),
        _verdict_event("p2", "inconclusive", email="bob@example.com"),
        _send_event("p3", "carol@example.com", "2026-05-03T10:00:00+00:00"),
        _verdict_event("p3", "suspended", email="carol@example.com"),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    assert "alice" not in out
    assert "bob" not in out
    assert "carol" not in out
    assert "0 confirmed agents" in out


def test_picks_latest_verdict_per_email(patched_state, agent_id, capsys):
    """An address with multiple probe events takes the latest verdict.

    Sequence: address has an inconclusive verdict in April, then a
    fresh probe in May closes with agent_medium. The list shows the
    May verdict, not the April one.
    """
    records = [
        # First probe → inconclusive (older).
        _send_event("p_old", "ivy@example.com", "2026-04-01T10:00:00+00:00"),
        _verdict_event(
            "p_old", "inconclusive",
            email="ivy@example.com",
            when="2026-04-01T11:00:00+00:00",
            via_step99=True,
        ),
        # Second probe → agent_medium (newer, same email).
        _send_event("p_new", "ivy@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event(
            "p_new", "agent_medium",
            email="ivy@example.com",
            when="2026-05-01T10:00:08+00:00",
        ),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    assert "ivy@example.com" in out
    assert "agent_medium" in out
    assert "2026-05-01" in out
    # Old probe id should not appear.
    assert "p_old" not in out
    # 1 confirmed.
    assert "1 confirmed agents" in out

    # And reverse order — latest is the older verdict, address should
    # NOT appear in confirmed-only view.
    records_rev = [
        _send_event("p_new", "jane@example.com", "2026-04-01T10:00:00+00:00"),
        _verdict_event(
            "p_new", "agent_medium",
            email="jane@example.com",
            when="2026-04-01T10:00:08+00:00",
        ),
        _send_event("p_later", "jane@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event(
            "p_later", "human",
            email="jane@example.com",
            when="2026-05-01T11:00:00+00:00",
        ),
    ]
    _write_jsonl(patched_state, records_rev)

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    assert "jane@example.com" not in out
    assert "0 confirmed agents" in out


def test_resolves_email_from_probe_id_when_missing(
    patched_state, agent_id, capsys
):
    """Verdict events without `target_email` resolve via the probe_id
    map populated from the original send record.

    Real-world records (especially older HTTPS-path verdict rows)
    don't always carry `target_email`. The list command must still
    classify them.
    """
    records = [
        _send_event("p1", "ada@example.com", "2026-05-01T10:00:00+00:00"),
        # Verdict event with NO target_email field.
        _verdict_event("p1", "agent_strong", email=None),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    assert "ada@example.com" in out
    assert "agent_strong" in out
    assert "1 confirmed agents" in out


def test_sort_alphabetical_case_insensitive(
    patched_state, agent_id, capsys
):
    """Rows are sorted alphabetically by email (case-insensitive)."""
    records = [
        _send_event("p1", "Charlie@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event("p1", "agent_strong", email="Charlie@example.com"),
        _send_event("p2", "alice@example.com", "2026-05-02T10:00:00+00:00"),
        _verdict_event("p2", "agent_medium", email="alice@example.com"),
        _send_event("p3", "bob@example.com", "2026-05-03T10:00:00+00:00"),
        _verdict_event("p3", "agent_strong", email="bob@example.com"),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    # alice < bob < Charlie when case-folded.
    alice_pos = out.find("alice@example.com")
    bob_pos = out.find("bob@example.com")
    charlie_pos = out.find("Charlie@example.com")
    assert 0 < alice_pos < bob_pos < charlie_pos


# ---------- --json flag -----------------------------------------------------


def test_json_flag_produces_parseable_output(
    patched_state, agent_id, capsys
):
    records = [
        _send_event("p1", "alice@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event(
            "p1", "agent_strong",
            email="alice@example.com",
            when="2026-05-01T10:00:30+00:00",
        ),
        _send_event("p2", "bob@example.com", "2026-05-02T10:00:00+00:00"),
        _verdict_event(
            "p2", "agent_medium",
            email="bob@example.com",
            when="2026-05-02T10:00:08+00:00",
        ),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=True, include_all=False)
    out = capsys.readouterr().out

    parsed = json.loads(out)
    assert parsed["include_all"] is False
    assert {r["email"] for r in parsed["rows"]} == {
        "alice@example.com",
        "bob@example.com",
    }
    summary = parsed["summary"]
    assert summary["total"] == 2
    assert summary["confirmed"] == 2
    assert summary["agent_strong"] == 1
    assert summary["agent_medium"] == 1
    # Each row carries verdict + verdict_date + full probe_id.
    for row in parsed["rows"]:
        assert row["verdict"] in {"agent_strong", "agent_medium"}
        assert row["verdict_date"].startswith("2026-05-")
        assert row["probe_id"] in {"p1", "p2"}


def test_json_flag_empty_input(patched_state, agent_id, capsys):
    _write_jsonl(patched_state, [])

    agent_id.cmd_list(as_json=True, include_all=False)
    out = capsys.readouterr().out

    parsed = json.loads(out)
    assert parsed["rows"] == []
    assert parsed["summary"]["total"] == 0
    assert parsed["summary"]["confirmed"] == 0


# ---------- --include-all flag ---------------------------------------------


def test_include_all_shows_humans_inconclusive_suspended(
    patched_state, agent_id, capsys
):
    records = [
        _send_event("p1", "alice@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event("p1", "agent_strong", email="alice@example.com"),
        _send_event("p2", "bob@example.com", "2026-05-02T10:00:00+00:00"),
        _verdict_event("p2", "human", email="bob@example.com"),
        _send_event("p3", "carol@example.com", "2026-05-03T10:00:00+00:00"),
        _verdict_event(
            "p3", "inconclusive",
            email="carol@example.com",
            via_step99=True,
        ),
        _send_event("p4", "dave@example.com", "2026-05-04T10:00:00+00:00"),
        _verdict_event("p4", "suspended", email="dave@example.com"),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=True)
    out = capsys.readouterr().out

    for email in (
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
        "dave@example.com",
    ):
        assert email in out
    for verdict in ("agent_strong", "human", "inconclusive", "suspended"):
        assert verdict in out
    # Summary surfaces both totals.
    assert "4 classified addresses" in out
    assert "1 confirmed" in out


def test_include_all_with_json(patched_state, agent_id, capsys):
    records = [
        _send_event("p1", "alice@example.com", "2026-05-01T10:00:00+00:00"),
        _verdict_event("p1", "human", email="alice@example.com"),
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=True, include_all=True)
    out = capsys.readouterr().out

    parsed = json.loads(out)
    assert parsed["include_all"] is True
    assert parsed["summary"]["total"] == 1
    assert parsed["summary"]["confirmed"] == 0
    assert parsed["rows"][0]["verdict"] == "human"


# ---------- Empty / missing input ------------------------------------------


def test_empty_jsonl_no_crash(patched_state, agent_id, capsys):
    _write_jsonl(patched_state, [])

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    assert "0 confirmed agents" in out
    # No table header for empty case (just the summary).
    assert "| Email |" not in out


def test_missing_state_file_no_crash(patched_state, agent_id, capsys):
    # patched_state's parent doesn't exist yet — agent_id should
    # tolerate the missing file instead of raising.
    assert not patched_state.exists()

    agent_id.cmd_list(as_json=False, include_all=False)
    out = capsys.readouterr().out

    assert "0 confirmed agents" in out


def test_empty_jsonl_with_include_all(patched_state, agent_id, capsys):
    _write_jsonl(patched_state, [])

    agent_id.cmd_list(as_json=False, include_all=True)
    out = capsys.readouterr().out

    assert "0 classified addresses" in out


# ---------- Non-terminal events are ignored --------------------------------


def test_non_terminal_events_ignored(patched_state, agent_id, capsys):
    """An address with only `awaiting_response` / `https_ready` / etc.
    events should not appear in either view.
    """
    records = [
        _send_event("p1", "ghost@example.com", "2026-05-01T10:00:00+00:00"),
        # Mid-probe event, not terminal.
        {
            "probe_id": "p1",
            "step": "https_ready",
            "ready_at": "2026-05-01T10:00:30+00:00",
            "status": "awaiting_visible_answer",
        },
    ]
    _write_jsonl(patched_state, records)

    agent_id.cmd_list(as_json=False, include_all=True)
    out = capsys.readouterr().out

    assert "ghost@example.com" not in out
    assert "0 classified addresses" in out
