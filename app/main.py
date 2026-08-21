"""Voice technical-interview prep agent, served over HTTP for any browser."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio import has_speech
from .backends import (Backend, NemotronOmni, make_backend, read_problem,
                       tidy_problem)
from .recap import recap
from .prompts import SCRATCHPAD, UMPIRE, expected_phase, time_hint
from .sandbox import run_cases
from .testgen import TestSuite, compare, generate_tests
from .tts import Kokoro

BACKEND_NAME = os.environ.get("BACKEND", "omni")
TTS = os.environ.get("TTS", "kokoro")
SESSIONS = Path(os.environ.get("SESSIONS_DIR", "sessions"))
SESSIONS.mkdir(exist_ok=True)

app = FastAPI(title="Interview Prep Agent")
backend: Backend | None = None
voice: Kokoro | None = None


@dataclass
class Session:
    id: str
    problem: str
    minutes: float
    started: float | None = None
    messages: list[dict] = field(default_factory=list)
    turns: int = 0
    scratchpad: str = SCRATCHPAD
    code: str = ""
    last_run: dict | None = None
    suite: TestSuite | None = None
    tests_done: bool = False
    ended: bool = False

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started is None else time.monotonic() - self.started

    def begin(self) -> None:
        if self.started is None:
            self.started = time.monotonic()

    @property
    def total(self) -> float:
        return self.minutes * 60

    @property
    def dir(self) -> Path:
        d = SESSIONS / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log(self, kind: str, **fields) -> None:
        """Append a turn to the session transcript, for later fine-tuning."""
        record = {"t": round(self.elapsed, 2), "kind": kind, **fields}
        with (self.dir / "turns.jsonl").open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    def note_scratchpad(self, text: str | None) -> None:
        """Keep the latest scratchpad, and log it when it changes."""
        text = lf(text)
        if text is None or text == self.scratchpad:
            return
        self.scratchpad = text
        self.log("scratchpad", text=text)

    def note_code(self, text: str | None) -> None:
        """Keep the latest editor contents, and log it when it changes.

        Snapshots rather than diffs: the interesting fine-tuning signal is what
        the file looked like at each turn, not the keystrokes between.
        """
        text = lf(text)
        if text is None or text == self.code:
            return
        self.code = text
        self.log("code", text=text)

    def save_audio(self, blob: bytes) -> str:
        """Keep the raw audio — for a voice model it is the training signal."""
        name = f"turn-{self.turns:03d}.webm"
        (self.dir / name).write_bytes(blob)
        return name


sessions: dict[str, Session] = {}


@app.on_event("startup")
def load_models() -> None:
    global backend, voice
    backend = make_backend(BACKEND_NAME)
    if TTS == "kokoro":
        voice = Kokoro()


def spoken(text: str) -> str | None:
    """Synthesise the reply as a base64 WAV, or None to let the browser speak."""
    if voice is None or not text:
        return None
    return base64.b64encode(voice.wav(text)).decode()


def lf(text: str | None) -> str | None:
    """Normalise newlines on the way in.

    Every text field arrives through a multipart form, and multipart encoding
    normalises newlines to CRLF on the wire. Nothing downstream wants them: the
    carriage returns show up in the fine-tuning corpus, in the problem the
    renderer parses, and in code that is otherwise pure LF. Stripping them once
    at the door beats stripping them at each of the places they would surface.
    """
    if text is None:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def system_prompt(session: Session) -> str:
    return UMPIRE.format(problem=session.problem, minutes=int(session.minutes))


def hint(session: Session) -> str:
    """Private state for this turn: clock, scratchpad, code and last run."""
    run = None
    if session.last_run:
        run = {**session.last_run,
               "ago": session.elapsed - session.last_run.get("at", 0.0)}
    return time_hint(
        session.elapsed, session.total, session.scratchpad, session.code, run
    )


def state(session: Session) -> dict:
    return {
        "elapsed": round(session.elapsed),
        "remaining": round(max(0.0, session.total - session.elapsed)),
        "phase": expected_phase(session.elapsed, session.total),
    }


@app.post("/session")
async def create(problem: str = Form(...), minutes: float = Form(30)):
    """Set the interview up. The clock does not start until /begin."""
    problem = lf(problem)
    session = Session(id=uuid.uuid4().hex[:8], problem=problem, minutes=minutes)
    sessions[session.id] = session
    session.log("setup", problem=problem, minutes=minutes, backend=BACKEND_NAME)
    start_testgen(session)
    return {"session": session.id}


# Held so the loop cannot garbage-collect a task nothing else references.
testgen_tasks: set[asyncio.Task] = set()


def start_testgen(session: Session) -> None:
    """Generate the test suite in the background, off the setup path.

    It takes ten to twenty seconds against the local model, which is roughly
    the time the candidate spends reading the problem. Session creation must
    never wait for it and must never fail because of it.
    """
    task = asyncio.create_task(generate_tests(session.problem))
    testgen_tasks.add(task)

    def done(t: asyncio.Task) -> None:
        testgen_tasks.discard(t)
        session.tests_done = True
        try:
            if t.cancelled():
                return
            suite = t.result()
        except BaseException as exc:                       # never break a session
            session.log("tests", oracle="none", reason=f"{type(exc).__name__}: {exc}")
            return
        session.suite = suite
        session.log("tests", oracle=suite.oracle, reason=suite.reason,
                    seconds=round(suite.seconds, 1), entry=suite.entry,
                    examples=len(suite.examples), hidden=len(suite.hidden),
                    log=list(suite.log))

    task.add_done_callback(done)


def need(session_id: str) -> Session:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    return session


def transcript(session: Session) -> list[dict]:
    """The conversation so far, so a refresh doesn't wipe the screen."""
    out = []
    for i, m in enumerate(session.messages):
        if m["role"] == "assistant":
            out.append({"who": "coach", "text": m["content"]})
        elif i == 0:
            continue  # the synthetic turn that kicks off the greeting
        elif isinstance(m["content"], str):
            out.append({"who": "me", "text": m["content"]})
        else:
            out.append({"who": "me", "text": "🎤 (spoken)"})
    return out


