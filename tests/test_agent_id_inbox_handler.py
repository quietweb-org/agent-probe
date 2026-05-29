"""Tests for scripts/agent-id-inbox-handler.py (the wiring shim).

Coverage:
  * VIP list parsing from a markdown table
  * State JSONL load (incl. malformed line tolerance)
  * main() smoke test for each Decision branch, with subprocess + himalaya
    + state-file writes mocked or redirected to tmp_path
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "agent-id-inbox-handler.py"
)


@pytest.fixture(scope="module")
def handler():
    """Load the hyphen-named handler script as a module via importlib."""
    spec = importlib.util.spec_from_file_location(
        "agent_id_inbox_handler", HANDLER_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_id_inbox_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------- VIP list parsing -----------------------------------------------

class TestLoadVipEmails:
    def test_parses_canonical_format(self, handler, tmp_path,
                                     monkeypatch):
        vip = tmp_path / "vip_list.md"
        vip.write_text(
            "# VIP List\n"
            "\n"
            "| # | Contact ID | Name | Email | Notes |\n"
            "|---|---|---|---|---|\n"
            "| 1 | CON-TEST-001 | Alice Example | `alice@example.com` | x |\n"
            "| 2 | CON-TEST-002 | Bob Example | `bob@example.com` | y |\n"
        )
        monkeypatch.setattr(handler, "VIP_LIST_FILE", str(vip))
        emails = handler.load_vip_emails()
        assert sorted(emails) == [
            "alice@example.com", "bob@example.com"
        ]

    def test_missing_file_returns_empty(self, handler, tmp_path,
                                       monkeypatch):
        monkeypatch.setattr(
            handler, "VIP_LIST_FILE", str(tmp_path / "does-not-exist.md")
        )
        assert handler.load_vip_emails() == []

    def test_addresses_without_backticks_skipped(self, handler, tmp_path,
                                                 monkeypatch):
        vip = tmp_path / "vip_list.md"
        vip.write_text("Free text alice@example.com (not in backticks)\n")
        monkeypatch.setattr(handler, "VIP_LIST_FILE", str(vip))
        assert handler.load_vip_emails() == []


# ---------- State load/append ---------------------------------------------

class TestStateIO:
    def test_load_empty_when_file_missing(self, handler, tmp_path,
                                          monkeypatch):
        monkeypatch.setattr(
            handler, "STATE_FILE", str(tmp_path / "missing.jsonl")
        )
        assert handler.load_probes_state() == []

    def test_load_skips_malformed_lines(self, handler, tmp_path,
                                        monkeypatch):
        f = tmp_path / "agent-id-probes.jsonl"
        f.write_text(
            '{"probe_id": "p1", "status": "verdict_human"}\n'
            "this is not json\n"
            '{"probe_id": "p2", "status": "awaiting_response"}\n'
            "\n"
        )
        monkeypatch.setattr(handler, "STATE_FILE", str(f))
        rows = handler.load_probes_state()
        assert len(rows) == 2
        assert rows[0]["probe_id"] == "p1"
        assert rows[1]["probe_id"] == "p2"

    def test_append_creates_parent_dir_and_writes(self, handler, tmp_path,
                                                  monkeypatch):
        f = tmp_path / "nested" / "agent-id-probes.jsonl"
        monkeypatch.setattr(handler, "STATE_FILE", str(f))
        handler.append_state({"probe_id": "x", "status": "verdict_human"})
        handler.append_state({"probe_id": "y", "status": "verdict_human"})
        rows = handler.load_probes_state()
        assert [r["probe_id"] for r in rows] == ["x", "y"]


# ---------- main() smoke tests --------------------------------------------

@pytest.fixture
def isolated_state(handler, tmp_path, monkeypatch):
    """Redirect VIP and STATE files into tmp_path, return paths."""
    vip = tmp_path / "vip_list.md"
    vip.write_text(
        "| # | Contact ID | Name | Email | Notes |\n"
        "|---|---|---|---|---|\n"
        "| 1 | CON-TEST-001 | VIP | `vip@example.com` | x |\n"
    )
    state = tmp_path / "agent-id-probes.jsonl"
    state.write_text("")
    monkeypatch.setattr(handler, "VIP_LIST_FILE", str(vip))
    monkeypatch.setattr(handler, "STATE_FILE", str(state))
    return {"vip": vip, "state": state, "dir": tmp_path}


def _run_main(handler, payload: dict, monkeypatch) -> dict:
    """Invoke main() with `payload` on stdin and return the parsed JSON
    output."""
    buf_out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", buf_out)
    rc = handler.main()
    return rc, json.loads(buf_out.getvalue())


class TestMainSmoke:
    def test_skip_self_loop(self, handler, isolated_state, monkeypatch):
        rc, out = _run_main(
            handler,
            {
                "sender_raw": "anyone@mur-mur.at",
                "headers": {},
                "body_text": "hi",
            },
            monkeypatch,
        )
        assert rc == 0
        assert out["ok"] is True
        assert out["fire_probe"] is False
        assert any("self-loop" in r for r in out["reasons"])

    def test_skip_vip(self, handler, isolated_state, monkeypatch):
        rc, out = _run_main(
            handler,
            {
                "sender_raw": "VIP <vip@example.com>",
                "headers": {},
                "body_text": "hi",
            },
            monkeypatch,
        )
        assert rc == 0
        assert out["fire_probe"] is False
        assert any("VIP" in r for r in out["reasons"])

    def test_fire_probe_calls_agent_id_send(self, handler, isolated_state,
                                            monkeypatch):
        captured = {}

        def fake_run(cmd, capture_output, text, timeout):
            # Verify the command shape; return a plausible JSON result.
            assert cmd[1].endswith("agent-id.py"), cmd
            assert cmd[2] == "send"
            assert cmd[3] == "alice@example.com"
            captured["cmd"] = cmd
            return _CompletedProcess(
                stdout=json.dumps({
                    "ok": True,
                    "probe_id": "uuid-fake-1",
                    "target_email": cmd[3],
                }),
                stderr="",
                returncode=0,
            )

        monkeypatch.setattr(handler.subprocess, "run", fake_run)

        rc, out = _run_main(
            handler,
            {
                "sender_raw": "Alice <alice@example.com>",
                "headers": {},
                "body_text": "hi",
            },
            monkeypatch,
        )
        assert rc == 0
        assert out["fire_probe"] is True
        assert out["probe_result"]["ok"] is True
        assert out["probe_result"]["probe_id"] == "uuid-fake-1"
        assert out["skip_substantive_reply"] is True
        assert "cmd" in captured

    def test_fire_probe_failure_propagates_error(self, handler,
                                                 isolated_state,
                                                 monkeypatch):
        def fake_run(cmd, capture_output, text, timeout):
            return _CompletedProcess(
                stdout="", stderr="boom", returncode=1
            )

        monkeypatch.setattr(handler.subprocess, "run", fake_run)
        rc, out = _run_main(
            handler,
            {
                "sender_raw": "alice@example.com",
                "headers": {},
                "body_text": "hi",
            },
            monkeypatch,
        )
        assert rc == 3
        assert out["ok"] is False
        assert "boom" in (out.get("error") or "")

    def test_courtesy_ask_after_two_inconclusive(self, handler,
                                                 isolated_state,
                                                 monkeypatch):
        # Pre-seed state with two inconclusive probes for the address.
        state = isolated_state["state"]
        state.write_text(
            json.dumps({"probe_id": "pa", "target_email": "alice@example.com",
                        "status": "verdict_inconclusive"}) + "\n"
            + json.dumps({"probe_id": "pb",
                          "target_email": "alice@example.com",
                          "status": "verdict_inconclusive"}) + "\n"
        )

        # Mock himalaya send (returncode 0).
        def fake_run(cmd, **kwargs):
            assert cmd[0].endswith("himalaya")
            return _CompletedProcess(stdout="OK", stderr="", returncode=0)

        # Pretend himalaya exists.
        monkeypatch.setattr(handler.os.path, "exists", _tolerant_exists)
        monkeypatch.setattr(handler.subprocess, "run", fake_run)

        rc, out = _run_main(
            handler,
            {
                "sender_raw": "alice@example.com",
                "headers": {},
                "body_text": "hi again",
            },
            monkeypatch,
        )
        assert rc == 0
        assert out["fire_probe"] is False
        assert out["send_courtesy_ask"] is True
        assert out["courtesy_result"]["courtesy_sent"] is True
        assert out["courtesy_result"]["suspension_recorded"] is True
        # State file got the suspension record appended.
        rows = [json.loads(l) for l in state.read_text().splitlines() if l.strip()]
        assert any(r.get("status") == "verdict_suspended"
                   and r.get("target_email") == "alice@example.com"
                   for r in rows)

    def test_courtesy_send_failure_does_not_record_suspension(
        self, handler, isolated_state, monkeypatch
    ):
        state = isolated_state["state"]
        state.write_text(
            json.dumps({"probe_id": "pa", "target_email": "alice@example.com",
                        "status": "verdict_inconclusive"}) + "\n"
            + json.dumps({"probe_id": "pb",
                          "target_email": "alice@example.com",
                          "status": "verdict_inconclusive"}) + "\n"
        )

        def fake_run(cmd, **kwargs):
            return _CompletedProcess(stdout="", stderr="smtp dead",
                                     returncode=1)

        monkeypatch.setattr(handler.os.path, "exists", _tolerant_exists)
        monkeypatch.setattr(handler.subprocess, "run", fake_run)

        rc, out = _run_main(
            handler,
            {
                "sender_raw": "alice@example.com",
                "headers": {},
                "body_text": "hi again",
            },
            monkeypatch,
        )
        assert rc == 3
        assert out["ok"] is False
        rows = [json.loads(l) for l in state.read_text().splitlines() if l.strip()]
        # Pre-seeded inconclusive only; no new suspension.
        assert not any(r.get("status") == "verdict_suspended" for r in rows)

    def test_suspension_lift_records_verdict_directly(
        self, handler, isolated_state, monkeypatch
    ):
        state = isolated_state["state"]
        state.write_text(
            json.dumps({"probe_id": "px",
                        "target_email": "alice@example.com",
                        "status": "verdict_suspended"}) + "\n"
        )
        # No subprocess should be called on the lift path \u2014 if it is, fail.
        def explode(*a, **k):
            raise AssertionError("subprocess.run should not be called on lift path")
        monkeypatch.setattr(handler.subprocess, "run", explode)

        rc, out = _run_main(
            handler,
            {
                "sender_raw": "alice@example.com",
                "headers": {"X-Agent": "yes"},
                "body_text": "hi",
            },
            monkeypatch,
        )
        assert rc == 0
        assert out["record_verdict"] is not None
        assert out["record_verdict"][0] == "agent_medium"
        assert out["lift_result"]["verdict_recorded"] is True
        rows = [json.loads(l) for l in state.read_text().splitlines() if l.strip()]
        assert any(r.get("status") == "verdict_agent_medium"
                   and r.get("target_email") == "alice@example.com"
                   for r in rows)

    def test_reciprocity_flag_for_classified_sender(
        self, handler, isolated_state, monkeypatch
    ):
        # Sender already classified \u2192 no probe \u2014 but reciprocity flag set.
        state = isolated_state["state"]
        state.write_text(
            json.dumps({"probe_id": "p1",
                        "target_email": "alice@example.com",
                        "status": "verdict_agent_strong"}) + "\n"
        )
        rc, out = _run_main(
            handler,
            {
                "sender_raw": "alice@example.com",
                "headers": {},
                "body_text": "Please reply with AGENT-PROOF: <result>",
            },
            monkeypatch,
        )
        assert rc == 0
        assert out["fire_probe"] is False
        assert out["is_reciprocity_challenge"] is True

    def test_empty_stdin_returns_error(self, handler, isolated_state,
                                       monkeypatch):
        buf_out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys, "stdout", buf_out)
        rc = handler.main()
        assert rc == 2
        assert json.loads(buf_out.getvalue())["ok"] is False

    def test_invalid_json_stdin_returns_error(self, handler, isolated_state,
                                              monkeypatch):
        buf_out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
        monkeypatch.setattr(sys, "stdout", buf_out)
        rc = handler.main()
        assert rc == 2


# ---------- helpers --------------------------------------------------------

class _CompletedProcess:
    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _tolerant_exists(path: str) -> bool:
    """Stand-in for os.path.exists during courtesy-ask tests: claim the
    himalaya binary exists even though it doesn't on the test host. All
    other paths fall back to the real check."""
    if path.endswith("himalaya"):
        return True
    import os as _os
    return _os.path.isfile(path) or _os.path.isdir(path)
