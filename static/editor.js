// In-browser Python for the interview page: a Monaco editor that runs the
// candidate's code in a Pyodide Web Worker, with a hard timeout and real
// termination of runaway code.
//
// EXPORTS
//
//   initEditor({ mount, starterCode, timeoutMs, onChange, onStatus }) -> handle
//       Builds the Code + Output panes inside `mount` (an element) and boots
//       Pyodide immediately, in parallel with whatever else the page is doing.
//         mount        element to fill. Required.
//         starterCode  initial editor contents. Default "".
//         timeoutMs    watchdog before a run is killed. Default 10000.
//         onChange(code)            fires on every edit (also via handle.onChange)
//         onStatus(state, result)   "booting"|"idle"|"running"|"restarting"
//
//   handle.getCode() -> string
//   handle.setCode(text)                 replaces the editor contents
//   handle.onChange(cb) -> unsubscribe   cb(code) on every edit
//   handle.run() -> Promise<Result>      runs the current editor contents
//   handle.runTests(tests) -> Promise<Result[]>
//         `tests` is an array of Python snippets; each runs as
//         code + "\n" + test, sequentially, each in a fresh namespace, so an
//         `assert` that fails comes back as status "error".
//   handle.stop()                        terminate + respawn the worker
//   handle.state -> string               same values as onStatus
//   handle.focus()
//   handle.destroy()                     removes the DOM and kills the worker
//
//   Result = { ok, status, stdout, stderr, value, ms }
//       status is "ok" | "error" | "killed" | "timeout"; ok === (status==="ok").
//       value is the string form of the trailing expression, or null.
//
//   createRunner({ onOutput, onStatus }) -> { run(code), stop(), state }
//       The worker layer on its own, for callers that want no DOM.
//
// The Code pane is Monaco — the same editor behind CoderPad, HackerRank and
// LeetCode — vendored under static/vendor/monaco/ and loaded by its own AMD
// loader, so there is still no build step and still nothing off the network.
// Practising against the surface the real screens use is the whole point, so
// it is configured as an interview pad rather than an IDE: Python, no
// minimap, and every form of completion switched off, because an editor that
// finishes the answer for you defeats the exercise.
//
// Monaco loads asynchronously and the page mounts the editor at boot, so the
// handle is returned before the editor exists. getCode/setCode/focus work
// against a buffer until it does; nothing upstream has to know.
//
// Stopping is terminate() + respawn, never a "stop" message: a worker spinning
// in a C-level loop (sum(range(10**11))) answers no messages and ignores
// Pyodide's interrupt buffer, which is the case that actually needs killing.

const WATCHDOG_MS = 10000;
const MONACO = "/static/vendor/monaco/vs";

// ------------------------------------------------------------------ monaco

// One load per page, shared by every editor on it. loader.js defines an AMD
// `require`; pointing it at our copy is all the configuration there is.
// editor.main then resolves its own stylesheet and its web worker relative to
// that path — same origin, no CDN — and installs a MonacoEnvironment that
// wraps the worker in the documented blob/importScripts shim itself.
let loading = null;

function loadMonaco(doc) {
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const s = doc.createElement("script");
    s.src = `${MONACO}/loader.js`;
    s.onerror = () => reject(new Error(`cannot load ${s.src}`));
    s.onload = () => {
      const amd = globalThis.require;
      amd.config({ paths: { vs: MONACO } });
      amd(["vs/editor/editor.main"], () => resolve(globalThis.monaco), reject);
    };
    doc.head.append(s);
  });
  return loading;
}