@app.get("/session/{session_id}")
def read_session(session_id: str):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    return {
        "session": session.id,
        "problem": session.problem,
        "scratchpad": session.scratchpad,
        "code": session.code,
        "ended": session.ended,
        "started": session.started is not None,
        "minutes": session.minutes,
        "transcript": transcript(session),
        **state(session),
    }


@app.post("/session/{session_id}/begin")
async def begin(session_id: str):
    """Start the clock and get the interviewer's opening turn."""
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    if session.started is not None:
        raise HTTPException(409, "already begun")

    session.begin()
    session.log("start", minutes=session.minutes)
    session.messages.append(
        {
            "role": "user",
            "content": "The interview starts now. Greet me briefly, ask for my "
            "intro, and start the Understand phase.",
        }
    )
    reply = await backend.reply(
        system_prompt(session), session.messages, time_hint(0, session.total)
    )
    session.messages.append({"role": "assistant", "content": reply})
    session.log("coach", text=reply)
    return {"reply": reply, "audio": spoken(reply), **state(session)}


@app.post("/turn")
async def turn(
    session_id: str = Form(...),
    audio: UploadFile | None = None,
    text: str = Form(None),
    scratchpad: str = Form(None),
    code: str = Form(None),
):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")

    session.note_scratchpad(scratchpad)
    session.note_code(code)
    note = hint(session)
    phase = expected_phase(session.elapsed, session.total)
    transcript = ""
    clip = None

    if text:
        transcript = lf(text).strip()
        if not transcript:
            return {"transcript": "", "reply": "", **state(session)}
        candidate = {"role": "user", "content": transcript}

    elif audio is not None:
        blob = await audio.read()

        # Gate on the waveform before spending a model call. Otherwise the
        # interviewer answers a cough as earnestly as a sentence.
        speaking, why = has_speech(blob)
        if not speaking:
            session.log("dropped", reason=why, phase=phase)
            return {"transcript": "", "reply": "", "dropped": why, **state(session)}

        session.turns += 1
        clip = session.save_audio(blob)

        # The model answers the audio directly, so it hears tone. A second
        # call transcribes the same clip: vLLM permits one audio per
        # prompt, so history has to be text by the next turn.
        spoken_turn = {
            "role": "user",
            # A content list holding only audio is rejected, so it is
            # paired with empty text rather than words never said.
            "content": [
                {"type": "text", "text": ""},
                NemotronOmni.audio_part(blob, fmt="webm"),
            ],
        }
        reply, transcript = await asyncio.gather(
            backend.reply(
                system_prompt(session), [*session.messages, spoken_turn], note
            ),
            backend.transcribe_audio(blob),
        )
        # Rare, but the gate passes noise the model then hears as nothing.
        if not re.search(r"[A-Za-z0-9]", transcript):
            session.log("dropped", reason="no words", audio=clip, phase=phase)
            return {
                "transcript": "",
                "reply": "",
                "dropped": "I didn't catch any words",
                **state(session),
            }

        if not reply:
            # Never answer with silence: the page would look like it had
            # simply ignored the turn.
            session.log("empty_reply", transcript=transcript, phase=phase)
            return {
                "transcript": transcript,
                "reply": "",
                "dropped": "The interviewer didn't answer that one",
                **state(session),
            }

        session.messages.append({"role": "user", "content": transcript})
        session.messages.append({"role": "assistant", "content": reply})
        session.log("candidate", text=transcript, audio=clip, phase=phase)
        session.log("coach", text=reply)
        return {
            "transcript": transcript,
            "reply": reply,
            "audio": spoken(reply),
            **state(session),
        }
    else:
        raise HTTPException(400, "need audio or text")

    # History only takes the turn once it has an answer. A failed call used to
    # strand the candidate's message, so a retry stacked two user turns.
    reply = await backend.reply(
        system_prompt(session), [*session.messages, candidate], note
    )
    if not reply:
        session.log("empty_reply", transcript=transcript, phase=phase)
        return {
            "transcript": transcript,
            "reply": "",
            "dropped": "The interviewer didn't answer that one",
            **state(session),
        }

    session.messages.append(candidate)
    session.messages.append({"role": "assistant", "content": reply})
    session.log("candidate", text=transcript or None, audio=clip, phase=phase)
    session.log("coach", text=reply)
    return {
        "transcript": transcript,
        "reply": reply,
        "audio": spoken(reply),
        **state(session),
    }


