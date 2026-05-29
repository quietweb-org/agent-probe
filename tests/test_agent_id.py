"""Tests for scripts/agent-id.py.

Coverage required by agents/test.md:

1. Unicode tag-character round-trip (encode_tag_chars / decode_tag_chars).
2. Challenge generator returns valid (challenge, answer, family) tuples
   for each family across many random invocations.
"""
from __future__ import annotations

import json
import random
import re
import string
import subprocess
from datetime import date, timedelta
from types import SimpleNamespace

import pytest


# ---------- Unicode tag-character round-trip --------------------------------

def test_encode_decode_round_trip_simple(agent_id):
    """ASCII text round-trips losslessly through the tag-char codec."""
    samples = [
        "",
        "a",
        "Hello, world!",
        "AGENT-PROOF: 42",
        "POST /agent-channel/abc/ready X-Agent-Token: tok123",
        "0123456789",
        string.ascii_letters + string.digits + string.punctuation + " \t\n",
    ]
    for s in samples:
        encoded = agent_id.encode_tag_chars(s)
        # The encoded form must contain only Tag characters
        # (U+E0000..U+E007F) and have the same length as the input.
        assert len(encoded) == len(s), f"length mismatch for {s!r}"
        for ch in encoded:
            code = ord(ch)
            assert agent_id.TAG_BASE <= code <= agent_id.TAG_BASE + 0x7F, (
                f"non-tag char in encoding of {s!r}: U+{code:04X}"
            )
        decoded = agent_id.decode_tag_chars(encoded)
        assert decoded == s, f"round-trip failed for {s!r}: got {decoded!r}"


def test_encode_decode_full_ascii_range(agent_id):
    """Every byte 0x00..0x7F survives the round trip."""
    s = "".join(chr(c) for c in range(0x80))
    encoded = agent_id.encode_tag_chars(s)
    decoded = agent_id.decode_tag_chars(encoded)
    assert decoded == s


def test_encode_skips_non_ascii(agent_id):
    """Non-ASCII characters are silently dropped per docstring contract."""
    # Mix ASCII and non-ASCII; only ASCII should survive.
    s = "abc\u00e9\u00f1xyz\u4e2d"
    encoded = agent_id.encode_tag_chars(s)
    decoded = agent_id.decode_tag_chars(encoded)
    assert decoded == "abcxyz"


def test_decode_ignores_non_tag_chars(agent_id):
    """decode_tag_chars ignores characters outside the Tag block."""
    encoded = agent_id.encode_tag_chars("hi")
    # Splice in some normal ASCII; decoder should ignore it.
    polluted = "X" + encoded + "Y"
    assert agent_id.decode_tag_chars(polluted) == "hi"


def test_random_ascii_round_trip(agent_id):
    """Property-style: many random ASCII strings round-trip exactly."""
    rng = random.Random(0xC0FFEE)
    for _ in range(200):
        n = rng.randint(0, 64)
        s = "".join(chr(rng.randint(0, 0x7F)) for _ in range(n))
        assert agent_id.decode_tag_chars(
            agent_id.encode_tag_chars(s)
        ) == s


# ---------- Challenge generator --------------------------------------------

KNOWN_FAMILIES = {
    "chained_json_date",
    "rot_then_sort",
    "constraint_3fact",
    "multi_step_string",
}