// Monaco wants hex, so these are the values of the custom properties in
// app.css rather than references to them. Keep the two in step.
const THEME = {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "", foreground: "e6e8ee" },
    { token: "comment", foreground: "8b93a7", fontStyle: "italic" },
    { token: "keyword", foreground: "5ac8fa" },
    { token: "string", foreground: "4ec9a0" },
    { token: "number", foreground: "4ec9a0" },
    { token: "type", foreground: "ff9f43" },
    { token: "identifier", foreground: "e6e8ee" },
  ],
  colors: {
    "editor.background": "#0f1115",
    "editor.foreground": "#e6e8ee",
    "editorLineNumber.foreground": "#3a4155",
    "editorLineNumber.activeForeground": "#8b93a7",
    "editor.lineHighlightBackground": "#161a24",
    "editor.selectionBackground": "#26344a",
    "editorIndentGuide.background1": "#232734",
    "editorIndentGuide.activeBackground1": "#3a4155",
    "editorCursor.foreground": "#5ac8fa",
    "editorWidget.background": "#161a24",
    "editorWidget.border": "#232734",
    "scrollbarSlider.background": "#23273480",
    // 0.56 still paints the bracket-highlighting classes with the default
    // gold-and-orchid pool even when bracketPairColorization is off, so the
    // pool itself is pinned to the colour the other delimiters already use.
    "editorBracketHighlight.foreground1": "#dcdcdc",
    "editorBracketHighlight.foreground2": "#dcdcdc",
    "editorBracketHighlight.foreground3": "#dcdcdc",
    "editorBracketHighlight.foreground4": "#dcdcdc",
    "editorBracketHighlight.foreground5": "#dcdcdc",
    "editorBracketHighlight.foreground6": "#dcdcdc",
    "editorBracketHighlight.unexpectedBracket.foreground": "#ff5f56",
  },
};

// An interview pad: no minimap, no completion of any kind, no hovers. The
// candidate is being asked to recall the answer, not to accept one.
const OPTIONS = {
  language: "python",
  theme: "interview-dark",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: 14,
  lineHeight: 22,
  padding: { top: 12, bottom: 12 },
  tabSize: 4,
  insertSpaces: true,
  detectIndentation: false,       // pasted 2-space code must not retune the pad
  automaticLayout: true,          // the pane is a flex child that resizes
  minimap: { enabled: false },
  // Gold-and-orchid rainbow brackets are Monaco's default and are louder than
  // anything else in this UI; the indent guides carry the same information.
  bracketPairColorization: { enabled: false },
  guides: { bracketPairs: false, indentation: true },
  scrollBeyondLastLine: false,
  renderLineHighlight: "line",
  quickSuggestions: false,
  suggestOnTriggerCharacters: false,
  wordBasedSuggestions: "off",
  inlineSuggest: { enabled: false },
  parameterHints: { enabled: false },
  acceptSuggestionOnEnter: "off",
  tabCompletion: "off",
  snippetSuggestions: "none",
  hover: { enabled: false },
  codeLens: false,
  lightbulb: { enabled: "off" },
  contextmenu: false,
  folding: false,
  fixedOverflowWidgets: true,
  scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
};

// ------------------------------------------------------------- worker layer

