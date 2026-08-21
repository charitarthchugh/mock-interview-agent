// Pyodide worker. Speaks a four-message protocol and nothing else; stopping a
// runaway is the main thread's job (terminate), because a wedged worker cannot
// answer a "please stop" message.
//
//   in   {cmd:"run", id, code}
//   out  {type:"ready", ms}
//        {type:"out", id, stream:"stdout"|"stderr", text}
//        {type:"done", id, status:"ok",    value, ms}
//        {type:"done", id, status:"error", error, ms}
import { loadPyodide } from "./pyodide.mjs";

const t0 = performance.now();
let py = null, dict = null, current = null;
const queued = [];

// Installed before the await below: a run posted while Pyodide is still
// loading would otherwise be dispatched to no handler and vanish.
onmessage = (e) => {
  if (!e.data || e.data.cmd !== "run") return;
  if (py) run(e.data.id, e.data.code); else queued.push(e.data);
};

// Relative to this module, so the mount prefix can move without edits.
py = await loadPyodide({ indexURL: new URL("./", import.meta.url).href });
dict = py.globals.get("dict");

py.setStdout({ batched: (t) => emit("stdout", t) });
py.setStderr({ batched: (t) => emit("stderr", t) });

function emit(stream, text) {
  if (current !== null) postMessage({ type: "out", id: current, stream, text });
}

// Pyodide's own frames (_pyodide/_base.py, <exec> wrappers) are noise a
// candidate should not spend interview minutes reading.
function cleanTraceback(raw) {
  const lines = String(raw).replace(/\s+$/, "").split("\n");
  const kept = [];
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^\s*File "<exec>", line (\d+)(.*)$/);
    if (m) {
      kept.push(`  File "your code", line ${m[1]}${m[2]}`);
      if (i + 1 < lines.length && /^\s{4,}\S/.test(lines[i + 1])) kept.push(lines[++i]);
    }
  }
  const last = lines[lines.length - 1] || "";
  if (kept.length) return ["Traceback (most recent call last):", ...kept, last].join("\n");
  return last || String(raw);
}

// Each run gets a fresh namespace: a solution that only passes because a name
// survived from an earlier run has taught the candidate something false.
function run(id, code) {
  const start = performance.now();
  const ns = dict();
  current = id;
  let result;
  try {
    const v = py.runPython(code, { globals: ns });
    let value = null;
    if (v !== undefined && v !== null) {
      value = String(v);
      if (typeof v.destroy === "function") v.destroy();
    }
    result = { type: "done", id, status: "ok", value, ms: Math.round(performance.now() - start) };
  } catch (e) {
    result = {
      type: "done", id, status: "error",
      error: cleanTraceback(e && e.message ? e.message : e),
      ms: Math.round(performance.now() - start),
    };
  } finally {
    ns.destroy();
    current = null;
  }
  postMessage(result);
}

postMessage({ type: "ready", ms: Math.round(performance.now() - t0) });
while (queued.length) { const q = queued.shift(); run(q.id, q.code); }