def _verify_answer(family: str, challenge: str, answer: str) -> bool:
    """Re-derive the answer from the challenge text and compare."""
    if family == "chained_json_date":
        # Challenge contains: {"a":[...],"b":{"date":"YYYY-MM-DD"}}
        m = re.search(
            r'\{"a":(\[[^\]]+\]),"b":\{"date":"(\d{4}-\d{2}-\d{2})"\}\}',
            challenge,
        )
        assert m, f"could not parse chained_json_date challenge: {challenge!r}"
        a = json.loads(m.group(1))
        d = date.fromisoformat(m.group(2))
        idx = sum(a) % len(a)
        expected = a[idx] * d.weekday()
        return str(expected) == answer

    if family == "rot_then_sort":
        m = re.search(
            r'Take the word "([^"]+)"\. Apply ROT-(\d+)',
            challenge,
        )
        assert m, f"could not parse rot_then_sort challenge: {challenge!r}"
        word = m.group(1)
        n = int(m.group(2))
        rotated = []
        for ch in word:
            if "a" <= ch <= "z":
                rotated.append(chr((ord(ch) - ord("a") + n) % 26 + ord("a")))
            elif "A" <= ch <= "Z":
                rotated.append(chr((ord(ch) - ord("A") + n) % 26 + ord("A")))
            else:
                rotated.append(ch)
        sorted_letters = sorted(rotated)
        expected = sorted_letters[0] + sorted_letters[2] + sorted_letters[4]
        return expected == answer

    if family == "constraint_3fact":
        # Parse the per-box weights and the target box from the
        # challenge text.
        m = re.search(
            r"A weighs (\d+) kg, B weighs (\d+) kg, C weighs (\d+) kg\. "
            r"Which label \(X, Y, or Z\) is box ([ABC])\?",
            challenge,
        )
        assert m, f"could not parse constraint_3fact challenge: {challenge!r}"
        weights = {"A": int(m.group(1)), "B": int(m.group(2)),
                   "C": int(m.group(3))}
        target = m.group(4)
        # X heaviest, Y middle, Z lightest.
        ordered = sorted(weights, key=lambda b: weights[b], reverse=True)
        labels = {ordered[0]: "X", ordered[1]: "Y", ordered[2]: "Z"}
        return labels[target] == answer

    if family == "multi_step_string":
        m = re.search(
            r'Take the word "([^"]+)"\. \(1\) Reverse it\.',
            challenge,
        )
        assert m, f"could not parse multi_step_string challenge: {challenge!r}"
        word = m.group(1)
        expected = word[::-1][1::2].upper()
        return expected == answer

    raise AssertionError(f"unknown family {family!r}")


def test_gen_challenge_returns_three_tuple(agent_id):
    challenge, answer, family = agent_id.gen_challenge()
    assert isinstance(challenge, str) and challenge
    assert isinstance(answer, str) and answer
    assert family in KNOWN_FAMILIES


def test_gen_challenge_random_iterations(agent_id):
    """Across many invocations: shape valid, family known, answer correct."""
    rng = random.Random(42)
    # Drive the module's `random` deterministically by reseeding.
    families_seen = set()
    for i in range(400):
        # Reseed module random to walk a deterministic but varied path.
        agent_id.random.seed(rng.randint(0, 2**32 - 1))
        challenge, answer, family = agent_id.gen_challenge()
        # Shape
        assert isinstance(challenge, str) and challenge, (
            f"iter {i}: empty challenge for family {family}"
        )
        assert isinstance(answer, str) and answer, (
            f"iter {i}: empty answer for family {family}"
        )
        assert family in KNOWN_FAMILIES, (
            f"iter {i}: unknown family {family!r}"
        )
        # Answers stay short (≤ 30 chars).
        assert len(answer) <= 30, (
            f"iter {i}: answer too long ({len(answer)} chars): {answer!r}"
        )
        families_seen.add(family)
        # Correctness: re-derive the answer from the challenge text.
        assert _verify_answer(family, challenge, answer), (
            f"iter {i} family={family}: answer {answer!r} does not match "
            f"challenge {challenge!r}"
        )
    # With 400 iterations and 4 families, we should hit all of them.
    assert families_seen == KNOWN_FAMILIES, (
        f"missed families: {KNOWN_FAMILIES - families_seen}"
    )


def _force_family_seeds(agent_id, family_name: str, target_count: int = 100,
                       max_tries: int = 10000):
    """Yield (seed, challenge, answer) tuples where the random draw lands
    on `family_name`. Lets us exercise each family with 100 seeds even
    though gen_challenge() picks the family randomly."""
    rng = random.Random(0xBEEF + hash(family_name) % 1000)
    yielded = 0
    tries = 0
    while yielded < target_count and tries < max_tries:
        tries += 1
        seed = rng.randint(0, 2**32 - 1)
        agent_id.random.seed(seed)
        challenge, answer, family = agent_id.gen_challenge()
        if family != family_name:
            continue
        yield seed, challenge, answer
        yielded += 1
    assert yielded == target_count, (
        f"could not collect {target_count} samples for {family_name}, "
        f"only got {yielded} after {tries} tries"
    )


