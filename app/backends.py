"""Talking to Nemotron 3 Nano Omni on vLLM.

The model hears the candidate's audio directly, so there is no separate
speech-to-text step: tone, hesitation and false starts reach it intact, and a
transcript is produced alongside only because vLLM permits one audio per
prompt and history has to be text by the next turn.
"""

from __future__ import annotations

import base64
import io
import json
import re
import os
from typing import Protocol

import httpx

OMNI = os.environ.get("OMNI_HOST", "http://localhost:8001")
OMNI_MODEL = os.environ.get("OMNI_MODEL", "nemotron-3-nano-omni-nvfp4")


def build(system: str, messages: list[dict], note: str) -> list[dict]:
    """The running brief, then the clean turns, then this turn's private note.

    The note is its own system message and is never stored in history: kept in
    the user turn it read as something the candidate said, and the model echoed
    it straight back.
    """
    built = [{"role": "system", "content": system}, *messages]
    if note:
        built.append({"role": "system", "content": note})
    return built


def unwrap_structured(text: str) -> str:
    """Recover the speech when a turn arrives wrapped as data.

    Under a long context the model sometimes answers with a JSON object, a
    list, or a quoted string instead of prose. The words inside are usually
    fine, so they are unwrapped rather than discarded.
    """
    if not text or text[0] not in '{["':
        return text
    try:
        value = json.loads(text)
    except ValueError:
        # Truncation leaves it unparseable; recover the longest quoted run.
        runs = re.findall(r'"((?:[^"\\]|\\.){20,})"', text)
        return max(runs, key=len).encode().decode("unicode_escape") if runs else text

    while isinstance(value, (dict, list)) and value:
        value = max(value.values(), key=lambda v: len(str(v))) if isinstance(
            value, dict
        ) else max(value, key=lambda v: len(str(v)))
    return value if isinstance(value, str) else text


NOTE_MARKERS = ("time check", "interviewer note", "elapsed", "scratchpad")
LEAKS = ("elapsed_seconds", "remaining_seconds", "expected_phase",
         "private state", "endgame:", "scratchpad:", "code_written",
         "code_lines", "tests_passed", "last_test_run", "first_failing_case")


def leaked(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in LEAKS)


def strip_note(text: str) -> str:
    """Drop a private note the model echoed back, and tidy the wrapping.

    A leading bracket is only removed when it reads like the note and when
    something is left afterwards: stripping a reply down to nothing is worse
    than leaving a stray bracket in it.
    """
    text = unwrap_structured(text.strip()).strip()

    while text.startswith("["):
        end = text.find("]")
        if end == -1:
            break
        inside = text[1:end].lower()
        rest = text[end + 1 :].strip()
        if not rest or not any(m in inside for m in NOTE_MARKERS):
            break
        text = rest

    if text.count('"') == 1:
        text = text.replace('"', "")
    text = text.strip()
    if len(text) > 1 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text.strip()


RETRY = (
    "Your previous reply repeated the private state instead of speaking. That"
    " block is context for you alone. Reply now with only the words you would"
    " say aloud to the candidate: no field names, no brackets, no braces."
)


def whole_sentences(text: str) -> str:
    """Drop a trailing fragment. Truncation is invisible in text and obvious
    the moment a voice reads it out and stops mid-word."""
    if not text or text[-1] in ".!?":
        return text
    cut = max(text.rfind(c) for c in ".!?")
    return text[: cut + 1] if cut > 0 else text


class Backend(Protocol):
    async def reply(
        self, system: str, messages: list[dict], note: str = ""
    ) -> str: ...