export function createRunner({ onOutput, onStatus, timeoutMs = WATCHDOG_MS } = {}) {
  const url = new URL("./vendor/pyodide/runner.js", import.meta.url);
  let worker = null, state = "booting", runId = 0, pending = null, timer = null;
  let bootMs = null;

  const setState = (s, result) => { state = s; if (onStatus) onStatus(s, result); };

  function spawn() {
    worker = new Worker(url, { type: "module" });
    worker.onmessage = (e) => {
      const m = e.data;
      if (m.type === "ready") { if (bootMs === null) bootMs = m.ms; setState("idle"); return; }
      if (!pending || m.id !== runId) return;                 // superseded run
      if (m.type === "out") {
        pending[m.stream].push(m.text);
        if (onOutput) onOutput(m.stream, m.text);
      } else if (m.type === "done") {
        settle({ status: m.status, value: m.value ?? null, error: m.error || "", ms: m.ms });
      }
    };
  }

  function settle(d) {
    if (!pending) return;
    const p = pending; pending = null;
    clearTimeout(timer);
    const stderr = p.stderr.join("\n") + (d.error ? (p.stderr.length ? "\n" : "") + d.error : "");
    const result = {
      ok: d.status === "ok", status: d.status,
      stdout: p.stdout.join("\n"), stderr, value: d.value ?? null,
      ms: d.ms ?? Math.round(performance.now() - p.t0),
    };
    // A run killed mid-flight still owes the candidate a visible reason, even
    // though the state stays "restarting" until the replacement worker boots.
    if (state !== "restarting") setState("idle", result);
    else if (onStatus) onStatus(state, result);
    p.resolve(result);
  }

  function kill(status) {
    worker.terminate();
    setState("restarting");
    settle({ status, error: status === "timeout" ? `Killed after ${timeoutMs} ms.` : "Stopped." });
    spawn();
  }

  spawn();

  return {
    get state() { return state; },
    get bootMs() { return bootMs; },
    run(code) {
      if (state !== "idle") return Promise.reject(new Error(`busy: ${state}`));
      runId += 1;
      setState("running");
      return new Promise((resolve) => {
        pending = { resolve, stdout: [], stderr: [], t0: performance.now() };
        timer = setTimeout(() => kill("timeout"), timeoutMs);
        worker.postMessage({ cmd: "run", id: runId, code });
      });
    },
    stop() { if (state === "running") kill("killed"); },
    destroy() { clearTimeout(timer); worker.terminate(); worker.onmessage = null; },
  };
}

// ---------------------------------------------------------------------- UI

const CSS = `
.ed-wrap { display:flex; flex-direction:column; min-height:0; height:100%; }
.ed-head { display:flex; gap:10px; align-items:center; padding:6px 16px;
  border-bottom:1px solid var(--line); background:var(--panel); flex:none;
  font-size:11px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
  color:var(--dim); }
.ed-head .ed-spacer { flex:1; }
.ed-head button { background:transparent; border:1px solid var(--line);
  color:var(--dim); padding:4px 12px; border-radius:8px; font-size:12px;
  letter-spacing:0; text-transform:none; font-weight:600; }
.ed-head button.ed-go { color:var(--accent); border-color:var(--accent); }
.ed-head button.ed-halt { color:var(--bad); border-color:var(--bad); }
.ed-head button:disabled { opacity:.4; cursor:default; }
/* Monaco fills this and measures itself against it, so it needs a real size
   and no padding of its own: the editor draws its own gutter and padding. */
.ed-code { flex:5; min-height:0; width:100%; overflow:hidden;
  background:var(--bg); }
.ed-outhead { border-top:1px solid var(--line); }
.ed-outhead .ed-note { font-weight:400; letter-spacing:0; text-transform:none;
  font-size:11px; color:var(--dim); }
.ed-out { flex:2; overflow:auto; padding:10px 16px; margin:0; min-height:0;
  white-space:pre-wrap; color:var(--fg);
  font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
.ed-out:empty { flex:0; padding:0; }
.ed-out .ed-err { color:var(--bad); }
.ed-out .ed-killed { color:var(--warn); }
.ed-out .ed-val { color:var(--dim); }
`;

function styleOnce(doc) {
  if (doc.getElementById("ed-style")) return;
  const s = doc.createElement("style");
  s.id = "ed-style"; s.textContent = CSS;
  doc.head.append(s);
}