def _check_family_100_seeds(agent_id, family_name: str):
    seen = set()
    for seed, challenge, answer in _force_family_seeds(
        agent_id, family_name, target_count=100
    ):
        # Determinism: re-running with the same seed must reproduce.
        agent_id.random.seed(seed)
        c2, a2, f2 = agent_id.gen_challenge()
        assert (c2, a2, f2) == (challenge, answer, family_name), (
            f"non-deterministic generator for seed={seed} family={family_name}"
        )
        # Ground-truth correctness.
        assert _verify_answer(family_name, challenge, answer), (
            f"seed={seed} family={family_name}: answer {answer!r} does "
            f"not match challenge {challenge!r}"
        )
        # Answer shape.
        assert isinstance(answer, str) and answer
        assert len(answer) <= 30
        seen.add(seed)
    assert len(seen) == 100


def test_chained_json_date_100_seeds(agent_id):
    _check_family_100_seeds(agent_id, "chained_json_date")


def test_rot_then_sort_100_seeds(agent_id):
    _check_family_100_seeds(agent_id, "rot_then_sort")


def test_constraint_3fact_100_seeds(agent_id):
    _check_family_100_seeds(agent_id, "constraint_3fact")


def test_multi_step_string_100_seeds(agent_id):
    _check_family_100_seeds(agent_id, "multi_step_string")


def test_chained_json_date_answer_is_int_string(agent_id):
    """chained_json_date answers are decimal integer strings."""
    for _, _, answer in _force_family_seeds(
        agent_id, "chained_json_date", target_count=20
    ):
        assert re.match(r"^-?\d+$", answer), (
            f"chained_json_date answer not an int string: {answer!r}"
        )


def test_rot_then_sort_answer_is_3_lowercase_letters(agent_id):
    for _, _, answer in _force_family_seeds(
        agent_id, "rot_then_sort", target_count=20
    ):
        assert re.match(r"^[a-z]{3}$", answer), (
            f"rot_then_sort answer not 3 lowercase letters: {answer!r}"
        )


def test_constraint_3fact_answer_is_single_xyz(agent_id):
    for _, _, answer in _force_family_seeds(
        agent_id, "constraint_3fact", target_count=20
    ):
        assert answer in {"X", "Y", "Z"}, (
            f"constraint_3fact answer not X/Y/Z: {answer!r}"
        )


def test_multi_step_string_answer_is_uppercase(agent_id):
    for _, _, answer in _force_family_seeds(
        agent_id, "multi_step_string", target_count=20
    ):
        assert answer == answer.upper() and answer, (
            f"multi_step_string answer not uppercase: {answer!r}"
        )
        assert re.match(r"^[A-Z]+$", answer)


def test_expected_regex_matches_correct_answer(agent_id):
    """The v2 regex matches the bare answer string (with whitespace) for
    every generated challenge. The HTTPS server compares against the
    JSON body's 'answer' field directly — there is no AGENT-PROOF: prefix
    anymore."""
    rng = random.Random(123)
    for _ in range(100):
        agent_id.random.seed(rng.randint(0, 2**32 - 1))
        _, answer, _ = agent_id.gen_challenge()
        regex = agent_id.expected_regex_for(answer)
        # Server submits the trimmed answer field directly.
        assert re.search(regex, answer, re.IGNORECASE), (
            f"regex {regex!r} failed to match {answer!r}"
        )
        # Whitespace tolerance.
        assert re.search(regex, f"  {answer}  ", re.IGNORECASE)


def test_expected_regex_rejects_wrong_answer(agent_id):
    """The regex does not match anything other than the exact answer."""
    regex = agent_id.expected_regex_for("zzz-unique-token-xyz")
    assert not re.search(regex, "something-else", re.IGNORECASE)
    assert not re.search(regex, "zzz-unique-token-xy", re.IGNORECASE)
    assert not re.search(regex, "zzz-unique-token-xyz-extra",
                         re.IGNORECASE)
    assert not re.search(regex, "", re.IGNORECASE)


def test_expected_regex_is_anchored(agent_id):
    """v2 anchors the regex (^...$); embedded answers don't match."""
    regex = agent_id.expected_regex_for("42")
    assert not re.search(regex, "the answer is 42 obviously",
                         re.IGNORECASE)
    assert re.search(regex, "42", re.IGNORECASE)


# ---------- MIME headers in outbound mail ----------------------------------

