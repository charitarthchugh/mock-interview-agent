# Voice Technical Interview Prep Agent

A mock technical interview that runs entirely on the DGX Spark. You speak, it
coaches you through the **UMPIRE** framework and manages your clock.

Nothing leaves the box.

## Run

Start the model, then the app:

```bash
sparkrun run @charitarth/nemotron-3-nano-omni-30b-a3b-reasoning-nvfp4-vllm-omni \
  --solo --port 8001            # ~4 min to first token on a cold start

uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

Then open **https://\<machine\>.\<tailnet\>.ts.net** from any device on the
tailnet — phone, laptop, tablet. The mic is whatever device you're holding,
which is how a box with no capture hardware runs a voice agent.

> The HTTPS matters. `getUserMedia` only works in a secure context, so a plain
> `http://192.168.x.x:8000` will silently refuse microphone access.

## Two pages

`/` is the start page: problem statement, screenshot import, time limit.
Submitting sets the interview up and hands off to `/interview?s=<id>`, which
is where the clock starts — so the setup form never eats into your thirty
minutes.

Refreshing mid-interview resumes rather than restarting. The server owns the
clock and the conversation, so the page picks both back up.

| Endpoint | |
|---|---|
| `POST /session` | set up, start test generation, no clock |
| `GET /session/{id}` | problem, scratchpad, code, transcript, clock |
| `POST /session/{id}/begin` | start the clock, get the opening turn |
| `POST /turn` | audio or text, plus the scratchpad and the code |
| `POST /scratchpad`, `POST /session/{id}/code` | autosave |
| `GET /session/{id}/tests` | the generated suite, or why there isn't one |
| `POST /session/{id}/run` | run the code against the suite |
| `POST /session/{id}/end` | stop, score, speak the feedback |
| `POST /read-problem` | OCR a screenshot |

## The screen

```
┌──────────────────────────────────────────────────────┐
│  24:13   Phase Plan          🔊 Voice on   End       │
├───────────────────────────┬──────────────────────────┤
│ PROBLEM                   │ SCRATCHPAD  CODE  TESTS• │
│   322. Coin Change        │  U — UNDERSTAND          │
│   You are given an ...    │    Inputs:               │
│                           │    Outputs:              │
├───────────────────────────┤    Constraints:          │
│ INTERVIEWER               │    Edge cases:           │
│  ▏So you're thinking      │                          │
│    about the boundaries…  │  M — MATCH               │
│              🎤 (spoken)  │    This reminds me of:   │
├───────────────────────────┴──────────────────────────┤
│  [ Hold to talk ]   Alt to speak · Ctrl+Space latch  │
└──────────────────────────────────────────────────────┘
```

Problem and interviewer share the left column. The problem pane shrinks to its
content and caps at 62% so a long statement never squeezes the conversation
out.

The statement itself is **rendered, not dumped**: the title becomes a heading,
the difficulty a chip, each `Example N` a card whose Input/Output/Explanation
line up in a column, and the constraints a monospace list. All of that
structure is recovered at display time by `static/problem.js`, from the plain
text — the stored problem stays verbatim, because the model, the test
generator and the transcript all read it. So the renderer restores nothing it
was not given: a pasted `10^5` that arrived as `105` is shown as `105`. A
statement with no examples and no constraints — a systems question typed in by
hand — falls back to plain paragraphs rather than to broken output.
`node tests/render_problem.mjs` checks both, and that no line is lost in
between.

The right column is one surface with three faces — **Scratchpad**, **Code**,
**Tests** — behind a tab bar that never moves and never hides. Three panes
side by side would have left every one of them too small to work in at 1440px
and unusable on a phone, and a pane that appears only when you need it is a
pane you have to go hunting for with the clock running. Switching costs one
click; a dot on the Tests tab turns on when a run finishes while you are
looking somewhere else, and turns red when something failed.

All three are mounted when the page loads, not on first click: the editor
boots Pyodide immediately, and that 1.4 seconds is free while you are still
reading the problem.

The scratchpad starts prefilled with the UMPIRE headings, and **the
interviewer can see it**. Its current contents ride along with every turn, so
an empty Match section is a cue — "your Match section is still blank, what
does this remind you of?" — and a written plan is something to be talked
through rather than invented twice. It never writes in the pad.

Two hotkeys, because both your hands are in the scratchpad:

| | |
|---|---|
| Hold <kbd>Alt</kbd> | push to talk, release to send |
| <kbd>Ctrl</kbd>+<kbd>Space</kbd> | latch the mic open, press again to send |

Releasing Alt outside the window would leave the mic hot, so window blur stops
recording too.

## Code, and tests it did not see