export function initEditor({ mount, starterCode = "", timeoutMs = WATCHDOG_MS, onChange, onStatus } = {}) {
  const doc = mount.ownerDocument;
  styleOnce(doc);
  mount.innerHTML = `
    <div class="ed-wrap">
      <div class="ed-head">Code<span class="ed-spacer"></span>
        <button class="ed-go" disabled>Loading Python…</button>
        <button class="ed-halt" disabled>Stop</button>
      </div>
      <div class="ed-code"></div>
      <div class="ed-head ed-outhead">Output <span class="ed-note"></span></div>
      <pre class="ed-out"></pre>
    </div>`;
  const q = (s) => mount.querySelector(s);
  const host = q(".ed-code"), out = q(".ed-out"), go = q(".ed-go"),
        halt = q(".ed-halt"), note = q(".ed-note");

  const listeners = [];
  if (onChange) listeners.push(onChange);
  const fire = (code) => listeners.forEach((cb) => cb(code));

  // Everything before Monaco arrives goes through `buffer`; after it arrives,
  // the model is the only copy. The page mounts this editor at boot and may
  // restore saved code into it a moment later, so that window is real.
  let ed = null, mon = null, buffer = starterCode, wantFocus = false;

  // A model takes its line endings from whatever text it was last given, and
  // an Enter typed into a fresh one can leave it on CRLF. This text goes to a
  // Python interpreter and into the session log, so LF is pinned on the model
  // and asked for again on the way out — reading is the boundary that matters.
  const useLF = () => ed.getModel().setEOL(mon.editor.EndOfLineSequence.LF);
  const getCode = () =>
    (ed ? ed.getModel().getValue(mon.editor.EndOfLinePreference.LF) : buffer);

  loadMonaco(doc).then((monaco) => {
    mon = monaco;
    monaco.editor.defineTheme("interview-dark", THEME);
    ed = monaco.editor.create(host, { ...OPTIONS, value: buffer });
    useLF();
    // The model is the single source of edits: setValue lands here too, so
    // listeners fire exactly once per change however the change was made.
    ed.onDidChangeModelContent(() => fire(getCode()));
    if (wantFocus) ed.focus();
  }).catch((err) => {
    host.textContent = `Editor failed to load: ${err.message}`;
    console.error(err);
  });

  function write(text, cls) {
    const span = doc.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text + "\n";
    out.append(span);
    out.scrollTop = out.scrollHeight;
  }

  const runner = createRunner({
    timeoutMs,
    onOutput: (stream, text) => write(text, stream === "stderr" ? "ed-err" : null),
    onStatus: (state, result) => {
      go.disabled = state !== "idle";
      halt.disabled = state !== "running";
      go.textContent = { booting: "Loading Python…", running: "Running…",
                         restarting: "Restarting Python…", idle: "Run" }[state];
      if (result) {
        if (result.status === "error") write(result.stderr.split("\n").slice(-40).join("\n"), "ed-err");
        else if (result.status !== "ok") write(result.stderr, "ed-killed");
        else if (result.value !== null) write(result.value, "ed-val");
        note.textContent = `${result.status} · ${result.ms} ms`;
      } else if (state === "running") {
        note.textContent = "running";
      }
      if (onStatus) onStatus(state, result);
    },
  });

  function run() {
    out.textContent = "";
    return runner.run(getCode());
  }
  go.onclick = run;
  halt.onclick = () => runner.stop();

  return {
    getCode,
    setCode(t) {
      if (ed) { ed.setValue(t); useLF(); }   // fires the model listener, which fires ours
      else { buffer = t; fire(t); }
    },
    onChange(cb) { listeners.push(cb); return () => listeners.splice(listeners.indexOf(cb), 1); },
    run,
    async runTests(tests) {
      const results = [];
      for (const t of tests) {
        // Appended, not injected: the test sees exactly the namespace the
        // candidate's own file would build.
        while (runner.state !== "idle") await new Promise((r) => setTimeout(r, 25));
        results.push(await runner.run(getCode() + "\n" + t));
      }
      return results;
    },
    stop: () => runner.stop(),
    get state() { return runner.state; },
    get bootMs() { return runner.bootMs; },
    focus() { if (ed) ed.focus(); else wantFocus = true; },
    destroy() {
      if (ed) { const m = ed.getModel(); ed.dispose(); m.dispose(); ed = null; }
      runner.destroy();
      mount.innerHTML = "";
    },
  };
}