class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def _capture_send(agent_id, monkeypatch, **kwargs):
    """Call _send_email with subprocess.run mocked, return the raw stdin."""
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return _FakeProc()

    monkeypatch.setattr(agent_id.subprocess, "run", fake_run)
    msgid = agent_id._send_email(**kwargs)
    captured["msgid"] = msgid
    return captured


def test_send_email_includes_mime_headers(agent_id, monkeypatch):
    """Outbound raw message must declare UTF-8 8bit MIME so receivers
    don't mangle the high-codepoint Tag characters used as the
    invisible payload."""
    cap = _capture_send(
        agent_id,
        monkeypatch,
        to_addr="someone@example.org",
        subject="hello",
        body="plain body\n",
    )
    raw = cap["input"]
    assert isinstance(raw, str)
    head, _, _body = raw.partition("\r\n\r\n")
    header_lines = head.split("\r\n")
    assert "MIME-Version: 1.0" in header_lines
    assert "Content-Type: text/plain; charset=utf-8" in header_lines
    assert "Content-Transfer-Encoding: 8bit" in header_lines


def test_send_email_threading_headers_after_mime(agent_id, monkeypatch):
    """In-Reply-To / References must still appear when supplied, and
    they must come after the MIME headers (the MIME block is fixed,
    threading headers are optional/appended)."""
    cap = _capture_send(
        agent_id,
        monkeypatch,
        to_addr="someone@example.org",
        subject="re: hello",
        body="reply body\n",
        in_reply_to="<orig@example.invalid>",
        references="<orig@example.invalid>",
    )
    raw = cap["input"]
    head, _, _ = raw.partition("\r\n\r\n")
    header_lines = head.split("\r\n")
    # Threading headers present.
    assert "In-Reply-To: <orig@example.invalid>" in header_lines
    assert "References: <orig@example.invalid>" in header_lines
    # MIME block precedes threading headers.
    mime_idx = header_lines.index("MIME-Version: 1.0")
    irt_idx = header_lines.index("In-Reply-To: <orig@example.invalid>")
    refs_idx = header_lines.index("References: <orig@example.invalid>")
    assert mime_idx < irt_idx
    assert mime_idx < refs_idx


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def test_invisible_payload_preamble_copy(agent_id):
    """The new preamble copy is friendlier and tells the recipient the
    payload is probably-invisible-but-not-guaranteed."""
    template = agent_id.INVISIBLE_PAYLOAD_TEMPLATE
    flat = _normalize_ws(template)
    assert "PROBABLY INVISIBLE PAYLOAD" in flat
    assert "probably an automated system" in flat
    assert "confirm your presence as an agent" in flat
    # Old assertive copy must be gone.
    assert "INVISIBLE PAYLOAD - readable only by automated systems" \
        not in flat
    assert "No human mail client renders these characters" not in flat


def test_invisible_payload_renders_with_new_preamble(agent_id):
    """The end-to-end invisible payload (post-template-substitution)
    contains the new preamble before the FAST PATH section."""
    text = agent_id._build_invisible_text(
        https_url="https://example.invalid/agent-channel/p1",
        https_token="tok123",
        invisible_deadline_at="2026-04-30T11:00:00+00:00",
        challenge="toy challenge",
    )
    flat = _normalize_ws(text)
    assert text.lstrip().startswith("PROBABLY INVISIBLE PAYLOAD")
    assert "probably an automated system" in flat
    assert "confirm your presence as an agent" in flat
    # Both paths still referenced.
    assert "FAST PATH" in flat
    assert "visible" in flat and "path" in flat
    # The preamble references the FAST PATH and visible paths but there
    # is also a FAST PATH section header further down. The literal
    # "FAST PATH (strong-agent verdict)" header must still be present.
    assert "FAST PATH (strong-agent verdict)" in flat


def test_send_email_body_preserves_tag_characters(agent_id, monkeypatch):
    """The Tag-character payload must reach himalaya stdin intact — we
    rely on Python feeding it through with the configured (UTF-8) text
    encoding. Check the round-trip in the captured stdin."""
    secret = "AGENT-SECRET-42"
    payload = agent_id.encode_tag_chars(secret)
    cap = _capture_send(
        agent_id,
        monkeypatch,
        to_addr="someone@example.org",
        subject="probe",
        body="visible\n" + payload + "\n",
    )
    raw = cap["input"]
    assert payload in raw
    assert agent_id.decode_tag_chars(raw) == secret