**Code** is Monaco — the editor behind CoderPad, HackerRank and LeetCode —
running the code through Pyodide in a Web Worker. Practising against a
textarea would be practising against the wrong surface, so it is the real
editor, configured as an interview pad rather than an IDE: Python, no minimap,
and every form of completion, suggestion and parameter hint switched off,
because an editor that finishes the answer defeats the exercise.

Monaco 0.56.0 is vendored under `static/vendor/monaco/vs` and loaded by its
own AMD loader, which needs no build step; `scripts/vendor-monaco.sh`
reproduces the drop from the pinned version. Pyodide is vendored the same way
under `static/vendor/pyodide/`. No CDN, no network, and a hard watchdog that
terminates a runaway rather than asking it to stop. Run is instant and local:
about 1–8 ms round trip, and `while True: pass` comes back as a timeout with
the interpreter still alive.

**Tests** are generated from the problem statement while you are reading it.
`POST /session` fires `generate_tests()` in the background and hands the page
a session id straight away; the suite lands ten to twenty seconds later and
the page polls for it. If generation fails, or the two independent references
disagree about what the answer should be, you get the statement's own examples
and a line saying so — a session never breaks because test generation did.

Running them is server-side (`POST /session/{id}/run`), in the sandbox in
`app/sandbox.py`, because that is where the oracle lives: the comparison knows
about unordered results, and the process is contained. It costs about 25 ms.

Be precise about that containment. `app/sandbox.py` prefers `bwrap`, but
`bwrap` is refused on this box (`loopback: Failed RTM_NEWADDR`) and refused in
the container, so it runs in its `prlimit` fallback — `mode()` reports which.
There, a network namespace blocks the network, `RLIMIT_*` bound CPU, memory,
file size and process count, the parent kills the group on the wall clock, and
an audit hook installed before the code is executed is the only thing standing
between a `open(path, 'w')` and your home directory. That hook once sat twelve
lines too late and module-level code walked straight past it; it is now
installed first, and `os.open`'s flag word is checked as well as `open`'s mode
string. It is a fallback, not a jail.

This is the part worth having. A greedy coin-change solution passes every
example LeetCode publishes and is wrong; against the generated suite it comes
back **26 of 29**, with the failing input printed for you to run by hand:

```
FAIL · hidden coinChange(coins=[11,3,3,11,6,5,11,17,10,14,7,7,13,4,13,1,16,15,18], amount=9)
expected 2  got 3
```

The interviewer sees your code and the result of your last run the same way it
sees the scratchpad — as private machine-fields it must paraphrase, never
read out. A failing case is the counterexample from rung three of the hint
ladder, handed to it for free.

## Ending, and the recap

The clock ending is not a suggestion. At zero — or when you press **End
interview** — `app/recap.py` reads `sessions/<id>/turns.jsonl` and tells you
what happened: how long you spoke and how much, when each UMPIRE section was
written, whether code came before the plan, how many cases passed. It is
arithmetic over the record. It asks no model, so it is instant, and it is the
same every time you reload.

There was a scored judge here, and it was dropped rather than tuned. Under
adversarial testing it rewarded confidence over correctness: a trajectory of
seven fluent turns in which every technical claim was false — Timsort is
linear, `list.pop(0)` is constant time, the algorithm is optimal — scored
3.8–4.0 out of 5, tied a genuinely strong session with working code, and beat
an honest candidate who hedged accurately. It quoted the false claims back as
its supporting evidence and told the candidate aloud that their algorithm and
complexity analysis were correct. A number that flatters a bluffer is worse
than no number, so now there is no number.

## Screenshots of problems

Paste a screenshot with <kbd>Ctrl</kbd>+<kbd>V</kbd> on the setup screen, drop
one on the drop zone, or pick a file. It goes to the same omni model — the one
already loaded — which transcribes it in about five seconds, examples,
constraints and all. No OCR dependency, because the model already does OCR.

The text lands in the box for you to check before starting, since a
misread constraint would quietly poison the whole interview.

## The backend

```
browser mic ─webm/opus─> FastAPI ─> Nemotron 3 Nano Omni (vLLM) ─> reply
```

One model, and it hands the audio straight to it. It *hears* the
candidate — hesitation, false starts, the tone of someone who has lost the
thread — none of which survives a transcript. Verified: given a spoken clip it
returned both an accurate transcription and "a happy and cheerful tone of
voice."

Each turn makes two calls at once: one answers the audio, the other
transcribes it. vLLM permits one audio per prompt, so history has to be text
by the next turn — and the transcript is worth having anyway, for the screen
and for the training data. Running them together costs nothing measurable; a
full audio turn round-trips in about two seconds.

There was a Whisper-plus-Ollama fallback for a while. It is gone: an omni
model that already hears is not improved by putting a transcript in front of
it, and the second path was two more dependencies for a strictly worse turn.
`faster-whisper` survives only in the `corpus` dependency group, for
transcribing recorded interviews offline — never at interview time.

## The voice

