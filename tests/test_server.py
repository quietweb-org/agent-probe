"""HTTP-level tests for the v3 test-room server (server.py).

Exercises the real FastAPI endpoints end-to-end via TestClient, including
the certification + PASS path. Uses a temp DB and a temp murmur.md, and a
freshly generated certifier key.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import murmur_keys as keys
import certify


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Fresh certifier key + temp paths, then (re)import server with them.
    cpriv, cpub = keys.generate_keypair()
    monkeypatch.setenv("PROBE_DB_PATH", str(tmp_path / "probes.db"))
    monkeypatch.setenv("CERTIFIER_PRIVATE_B64", cpriv)
    monkeypatch.setenv("CERTIFIER_EMAIL", "murmur@mur-mur.at")
    monkeypatch.setenv("MURMUR_MD_PATH", str(tmp_path / "murmur.md"))
    monkeypatch.setenv("RESULT_DELIVERY", "log")
    monkeypatch.setenv("AGENT_CHANNEL_BASE_URL", "https://mur-mur.at/agent-channel")
    monkeypatch.setenv("ALLOW_REMOTE_REGISTER", "1")  # TestClient host isn't loopback
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    c._certifier_pub = cpub
    c._murmur_md = tmp_path / "murmur.md"
    return c


def _register(client, stranger="alice@example.com"):
    # TestClient reports loopback client host, so register is allowed.
    r = client.post("/agent-channel/register", json={
        "stranger_email": stranger,
        "requester": "murmur@mur-mur.at",
        "deliver_to": "murmur@mur-mur.at",
    })
    assert r.status_code == 200, r.text
    return r.json()


def _solve_and_answer(client, reg, priv, pub):
    # knock
    pid = reg["probe_id"]
    r = client.get(f"/agent-channel/{pid}", params={"c": reg["code"]})
    assert r.status_code == 200, r.text
    # the server holds the expected answer; in a test we peek via the store
    import server
    expected = server._store.get(pid).expected_answer
    sig = keys.probe_sign(priv, pid, expected)
    return client.post(f"/agent-channel/{pid}", json={
        "public_key": pub, "answer": expected, "signature": sig,
    })


class TestHappyPath:
    def test_healthz(self, client):
        assert client.get("/agent-channel/healthz").json()["ok"] is True

    def test_full_pass_certifies(self, client):
        reg = _register(client)
        priv, pub = keys.generate_keypair()
        r = _solve_and_answer(client, reg, priv, pub)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verdict"] == "pass"
        assert body["certified"] is True
        row = body["certification"]
        assert "alice@example.com" in row
        assert "murmur@mur-mur.at" in row
        assert "passed live-agent verification" in row

    def test_certification_line_is_valid_signature(self, client):
        reg = _register(client, "bob@example.com")
        priv, pub = keys.generate_keypair()
        r = _solve_and_answer(client, reg, priv, pub)
        row = r.json()["certification"]
        # parse the markdown row back into fields and verify the sig
        cells = [c.strip() for c in row.strip("|").split("|")]
        who, referrer, desc, updated, sig = cells
        assert keys.verify_murmur_line(sig, who=who, referrer=referrer,
                                       description=desc, updated=updated)
        # signed by the certifier's key
        assert keys.pubkey_of_sig(sig) == client._certifier_pub

    def test_certification_appended_to_murmur_md(self, client):
        reg = _register(client, "carol@example.com")
        priv, pub = keys.generate_keypair()
        _solve_and_answer(client, reg, priv, pub)
        content = client._murmur_md.read_text()
        assert "carol@example.com" in content


class TestFailPaths:
    def test_wrong_answer_no_cert(self, client):
        reg = _register(client)
        priv, pub = keys.generate_keypair()
        pid = reg["probe_id"]
        client.get(f"/agent-channel/{pid}", params={"c": reg["code"]})
        sig = keys.probe_sign(priv, pid, "nonsense")
        r = client.post(f"/agent-channel/{pid}", json={
            "public_key": pub, "answer": "nonsense", "signature": sig})
        assert r.status_code == 200
        assert r.json()["verdict"] == "fail"
        assert r.json()["reason"] == "wrong answer"

    def test_bad_code_knock_404(self, client):
        reg = _register(client)
        r = client.get(f"/agent-channel/{reg['probe_id']}", params={"c": "wrong"})
        assert r.status_code == 404

    def test_missing_answer_fields_400(self, client):
        reg = _register(client)
        pid = reg["probe_id"]
        client.get(f"/agent-channel/{pid}", params={"c": reg["code"]})
        r = client.post(f"/agent-channel/{pid}", json={"public_key": "x"})
        assert r.status_code == 400

    def test_answer_before_knock_409(self, client):
        reg = _register(client)
        priv, pub = keys.generate_keypair()
        r = client.post(f"/agent-channel/{reg['probe_id']}", json={
            "public_key": pub, "answer": "x", "signature": "y"})
        assert r.status_code == 409