class NemotronOmni:
    """Nemotron 3 Nano Omni on vLLM — hears the audio directly."""

    async def transcribe_audio(self, blob: bytes) -> str:
        """Transcribe a turn so history can stay text.

        vLLM allows one audio per prompt, so past turns cannot stay as audio.
        Running this alongside the reply keeps tone in the live turn, gives the
        page something to show, and keeps the transcript for fine-tuning.
        """
        payload = {
            "model": OMNI_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Transcribe this speech verbatim. Output the "
                            "words only, with no preamble and no commentary.",
                        },
                        self.audio_part(blob, "webm"),
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": 400,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient(timeout=180) as client:
            r = check(await client.post(f"{OMNI}/v1/chat/completions", json=payload))
            content = r.json()["choices"][0]["message"].get("content") or ""
            return unwrap_structured(content.strip())

    @staticmethod
    def image_part(blob: bytes, mime: str = "image/png") -> dict:
        data = base64.b64encode(blob).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}

    @staticmethod
    def audio_part(blob: bytes, fmt: str = "wav") -> dict:
        return {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(blob).decode(), "format": fmt},
        }

    async def reply(
        self, system: str, messages: list[dict], note: str = ""
    ) -> str:
        payload = {
            "model": OMNI_MODEL,
            "messages": build(system, messages, note),
            "temperature": 0.7,
            "max_tokens": 200,
            # The recipe serves with --reasoning-parser nemotron_v3, so thinking
            # lands in reasoning_content and never pollutes the spoken reply.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient(timeout=180) as client:
            r = check(await client.post(f"{OMNI}/v1/chat/completions", json=payload))
            content = r.json()["choices"][0]["message"].get("content") or ""
            text = whole_sentences(strip_note(content.strip()))
            if text and not leaked(text):
                return text
            # The model sometimes answers with nothing but a copy of the
            # private note, which strips away to nothing. Ask again, plainly.
            payload["messages"] = build(system, messages, f"{note}\n{RETRY}")
            r = check(await client.post(f"{OMNI}/v1/chat/completions", json=payload))
            content = r.json()["choices"][0]["message"].get("content") or ""
            return whole_sentences(strip_note(content.strip()))


OCR_PROMPT = """Read this screenshot of a coding problem and write out its full
text as plain text.

Keep the title, the description, the constraints, and every example with its
input, output and explanation. Preserve the line breaks and indentation of code
and examples so they stay readable.

Transcribe only — do not solve it, do not summarise it, do not comment on it,
and do not add a preamble. Output the problem text and nothing else."""


TIDY_PROMPT = """You are given text pasted from a coding-problem website. Return
the problem statement alone.

A problem statement is exactly these parts, in this order:
  the title, the description, every Example with its Input, Output and
  Explanation, any Note or Follow-up, and the Constraints.

Start your output at the title. End it at the last constraint (or the
follow-up, if one comes after). Everything outside that span is page furniture
and must not appear.

DELETE, always — these are never part of a problem:
  site navigation, "Premium", "Solved", the difficulty word on its own line,
  "Topics:" / "Related Topics", "Companies:", "Hint 1" / "Hint 2" and any hint
  toggles, "Accepted"/"Submissions"/"Acceptance Rate" numbers, "Seen this
  question in a real interview before", "Similar Questions", "Discussion",
  "Editorial", "Solutions", vote and comment counts, copyright lines, cookie
  banners, and code-editor scaffolding.

KEEP, exactly as written — copy word for word:
  every sentence of the description, every example including its numbers, and
  every constraint line. Do not paraphrase, shorten, reorder, round a number,
  reformat an inequality, or fix anything you think is wrong. Someone will be
  judged against these, so a constraint that changes is worse than one that
  stays messy. Preserve line breaks and indentation.

Output the problem text and nothing else: no preamble, no commentary, no
solution, no explanation of what you removed."""


def check(response: httpx.Response) -> httpx.Response:
    """Raise with the server's own message: a bare status hides the reason."""
    if response.is_error:
        raise RuntimeError(
            f"{response.request.url} returned {response.status_code}: "
            f"{response.text[:600]}"
        )
    return response


async def read_problem(blob: bytes, mime: str = "image/png") -> str:
    """Transcribe a screenshot of a problem into clean text."""
    payload = {
        "model": OMNI_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    NemotronOmni.image_part(blob, mime),
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient(timeout=300) as client:
        r = check(await client.post(f"{OMNI}/v1/chat/completions", json=payload))
        content = r.json()["choices"][0]["message"].get("content") or ""
        return unwrap_structured(content.strip())


async def tidy_problem(text: str) -> str:
    """Strip the website furniture off a pasted problem.

    Conservative on purpose: the model is told to copy verbatim and to keep
    anything it is unsure about, because a silently dropped constraint would
    be scored against the candidate later.
    """
    payload = {
        "model": OMNI_MODEL,
        "messages": [
            {"role": "system", "content": TIDY_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": 2000,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient(timeout=300) as client:
        r = check(await client.post(f"{OMNI}/v1/chat/completions", json=payload))
        content = r.json()["choices"][0]["message"].get("content") or ""
        return unwrap_structured(content.strip()).strip()


def make_backend(name: str) -> Backend:
    if name == "omni":
        return NemotronOmni()
    raise ValueError(f"unknown backend {name!r}; only 'omni' exists")
