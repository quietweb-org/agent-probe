"""Shared fixtures for scripts/ tests.

The legacy `agent-id.py` script lives at scripts/agent-id.py (note the
hyphen, which means we have to load it via importlib.util rather than
a normal import). The newer `agent_id_inbox.py` module uses a normal
import — we just put `scripts/` on sys.path here so test files can
write `from agent_id_inbox import ...`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SCRIPTS_DIR / "agent-id.py"

# Make scripts/ importable for normal-name modules (e.g. agent_id_inbox).
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_agent_id_module():
    spec = importlib.util.spec_from_file_location("agent_id", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_id"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def agent_id():
    return _load_agent_id_module()