@app.post("/read-problem")
async def extract(image: UploadFile):
    """OCR a screenshot of a problem so it can be pasted straight in."""
    blob = await image.read()
    if not blob:
        raise HTTPException(400, "empty image")
    text = await read_problem(blob, image.content_type or "image/png")
    return {"problem": text}


# Phrases that only ever appear in the instructions, never in a problem.
ECHOED = ("coding-problem website", "page furniture", "problem statement alone",
          "Output the problem text and nothing else")

# Markers that must survive if the paste had them: losing an example or the
# constraints is the failure that would quietly poison an interview.
LANDMARKS = ("Example 1", "Constraints", "Follow up", "Input:", "Output:")


def faithful(cleaned: str, raw: str) -> str | None:
    """Why the cleanup should be rejected, or None if it holds.

    The model is asked to copy verbatim and delete, so every line it returns
    should be findable in the paste. Judging it by how much it removed does
    not work — a page dump with a discussion thread legitimately loses three
    quarters of its characters.
    """
    if len(cleaned) < 80:
        return "the cleanup dropped too much"

    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    invented = sum(1 for l in lines if l not in raw)
    if lines and invented / len(lines) > 0.05:
        return "the cleanup rewrote the problem instead of trimming it"

    lost = [m for m in LANDMARKS if m in raw and m not in cleaned]
    if lost:
        return f"the cleanup lost {lost[0]}"
    return None


