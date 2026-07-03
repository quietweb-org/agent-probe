#!/usr/bin/env python3
"""
challenges.py — fresh-puzzle generators for the agent-probe test.

Each call to gen_challenge() returns (challenge_text, expected_answer,
family). The puzzles chain 2-4 data-dependent deterministic steps, so a
regex or one-liner script cannot shortcut them — solving requires actually
understanding and executing a novel instruction (the intelligence bar). A
frontier LLM solves any of them in seconds.

Extracted verbatim from the v2 agent-id.py so the test room and the legacy
CLI share one canonical generator.
"""
from __future__ import annotations

import json
import random
import re

_CHALLENGE_WORDS = [
    "echolalia", "thunderclap", "marigold", "trapezoid",
    "limerick", "filament", "cardinal", "obscura",
    "jukebox", "vortices", "plywood", "bramble",
    "crayon", "wisdom", "pyrite", "glamour",
]


def gen_challenge() -> tuple[str, str, str]:
    """Generate a fresh challenge.

    Returns (challenge_text, expected_answer, family).
    The expected_answer is a short string that the agent must POST in
    its answer body; the server matches with expected_regex (anchored).

    Each family chains 2–4 deterministic operations with data
    dependencies between the steps, so a regex / one-liner script
    cannot reduce the challenge to a textual lookup. A frontier LLM
    can still solve any of these in seconds.
    """
    family = random.choice([
        "chained_json_date",
        "rot_then_sort",
        "constraint_3fact",
        "multi_step_string",
    ])

    if family == "chained_json_date":
        return _gen_chained_json_date()
    if family == "rot_then_sort":
        return _gen_rot_then_sort()
    if family == "constraint_3fact":
        return _gen_constraint_3fact()
    if family == "multi_step_string":
        return _gen_multi_step_string()
    raise AssertionError(f"unreachable family {family!r}")


# --- Family A: chained_json_date ------------------------------------------

def _gen_chained_json_date() -> tuple[str, str, str]:
    """Parse JSON, pick an array element by a derived index, multiply by
    the weekday number of a date. Three deterministic steps with
    dependencies (sum -> index -> lookup -> multiply by weekday).
    """
    from datetime import date as _date
    a = [random.randint(2, 9) for _ in range(5)]
    base_year = random.randint(2020, 2030)
    base_month = random.randint(1, 12)
    base_day = random.randint(1, 28)
    d = _date(base_year, base_month, base_day)
    # weekday(): Monday=0 ... Sunday=6.
    weekday = d.weekday()
    idx = sum(a) % len(a)
    answer_int = a[idx] * weekday
    challenge = (
        f'Given the JSON {{"a":{json.dumps(a)},'
        f'"b":{{"date":"{d.isoformat()}"}}}}, compute '
        "a[(sum(a) mod len(a))] multiplied by the weekday number of "
        "b.date where Monday=0, Tuesday=1, ..., Sunday=6. Return the "
        "single integer result."
    )
    return challenge, str(answer_int), "chained_json_date"


# --- Family B: rot_then_sort ----------------------------------------------

def _gen_rot_then_sort() -> tuple[str, str, str]:
    """ROT-N a word, sort the resulting letters, take chars at indices
    [0,2,4]. Three chained steps; the ROT shift is the dependency.
    """
    # Choose a word with at least 6 distinct letters so the index-pick
    # is unambiguous after sorting.
    word = random.choice([w for w in _CHALLENGE_WORDS if len(w) >= 6])
    n = random.randint(1, 12)
    rotated = _rot_n(word, n)
    sorted_letters = sorted(rotated)
    picked = sorted_letters[0] + sorted_letters[2] + sorted_letters[4]
    challenge = (
        f'Take the word "{word}". Apply ROT-{n} to each letter '
        f"(shift each lowercase ASCII letter forward by {n} positions, "
        "wrapping z to a). Sort the resulting letters in ascending "
        "alphabetical order. Concatenate the characters at indices "
        "0, 2, and 4 (0-indexed) of that sorted list and return the "
        "resulting 3-character string."
    )
    return challenge, picked, "rot_then_sort"


def _rot_n(text: str, n: int) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - ord("a") + n) % 26 + ord("a")))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - ord("A") + n) % 26 + ord("A")))
        else:
            out.append(ch)
    return "".join(out)


# --- Family C: constraint_3fact -------------------------------------------

def _gen_constraint_3fact() -> tuple[str, str, str]:
    """Tiny logical-inference puzzle: three boxes with a strict ordering
    by weight, three labels with given weights, find which label is the
    target box.

    The challenge gives a partial ordering (X heavier than Y, Z lighter
    than Y => X > Y > Z) plus the weights of A, B, C in some order.
    Sorting A/B/C by weight reveals which is X (heaviest), Y, Z.
    Then the question asks which label corresponds to a specific box.
    """
    # Three weights, distinct.
    weights = random.sample(range(2, 30), 3)
    box_names = ["A", "B", "C"]
    # Map box name -> weight.
    box_to_weight = dict(zip(box_names, weights))
    # X is heaviest, Y is middle, Z is lightest.
    sorted_by_weight_desc = sorted(box_names, key=lambda b: box_to_weight[b],
                                    reverse=True)
    label_to_box = {
        "X": sorted_by_weight_desc[0],
        "Y": sorted_by_weight_desc[1],
        "Z": sorted_by_weight_desc[2],
    }
    # Build the inverse for the question.
    box_to_label = {b: l for l, b in label_to_box.items()}
    target_box = random.choice(box_names)
    answer = box_to_label[target_box]
    challenge = (
        "Three boxes are labelled X, Y, and Z. X is heavier than Y, "
        "and Z is lighter than Y. We have three physical boxes A, B, "
        f"and C with weights: A weighs {box_to_weight['A']} kg, "
        f"B weighs {box_to_weight['B']} kg, C weighs "
        f"{box_to_weight['C']} kg. Which label (X, Y, or Z) is box "
        f"{target_box}? Return only the single letter."
    )
    return challenge, answer, "constraint_3fact"


# --- Family D: multi_step_string ------------------------------------------

def _gen_multi_step_string() -> tuple[str, str, str]:
    """Three chained string transforms: reverse a word, take chars at
    odd indices, then uppercase. The final answer depends on all
    three steps in sequence.
    """
    word = random.choice([w for w in _CHALLENGE_WORDS if len(w) >= 6])
    reversed_word = word[::-1]
    odd_chars = reversed_word[1::2]
    answer = odd_chars.upper()
    challenge = (
        f'Take the word "{word}". (1) Reverse it. (2) From the reversed '
        "string, take the characters at odd indices (1, 3, 5, ...; "
        "0-indexed). (3) Uppercase the resulting string. Return the "
        "final string."
    )
    return challenge, answer, "multi_step_string"


def expected_regex_for(answer: str) -> str:
    """Regex pattern (as a string) that matches a correct answer body.

    v2: server compares against the raw `answer` field in the JSON body,
    anchored with optional whitespace. No more AGENT-PROOF: prefix
    (the email-reply path is gone).
    """
    return r"^\s*" + re.escape(answer) + r"\s*$"


