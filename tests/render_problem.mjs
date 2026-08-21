// Structure recovery for problem statements, checked without a browser.
//
//   node tests/render_problem.mjs
//
// `renderProblem` touches no browser API beyond createElement/textContent, so a
// twenty-line DOM shim is enough to assert on the tree it builds — which keeps
// the parser's real failure mode (a statement it mangles) in a test that runs in
// milliseconds, rather than only in a screenshot.

class Node {
  constructor(tag) {
    this.tagName = (tag || "").toUpperCase();
    this.className = "";
    this.children = [];
    this._text = "";
  }
  set textContent(v) {
    this._text = String(v);
    this.children = [];
  }
  get textContent() {
    return this.children.length
      ? this.children.map((c) => c.textContent).join("")
      : this._text;
  }
  append(...kids) {
    for (const k of kids) {
      if (k.tagName === "#FRAGMENT") this.children.push(...k.children);
      else this.children.push(k);
    }
  }
  find(sel) {
    const [tag, cls] = sel.split(".");
    const hit = [];
    const walk = (n) => {
      for (const c of n.children) {
        const tagOk = !tag || c.tagName === tag.toUpperCase();
        const clsOk = !cls || c.className.split(" ").includes(cls);
        if (tagOk && clsOk) hit.push(c);
        walk(c);
      }
    };
    walk(this);
    return hit;
  }
}

globalThis.document = {
  createElement: (t) => new Node(t),
  createDocumentFragment: () => new Node("#fragment"),
};

const { renderProblem } = await import("../static/problem.js");

let failed = 0;
const is = (got, want, what) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) console.log(`       got  ${JSON.stringify(got)}\n       want ${JSON.stringify(want)}`);
};

// The real thing: pasted from LeetCode, separators are non-breaking spaces, and
// the copy has already lost the exponents from the constraints.
const LEETCODE = [
  "53. Maximum Subarray",
  "Medium",
  "",
  "Given an integer array nums, find the with the largest sum, and return its sum.",
  "",
  " ",
  "",
  "Example 1:",
  "",
  "Input: nums = [-2,1,-3,4,-1,2,1,-5,4]",
  "Output: 6",
  "Explanation: The subarray [4,-1,2,1] has the largest sum 6.",
  "",
  "Example 2:",
  "",
  "Input: nums = [1]",
  "Output: 1",
  "",
  " ",
  "",
  "Constraints:",
  "",
  "    1 <= nums.length <= 105",
  "    -104 <= nums[i] <= 104",
  "",
  " ",
  "",
  "Follow up: If you have figured out the O(n) solution, try coding another",
  "solution using the divide and conquer approach.",
].join("\n");

{
  const f = renderProblem(LEETCODE);
  is(f.find("h1").map((n) => n.textContent), ["53. Maximum Subarray"], "title becomes a heading");
  is(f.find("span.chip").map((n) => [n.textContent, n.className]),
     [["Medium", "chip medium"]], "difficulty becomes a chip");
  is(f.find("p.p").map((n) => n.textContent),
     ["Given an integer array nums, find the with the largest sum, and return its sum."],
     "description survives as one paragraph, separators dropped");
  is(f.find("section.ex").length, 2, "both examples become sections");
  is(f.find("h3").map((n) => n.textContent),
     ["Example 1", "Example 2", "Constraints"], "each section is labelled");
  is(f.find("div.field").map((n) => n.children.map((c) => c.textContent)),
     [["Input", "nums = [-2,1,-3,4,-1,2,1,-5,4]"], ["Output", "6"],
      ["Explanation", "The subarray [4,-1,2,1] has the largest sum 6."],
      ["Input", "nums = [1]"], ["Output", "1"]],
     "Input/Output/Explanation split into labelled rows");
  is(f.find("li").map((n) => n.textContent),
     ["1 <= nums.length <= 105", "-104 <= nums[i] <= 104"],
     "constraints listed verbatim, including the exponents the paste ate");
  is(f.find("p.note").map((n) => n.textContent),
     ["Follow up: If you have figured out the O(n) solution, try coding another\nsolution using the divide and conquer approach."],
     "the follow-up is set apart");
  // Nothing may be silently dropped: every non-blank source line must survive.
  // Headings lose their trailing colon and Input:/Output: split into two nodes,
  // so the comparison ignores colons and runs of space -- and nothing else.
  const flat = (s) => s.replace(/:/g, "").replace(/\s+/g, " ").trim();
  // Sibling boxes are separated on screen, so join them with a space rather
  // than the empty string textContent would give.
  const visible = (n) => n.children.length ? n.children.map(visible).join(" ") : n.textContent;
  const rendered = flat(visible(f));
  const lost = LEETCODE.split("\n").map(flat).filter((l) => l && !rendered.includes(l));
  is(lost, [], "no line is lost between the text and the render");
}

// The fallback that matters: a statement with none of the furniture.
{
  const free = "Rate limiter\n\nIt should allow N requests per window.\nThink about clock skew.";
  const f = renderProblem(free);
  is(f.find("h1").map((n) => n.textContent), ["Rate limiter"], "short first line still titles");
  is(f.find("p.p").map((n) => n.textContent),
     ["It should allow N requests per window.\nThink about clock skew."],
     "the rest degrades to a paragraph, not to nothing");
  is(f.find("section").length, 0, "no sections are invented");
}

// A first line that is plainly prose must not be stolen for a title.
{
  const f = renderProblem("Given an array of integers, return the indices of the two numbers such that they add up to a target.");
  is(f.find("h1").length, 0, "a long sentence is not a title");
  is(f.find("p.p").length, 1, "it is the description");
}

// Degenerate input must not throw.
for (const bad of ["", "   ", " \n ", null, undefined]) {
  const f = renderProblem(bad);
  is(f.children.length, 0, `empty input renders nothing (${JSON.stringify(bad)})`);
}

// Unlabelled example bodies keep their shape rather than being reflowed.
{
  const f = renderProblem("Q\n\nExample:\n\n  foo(1) -> 2\n  foo(2) -> 4\n\nConstraints:\n\n  n < 10");
  is(f.find("pre.raw").map((n) => n.textContent), ["  foo(1) -> 2\n  foo(2) -> 4"],
     "an example with no Input: label is preserved verbatim");
}

console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