@app.post("/tidy-problem")
async def tidy(problem: str = Form(...)):
    """Clean website furniture off a pasted problem statement."""
    raw = lf(problem).strip()
    if not raw:
        raise HTTPException(400, "empty problem")

    cleaned = await tidy_problem(raw)

    # Handed a short, already-clean statement the model has nothing to strip
    # and sometimes recites its own instructions back instead.
    if any(marker in cleaned for marker in ECHOED):
        return {"problem": raw, "changed": False,
                "note": "left as pasted — nothing to clean"}

    problem_with = faithful(cleaned, raw)
    if problem_with:
        return {"problem": raw, "changed": False,
                "note": f"left as pasted — {problem_with}"}

    return {"problem": cleaned, "changed": cleaned != raw}


@app.post("/scratchpad")
def save_scratchpad(session_id: str = Form(...), scratchpad: str = Form(...)):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    session.note_scratchpad(scratchpad)
    return {"saved": True}


@app.post("/session/{session_id}/code")
def save_code(session_id: str, code: str = Form(...)):
    """Autosaved editor contents. Snapshots are the fine-tuning signal."""
    session = need(session_id)
    session.note_code(code)
    return {"saved": True}


@app.get("/session/{session_id}/tests")
def read_tests(session_id: str):
    """Poll while generation is still running; the page starts before it ends."""
    session = need(session_id)
    if session.suite is None:
        return {"ready": session.tests_done, "suite": None,
                "reason": "no generated tests"}
    return {"ready": True, "suite": session.suite.as_dict()}


def runnable(suite: TestSuite) -> list[tuple[str, object]]:
    """The cases worth running: the statement's own, plus generated ones only
    when two references agreed on what they should return."""
    cases = [("example", c) for c in suite.examples]
    if suite.oracle == "validated":
        cases += [("hidden", c) for c in suite.hidden]
    return cases


@app.post("/session/{session_id}/run")
async def run_suite(session_id: str, code: str = Form(...)):
    """Run the candidate's code against the generated suite, out of process."""
    session = need(session_id)
    session.note_code(code)
    suite = session.suite
    if suite is None or suite.spec is None or not runnable(suite):
        return {"ok": False, "error": "no generated tests for this problem"}

    picked = runnable(suite)
    # run_cases forks a process and waits on it; awaiting it on the event loop
    # would stall the voice turn that is the whole point of the app.
    # session.code, not the form field: note_code has normalised the newlines.
    r = await asyncio.to_thread(
        run_cases, session.code, suite.entry, [dict(c.inputs) for _, c in picked],
        20.0, 4.0,
    )
    if not r["ok"]:
        session.last_run = {"ok": False, "error": r["error"],
                            "at": session.elapsed}
        session.log("run", ok=False, error=r["error"], passed=0,
                    total=len(picked))
        return {"ok": False, "error": r["error"]}

    cases = []
    for (source, case), got in zip(picked, r["results"]):
        error = got.get("error")
        passed = (not error) and compare(got["got"], case.expected, suite.spec)
        cases.append({
            "source": source, "family": case.family, "inputs": case.inputs,
            "expected": case.expected, "got": got.get("got"),
            "error": error, "passed": passed, "ms": got.get("ms"),
        })
    passed = sum(1 for c in cases if c["passed"])
    session.last_run = {"ok": True, "cases": cases, "at": session.elapsed}
    session.log("run", ok=True, passed=passed, total=len(cases),
                entry=suite.entry, cases=cases)
    return {"ok": True, "passed": passed, "total": len(cases), "cases": cases,
            "mode": r.get("mode"), **state(session)}


@app.post("/session/{session_id}/end")
async def end(session_id: str, scratchpad: str = Form(None),
              code: str = Form(None)):
    """Stop the clock and recap the session. Fired at zero or by the candidate."""
    session = need(session_id)
    session.note_scratchpad(scratchpad)
    session.note_code(code)
    if not session.ended:
        session.ended = True
        session.log("end", elapsed=round(session.elapsed, 2))
    r = recap(session.id)
    return {"recap": r, "speech": r["speech"], "audio": spoken(r["speech"])}


@app.get("/")
def start_page() -> FileResponse:
    return FileResponse("static/start.html")


@app.get("/interview")
def interview_page() -> FileResponse:
    return FileResponse("static/interview.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