Kokoro (82M) renders the replies on the Spark, on the GPU beside the 30B
model. Nemotron cannot do this itself — its model card lists **Modality (out):
Text**, and its audio side is a Parakeet *encoder*. It has ears, not a mouth.

Warm synthesis is 0.06s for a typical reply, so the voice costs nothing
measurable: a full audio turn round-trips in about 1.4s. Set `TTS=off` to fall
back to the browser's `speechSynthesis`, which needs no GPU but sounds like a
different coach on every device and is not always local.

Because the audio is ours, the coach can be interrupted. Pressing the talk
button cuts it off mid-sentence, which is what you want when it is explaining
something you already understand and your clock is running.

## The interviewer's playbook

`app/prompts.py` holds the whole thing — there is no state machine. Every move
in it is a move an experienced interviewer actually makes across the recorded
sessions the prompt was written from. The ones that mattered most:

**Two rules above all: never propose an approach, and never suggest a
different one.** The next idea has to be theirs. The interviewer asks
questions; the candidate supplies approaches.

**The counterexample is the sharpest tool.** When a candidate committed to
greedy on Coin Change, the interviewer didn't correct them — they asked "what
if I gave you coins one, three and four, and asked for six? Apply your
strategy." The candidate got three, the answer was two, and they found the flaw
themselves.

**A hint ladder, worked top-down.** Mirror what they're doing → point at the
problem statement → counterexample → ask if they want a hint → give a
principle and a question, never a solution → narrow to the data structure →
if lost, climb back to mechanics they already know. "Did I lose you there?"

**Implement is optional.** Said out loud: "implement being the nice to have,
we don't require it — you don't need to start coding." The phase budget is
weighted to Understand, Match and Plan accordingly.

**Time checks reassure.** "I want to call out the time — you're moving along
well. About eighteen minutes left, and we want to leave a bit at the end for
complexity."

**Struggle is normalised, brute force is respected.** "That's totally fine."
"There's always a good place to start, and if this is where you can start, go
for it." "We can always refine it later — follow that thread."

The server owns the clock and injects private state before every turn:

```
=== PRIVATE STATE — never speak, quote or repeat any of this ===
elapsed_seconds: 750
remaining_seconds: 1050
expected_phase: Plan
scratchpad: |
  U — UNDERSTAND
    Inputs: coins array, amount
=== END PRIVATE STATE ===
```

Written as machine fields on purpose. Prose here gets read out verbatim and a
bracketed block gets copied as a template — both were observed. Key-value
lines are not speakable, so they have to be paraphrased. Replies that leak a
field name anyway are retried, and inside the last 15% an `endgame: true` line
turns the state into an instruction: get complexity, then close with one glow
and one grow.

`PHASE_BUDGET` maps elapsed fraction to the expected phase — Understand 20%,
Match 35%, Plan 60%, Implement 85%, Review 93%, Evaluate 100%. The model
decides how hard to push; the numbers are one edit away.

## The corpus

`scripts/extract_audio.py` then `scripts/transcribe.py` turn session
recordings into timestamped transcripts:

```bash
uv run python scripts/extract_audio.py <recordings-dir> corpus/audio
uv run python scripts/transcribe.py corpus/audio corpus/transcripts
```

3.6 hours ran in about 24 minutes on CPU. `corpus/` is gitignored — it is
400MB of wav, and the transcripts name real people.

These are group sessions, so there are no speaker labels: the transcript
cannot tell coach from candidate. Good enough for shaping a prompt by reading,
not good enough for supervised fine-tuning. The wavs are kept, so diarization
is a later pass rather than lost data.

## Transcripts for fine-tuning

Each session writes `sessions/<id>/`:

```
turns.jsonl      one record per turn, with the UMPIRE phase attached
turn-001.webm    the raw candidate audio
```

`turns.jsonl` records the whole session, not just the talking: `setup`,
`start`, `candidate`, `coach`, `dropped`, `scratchpad` snapshots, `tests`
(what the generator produced and why), `code` snapshots from the editor, `run`
(every case with its inputs, expected value and what your code returned) and
`end`. That is the whole reason the logging exists — a turn is
only supervision if you can see what was on the screen when it was taken.

The audio is kept deliberately. For fine-tuning a model that listens, the
waveform is the training signal and the transcript is a lossy summary of it.
Run interviews now — this data only exists for sessions you actually record.

## Configuration

| Variable          | Default                       |
| ----------------- | ----------------------------- |
| `OMNI_HOST`       | `http://localhost:8001`       |
| `OMNI_MODEL`      | `nemotron-3-nano-omni-nvfp4`  |
| `TTS`             | `kokoro` (`off` for browser)  |
| `TTS_VOICE`       | `af_heart`                    |
| `TTS_DEVICE`      | `cuda`                        |
| `TTS_SPEED`       | `1.05`                        |
