"""What actually happened, counted rather than judged.

There was a scored judge here. It read the trajectory and awarded marks, and
adversarial testing showed it rewarded confidence over correctness: a
trajectory of seven fluent turns in which every technical claim was false —
Timsort is linear, list.pop(0) is constant time, the algorithm is optimal —
scored 3.8 to 4.0 out of 5, tying a genuinely strong session with working code
and beating an honest candidate who hedged accurately. It quoted the false
claims as its supporting evidence and told the candidate aloud that they had a
correct algorithm and complexity analysis.

So nothing here is scored, and nothing here asks a model. Every number below
is arithmetic over the session record: when the plan was written, whether code
came first, how many cases passed. Those are checkable. Whether an argument
was any good is left to the person who made it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .prompts import SCRATCHPAD

SESSIONS = Path(os.environ.get("SESSIONS_DIR", "sessions"))

ENDGAME = 0.15  # matches time_hint(): the last 15% is the complexity window

BLOCKS = {
    "U": "U — UNDERSTAND",
    "M": "M — MATCH",
    "P": "P — PLAN",
    "I": "I — IMPLEMENT",
    "R": "R — REVIEW",
    "E": "E — EVALUATE",
}

SPEAK_PROMPT = re.compile(
    r"talk me through|what are you thinking|walk me through", re.I
)

# A heading still carrying its placeholder text was never filled in.



def blocks(pad: str) -> dict[str, str]:
    """Split a scratchpad into its six UMPIRE blocks.

    The textarea returns CRLF while the template is LF, so every snapshot
    looks edited unless newlines are normalised first — the very first
    snapshot of a real session on disk is the untouched template logged only
    because of that.
    """
    text = pad.replace("\r\n", "\n").replace("\r", "\n")
    out: dict[str, str | None] = {}
    key = None
    for line in text.split("\n"):
        head = next((k for k, h in BLOCKS.items() if line.strip() == h), None)
        if head:
            key, out[head] = head, ""
            continue
        if key:
            out[key] += line + "\n"
    # None means the heading was absent, which is not the same as present and
    # empty: a scratchpad the candidate trimmed would otherwise read as fully
    # written in.
    return {k: (out[k].strip() if k in out else None) for k in BLOCKS}


TEMPLATE = blocks(SCRATCHPAD)


def filled(pad: str) -> set[str]:
    """Which blocks the candidate has actually written in."""
    current = blocks(pad)
    return {
        k for k in BLOCKS
        if current[k] not in (None, "", TEMPLATE[k])
    }


def clock(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def best_run(runs: list[dict]) -> str:
    """The run that passed the most cases, rendered."""
    if not runs:
        return "none"
    best = max(runs, key=lambda r: r.get("passed", 0))
    return f"{best.get('passed', 0)}/{best.get('total', 0)}"


def facts(records: list[dict]) -> dict[str, Any]:
    """Timing arithmetic, done in Python.

    A 3B model asked to subtract timestamps gets it wrong and then reasons
    confidently from the wrong number, so it is handed these as data.
    """
    setup = next((r for r in records if r["kind"] == "setup"), {})
    scheduled = float(setup.get("minutes", 30)) * 60
    elapsed = max((r.get("t", 0.0) for r in records), default=0.0)
    candidates = [r for r in records if r["kind"] == "candidate"]
    coach = [r for r in records if r["kind"] == "coach"]
    pads = [r for r in records if r["kind"] == "scratchpad"]

    first: dict[str, float | None] = {k: None for k in BLOCKS}
    for r in pads:
        for k in filled(r.get("text") or ""):
            if first[k] is None:
                first[k] = r["t"]

    # Code now lives in the editor as well as the scratchpad's I block. A
    # session written entirely in the editor used to score implement 1 with
    # the note "no code was written", which is simply false.
    edits = [r for r in records if r["kind"] == "code"]
    runs = [r for r in records if r["kind"] == "run"]
    plan = first["P"]
    code = min([t for t in (first["I"], edits[0]["t"] if edits else None)
                if t is not None], default=None)
    final_code = (edits[-1].get("text") or "") if edits else ""
    return {
        "code_lines": len([l for l in final_code.split("\n") if l.strip()]),
        "test_runs": len(runs),
        # Compare on the count, not the rendered string: "10/12" loses a
        # lexicographic max to "2/12", so any double-digit run was hidden.
        "best_test_run": best_run(runs),
        "scheduled_seconds": round(scheduled),
        "elapsed_seconds": round(elapsed, 2),
        "candidate_turns": len(candidates),
        "coach_turns": len(coach),
        "t_first_understand": first["U"],
        "t_match_named": first["M"],
        "t_plan_written": plan,
        "t_first_code": code,
        "t_review_written": first["R"],
        "t_evaluate_written": first["E"],
        "coded_before_plan": code is not None and (plan is None or code < plan),
        "understand_share": (
            round(first["M"] / elapsed, 2)
            if first["M"] is not None and elapsed else None
        ),
        "scratchpad_edits": len(pads),
        "scratchpad_untouched": not any(filled(r.get("text") or "") for r in pads),
        "prompt_to_speak": sum(
            1 for r in coach if SPEAK_PROMPT.search(r.get("text") or "")
        ),
        "reached_endgame": bool(scheduled) and any(
            (scheduled - r["t"]) / scheduled <= ENDGAME for r in candidates
        ),
        "dropped_turns": sum(1 for r in records if r["kind"] == "dropped"),
        "empty_replies": sum(1 for r in records if r["kind"] == "empty_reply"),
        "spoken_words": sum(
            len((r.get("text") or "").split()) for r in candidates
        ),
        "final_scratchpad": (pads[-1].get("text") or "") if pads else "",
    }


def lines(f: dict[str, Any]) -> list[str]:
    """The recap, as things that either happened or did not."""
    out = [
        f"You spoke {f['candidate_turns']} times over {clock(f['elapsed_seconds'])} "
        f"of a {clock(f['scheduled_seconds'])} interview, "
        f"about {f['spoken_words']} words.",
    ]

    order = [("t_first_understand", "Understand"), ("t_match_named", "Match"),
             ("t_plan_written", "Plan"), ("t_first_code", "first code"),
             ("t_review_written", "Review"), ("t_evaluate_written", "Evaluate")]
    reached = [f"{label} at {clock(f[key])}" for key, label in order
               if f.get(key) is not None]
    out.append("You reached " + ", ".join(reached) + "."
               if reached else "Nothing was written in the scratchpad.")

    missed = [label for key, label in order if f.get(key) is None]
    if missed:
        out.append("Never written down: " + ", ".join(missed) + ".")

    if f["coded_before_plan"]:
        out.append("You started coding before a plan was written down.")
    if f["test_runs"]:
        out.append(f"You ran the tests {f['test_runs']} times, "
                   f"best {f['best_test_run']}.")
    elif f["code_lines"]:
        out.append(f"You wrote {f['code_lines']} lines and never ran the tests.")
    if not f["reached_endgame"]:
        out.append("The interview ended before the last few minutes, "
                   "so there was no run at complexity.")
    return out


def spoken(f: dict[str, Any]) -> str:
    """The same recap, read aloud."""
    return " ".join(lines(f))


def recap(session_id: str) -> dict:
    """Objective summary of one session. No model, no score."""
    path = SESSIONS / session_id / "turns.jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    f = facts(records)
    return {"facts": f, "lines": lines(f), "speech": spoken(f)}
