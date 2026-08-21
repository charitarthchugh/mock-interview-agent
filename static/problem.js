// Rendering the problem statement.
//
// The stored problem text is verbatim — `tidy_problem` deletes page furniture
// and is forbidden from rewriting a single character, because a constraint that
// quietly changes is worse than one that stays messy. So every bit of structure
// here is recovered at display time from the plain text, and nothing this file
// does can travel back: the model, the test generator and the transcript all
// read `session.problem`, never the DOM.
//
// Two things this deliberately does NOT do:
//   * restore lost superscripts (a pasted `10^5` arrives as `105`). That is a
//     content edit dressed as formatting, and `105` is a legal number.
//   * guess at inline code inside prose. Heuristics for `nums[i]`-shaped tokens
//     misfire on ordinary sentences; the blocks that hold code-ish text are
//     already monospace.
//
// The renderer is total. Anything it does not recognise becomes a paragraph, so
// a free-form statement pasted from an email still reads correctly.

// LeetCode's separator lines are a lone non-breaking space, not an empty line.
const blank = (l) => l.trim().replace(/ /g, "") === "";

const EXAMPLE = /^example\s*\d*\s*:?\s*$/i;
const CONSTRAINTS = /^constraints\s*:?\s*$/i;
const FOLLOWUP = /^follow[- ]?up\s*:/i;
const NOTE = /^note\s*:/i;
const DIFFICULTY = /^(easy|medium|hard)$/i;
const FIELD = /^(input|output|explanation)\s*:\s*/i;
// A title is the first line only when it looks like one: LeetCode numbers them,
// and an unnumbered title is short and unpunctuated.
const NUMBERED = /^\d+\s*[.)]\s+\S/;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Trailing blank lines removed, so a section never renders its own padding. */
function trimmed(lines) {
  const out = lines.slice();
  while (out.length && blank(out[out.length - 1])) out.pop();
  while (out.length && blank(out[0])) out.shift();
  return out;
}

/** Consecutive non-blank runs, each one a paragraph's worth of lines. */
function paragraphs(lines, into) {
  let run = [];
  const flush = () => {
    if (run.length) into.append(el("p", "p", run.join("\n")));
    run = [];
  };
  for (const line of lines) blank(line) ? flush() : run.push(line);
  flush();
}

/** Input:/Output:/Explanation: as labelled rows; anything else stays verbatim. */
function example(heading, lines, into) {
  const box = el("section", "ex");
  box.append(el("h3", null, heading.replace(/:\s*$/, "")));
  const body = el("div", "ex-body");
  let loose = [];
  const flush = () => {
    if (trimmed(loose).length) body.append(el("pre", "raw", trimmed(loose).join("\n")));
    loose = [];
  };
  for (const line of trimmed(lines)) {
    const m = line.match(FIELD);
    if (!m) {
      loose.push(line);
      continue;
    }
    flush();
    const row = el("div", "field");
    row.append(el("span", "label", m[1]));
    row.append(el("span", "value", line.slice(m[0].length)));
    body.append(row);
  }
  flush();
  box.append(body);
  into.append(box);
}

/** Constraints are inequalities: one per line, monospace, as a list. */
function constraints(lines, into) {
  const box = el("section", "cons");
  box.append(el("h3", null, "Constraints"));
  const list = el("ul");
  for (const line of trimmed(lines)) {
    if (blank(line)) continue;
    list.append(el("li", null, line.trim()));
  }
  box.append(list);
  into.append(box);
}

/**
 * Structure recovered from a plain-text problem statement.
 * @param {string} text
 * @returns {DocumentFragment}
 */
export function renderProblem(text) {
  const frag = document.createDocumentFragment();
  const lines = trimmed(String(text ?? "").split("\n"));
  if (!lines.length) return frag;

  let i = 0;
  const first = lines[0].trim();
  if (NUMBERED.test(first) || (first.length <= 70 && !/[.?!,;]$/.test(first) && lines.length > 1)) {
    frag.append(el("h1", null, first));
    i = 1;
  }
  if (i < lines.length && DIFFICULTY.test(lines[i].trim())) {
    frag.append(el("span", "chip " + lines[i].trim().toLowerCase(), lines[i].trim()));
    i += 1;
  }

  // Everything from here is a run of lines owned by the heading that opened it.
  let body = [];
  let open = null; // null = description, otherwise the heading line
  const close = () => {
    if (open === null) paragraphs(trimmed(body), frag);
    else if (CONSTRAINTS.test(open)) constraints(body, frag);
    else example(open, body, frag);
    body = [];
  };

  for (; i < lines.length; i++) {
    const line = lines[i];
    const head = line.trim();
    if (EXAMPLE.test(head) || CONSTRAINTS.test(head)) {
      close();
      open = head;
    } else if (FOLLOWUP.test(head) || NOTE.test(head)) {
      close();
      open = null;
      // A follow-up runs to the blank line, not to the end of its first line.
      const note = [head];
      while (i + 1 < lines.length && !blank(lines[i + 1])) note.push(lines[++i]);
      frag.append(el("p", "note", note.join("\n")));
    } else {
      body.push(line);
    }
  }
  close();
  return frag;
}

/** Replace an element's contents with the rendered statement. */
export function showProblem(node, text) {
  node.textContent = "";
  node.append(renderProblem(text));
  return node;
}
