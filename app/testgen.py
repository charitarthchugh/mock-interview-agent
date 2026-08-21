"""Turning a problem statement into tests that are actually true.

The whole module exists to keep one promise: an expected output is either
copied from the statement or computed by a reference solution that earned the
right to be believed. Nothing else is ever shown as ground truth.

Earning that right takes two gates, because one is not enough. A greedy coin
change -- the classic wrong answer -- passes every example LeetCode 322
publishes; statement examples are chosen to illustrate, not to discriminate. So
two references are sampled independently, both must reproduce the statement's
examples, and both must then agree on every generated input. Where they
disagree the hidden tests are thrown away and the suite says so, because the
interviewer's sharpest move is handing the candidate a concrete input that
breaks their approach, and a wrong one there is worse than none.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .backends import OMNI, OMNI_MODEL, check
from .sandbox import run_cases

TYPES = ["int", "float", "str", "bool", "List[int]", "List[float]", "List[str]",
         "List[bool]", "List[List[int]]", "List[List[str]]"]

LIST_TYPES = {t for t in TYPES if t.startswith("List[")}
ELEM = {"List[int]": "int", "List[float]": "float", "List[str]": "str",
        "List[bool]": "bool", "List[List[int]]": "List[int]",
        "List[List[str]]": "List[str]"}


# ----------------------------------------------------------------- shapes

@dataclass(frozen=True)
class Param:
    name: str
    type: str


@dataclass(frozen=True)
class Case:
    family: str
    inputs: dict
    expected: Any = None
    source: str = "generated"      # "statement" | "generated"


@dataclass(frozen=True)
class Spec:
    style: str                     # "leetcode" | "stdin"
    entry: str
    params: tuple[Param, ...]
    returns: str
    unordered: bool = False
    examples: tuple[Case, ...] = ()
    constraints: tuple[dict, ...] = ()

    @property
    def signature(self) -> str:
        args = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        return f"def {self.entry}({args}) -> {self.returns}"


@dataclass(frozen=True)
class TestSuite:
    """What the integrator gets. `oracle` is the only thing worth trusting on.

    oracle == "validated": every case in `hidden` carries an expected output
    two independent references agreed on. oracle == "none": `hidden` is empty
    and `reason` says why; `examples` may still be usable because they were
    copied out of the statement rather than computed.
    """
    oracle: str                    # "validated" | "none"
    reason: str = ""
    spec: Spec | None = None
    examples: tuple[Case, ...] = ()
    hidden: tuple[Case, ...] = ()
    reference: str | None = None   # only when oracle == "validated"
    stub: str = ""
    sandbox_mode: str = ""
    seconds: float = 0.0
    log: tuple[str, ...] = ()      # how it got here, for Session.log

    @property
    def entry(self) -> str:
        return self.spec.entry if self.spec else ""

    @property
    def signature(self) -> str:
        return self.spec.signature if self.spec else ""

    def as_dict(self) -> dict:
        """JSON for the HTTP layer. Expected outputs on hidden cases exist only
        when the oracle validated, which is enforced here as well as upstream."""
        def case(c: Case) -> dict:
            d = {"family": c.family, "inputs": c.inputs, "source": c.source}
            if c.source == "statement" or self.oracle == "validated":
                d["expected"] = c.expected
            return d

        return {
            "oracle": self.oracle,
            "reason": self.reason,
            "entry": self.entry,
            "signature": self.signature,
            "stub": self.stub,
            "examples": [case(c) for c in self.examples],
            "hidden": [case(c) for c in self.hidden] if self.oracle == "validated" else [],
            "sandbox": self.sandbox_mode,
            "seconds": round(self.seconds, 1),
        }


# ------------------------------------------------------- pure: validation

def is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def conforms(value: Any, typ: str) -> bool:
    """Does this JSON value match the declared type? Bools are not ints here:
    the model reaches for True when it means 1 and the confusion is silent."""
    if typ == "int":
        return is_int(value)
    if typ == "float":
        return is_int(value) or isinstance(value, float)
    if typ == "str":
        return isinstance(value, str)
    if typ == "bool":
        return isinstance(value, bool)
    if typ == "None":
        return value is None
    if typ in LIST_TYPES:
        return isinstance(value, list) and all(conforms(v, ELEM[typ]) for v in value)
    return False


TOKEN = re.compile(r"-?\d+(?:\.\d+)?|[A-Za-z]+")


def _tokens(text: str) -> set[str]:
    return set(TOKEN.findall(text))


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    if isinstance(value, (list, tuple)):
        return [leaf for v in value for leaf in _leaves(v)]
    return [value]


def grounded(value: Any, tokens: set[str]) -> float:
    """Fraction of this value's scalar leaves that literally appear in the
    statement. Deliberately a ratio, not a yes/no: a stdin sample writes its
    array as "10 20 20" rather than [10,20,20], so a token-set comparison is
    the only form that survives both statement styles."""
    leaves = [v for v in _leaves(value) if not isinstance(v, bool) and v is not None]
    if not leaves:
        return 1.0
    hits = 0
    for v in leaves:
        s = str(v)
        if s in tokens or (isinstance(v, float) and str(int(v)) in tokens
                           and float(v).is_integer()):
            hits += 1
        elif isinstance(v, str) and _tokens(v) <= tokens:
            hits += 1
    return hits / len(leaves)


def validate_spec(raw: dict, problem: str = "") -> list[str]:
    """Everything the JSON schema cannot promise. Shape is guaranteed by
    xgrammar; truth is not. Returns human-readable failures, fed straight back
    to the model on the retry."""
    bad: list[str] = []
    params = raw.get("params") or []
    if not params:
        bad.append("params is empty: no interface was extracted")
    names = [p.get("name", "") for p in params]
    if len(set(names)) != len(names):
        bad.append(f"duplicate parameter names: {names}")
    for n in names:
        if not n.isidentifier():
            bad.append(f"parameter name {n!r} is not a Python identifier")

    examples = raw.get("examples") or []
    if not examples:
        bad.append("examples is empty: no worked example to check a solution against")

    types = {p["name"]: p["type"] for p in params if "name" in p and "type" in p}
    tokens = _tokens(problem)
    for i, ex in enumerate(examples):
        inputs = ex.get("inputs")
        if not isinstance(inputs, dict):
            bad.append(f"example {i + 1}: inputs is not an object")
            continue
        if set(inputs) != set(names):
            bad.append(
                f"example {i + 1}: inputs keys {sorted(inputs)} do not match the "
                f"parameters {sorted(names)}; give one entry per parameter, by name")
            continue
        for name, value in inputs.items():
            if not conforms(value, types.get(name, "")):
                bad.append(f"example {i + 1}: {name}={json.dumps(value)[:60]} is not "
                           f"a {types.get(name)}")
        if not conforms(ex.get("expected"), raw.get("returns", "")):
            bad.append(f"example {i + 1}: expected "
                       f"{json.dumps(ex.get('expected'))[:60]} is not a "
                       f"{raw.get('returns')}")
        if problem and grounded(inputs, tokens) < 0.6:
            bad.append(f"example {i + 1}: its input values are not written anywhere in "
                       f"the statement; do not invent examples")

    for c in raw.get("constraints") or []:
        lo, hi = c.get("lo"), c.get("hi")
        if lo is not None and hi is not None and lo > hi:
            bad.append(f"constraint {c.get('expr')!r}: lo {lo} exceeds hi {hi}")
        if c.get("target") and lo is None and hi is None:
            bad.append(f"constraint {c.get('expr')!r}: names a target but no bounds")
    return bad


def retarget_constraints(raw: dict, problem: str) -> dict:
    """Rewrite a bound written on a length variable onto the array it measures.

    HackerRank declares "int A[n]" and then bounds "1 <= n <= 10". Read
    literally that is a bound on a parameter named n, which for this statement
    does not exist -- so A comes out unbounded and the generator happily builds
    a hundred-element array the oracle cannot chew. Measured on Jesse loves
    cookies: 28 of 32 generated cases were thrown away for timing out.
    """
    names = {p.get("name") for p in raw.get("params") or []}
    lists = [p["name"] for p in (raw.get("params") or [])
             if p.get("type", "").startswith("List[")]
    out = []
    for c in raw.get("constraints") or []:
        target = (c.get("target") or "").strip()
        if target and target.isidentifier() and target not in names:
            for name in lists:
                if re.search(rf"\b{re.escape(name)}\s*\[\s*{re.escape(target)}\s*\]",
                             problem):
                    c = dict(c, target=f"{name}.length")
                    break
        out.append(c)
    return dict(raw, constraints=out)


def to_spec(raw: dict) -> Spec:
    return Spec(
        style=raw.get("style", "leetcode"),
        entry=raw["entry"],
        params=tuple(Param(p["name"], p["type"]) for p in raw["params"]),
        returns=raw.get("returns", "int"),
        unordered=bool(raw.get("unordered")),
        examples=tuple(Case("statement", ex["inputs"], ex.get("expected"), "statement")
                       for ex in raw.get("examples", [])),
        constraints=tuple(raw.get("constraints", ())),
    )


STDIN_MARKS = ("input format", "stdin", "first line contains", "standard input",
               "sample input", "read from")
LEETCODE_MARKS = ("class solution", "input: ", "return the", "leetcode")


def detect_style(problem: str, claimed: str) -> str:
    """The model's guess, overruled by a substring check. The check is cheap and
    cannot hallucinate; the guess is neither."""
    low = problem.lower()
    if any(m in low for m in STDIN_MARKS):
        return "stdin"
    if any(m in low for m in LEETCODE_MARKS):
        return "leetcode"
    return claimed


# ------------------------------------------------------------ pure: stub

STDIN_READ = {
    "int": "int(input())",
    "float": "float(input())",
    "str": "input()",
    "bool": "bool(int(input()))",
    "List[int]": "list(map(int, input().split()))",
    "List[float]": "list(map(float, input().split()))",
    "List[str]": "input().split()",
    "List[bool]": "[bool(int(x)) for x in input().split()]",
}


def stub_for(spec: Spec) -> str:
    """The candidate's starting point. Both styles are one function underneath;
    style only decides whether a stdin harness is bolted on below it."""
    args = ", ".join(f"{p.name}: {p.type}" for p in spec.params)
    if spec.style == "leetcode":
        body = (f"class Solution:\n    def {spec.entry}(self, {args}) -> "
                f"{spec.returns}:\n        pass\n")
        return body
    lines = [f"def {spec.entry}({args}) -> {spec.returns}:", "    pass", "", "",
             'if __name__ == "__main__":']
    for p in spec.params:
        lines.append(f"    {p.name} = {STDIN_READ.get(p.type, 'input()')}")
    lines.append(f"    print({spec.entry}({', '.join(p.name for p in spec.params)}))")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------ pure: comparison

def compare(got: Any, expected: Any, spec: Spec) -> bool:
    if spec.unordered and isinstance(got, list) and isinstance(expected, list):
        try:
            return sorted(got) == sorted(expected)
        except TypeError:
            return sorted(map(repr, got)) == sorted(map(repr, expected))
    if spec.returns in ("float", "List[float]"):
        g, e = _leaves(got), _leaves(expected)
        return len(g) == len(e) and all(
            isinstance(a, (int, float)) and isinstance(b, (int, float))
            and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9) for a, b in zip(g, e))
    if isinstance(got, (list, tuple)) and isinstance(expected, (list, tuple)):
        return list(got) == list(expected)
    return got == expected


# ------------------------------------------------------ pure: constraints

SIZE_KEYS = ("length", "size", "len", "count")


def bounds_for(spec: Spec, name: str, size: bool) -> tuple[int | None, int | None]:
    """Bounds a constraint licenses for one parameter, either its length or its
    element values. An absent bound stays None: a family the statement does not
    license is skipped, never invented."""
    lo = hi = None
    for c in spec.constraints:
        target = (c.get("target") or "").strip()
        if not target:
            continue
        t = target.lower()
        base = re.split(r"[.\[(]", t)[0].strip(" )")
        if base != name.lower() and t != name.lower():
            continue
        is_size = any(k in t for k in SIZE_KEYS) or t.endswith(")")
        if is_size == size:
            if c.get("lo") is not None:
                lo = c["lo"] if lo is None else min(lo, c["lo"])
            if c.get("hi") is not None:
                hi = c["hi"] if hi is None else max(hi, c["hi"])
    return lo, hi


def within_constraints(inputs: dict, spec: Spec) -> bool:
    """Re-check every generated input against the statement's own bounds. A
    generator that drifts outside them produces a test the candidate can
    rightly refuse."""
    for p in spec.params:
        if p.name not in inputs:
            return False
        v = inputs[p.name]
        if not conforms(v, p.type):
            return False
        if isinstance(v, list):
            lo, hi = bounds_for(spec, p.name, size=True)
            if lo is not None and len(v) < lo:
                return False
            if hi is not None and len(v) > hi:
                return False
            elo, ehi = bounds_for(spec, p.name, size=False)
            flat = [x for x in _leaves(v) if isinstance(x, (int, float))
                    and not isinstance(x, bool)]
            if elo is not None and flat and min(flat) < elo:
                return False
            if ehi is not None and flat and max(flat) > ehi:
                return False
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = bounds_for(spec, p.name, size=False)
            if lo is not None and v < lo:
                return False
            if hi is not None and v > hi:
                return False
        elif isinstance(v, str):
            lo, hi = bounds_for(spec, p.name, size=True)
            if lo is not None and len(v) < lo:
                return False
            if hi is not None and len(v) > hi:
                return False
    return True


# ------------------------------------------------------- pure: families

ALPHA = "abcdefghijklmnopqrstuvwxyz"


def _clamp(v: int, lo: int | None, hi: int | None) -> int:
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def _values(k: int, mode: str, lo: int, hi: int, rng: random.Random) -> list[int]:
    span = max(1, hi - lo)
    if mode == "identical":
        return [_clamp(lo + span // 2, lo, hi)] * k
    if mode == "sorted":
        return sorted({_clamp(lo + (i * span) // max(1, k), lo, hi) for i in range(k)}) \
            or [lo] * k
    if mode == "reverse":
        return list(reversed(_values(k, "sorted", lo, hi, rng)))
    if mode == "dups":
        half = _values(max(1, k // 2), "random", lo, hi, rng)
        return (half * 2)[:k] or [lo]
    if mode == "negatives":
        return [rng.randint(lo, min(-1, hi)) for _ in range(k)]
    if mode == "extreme":
        return [rng.choice((lo, hi)) for _ in range(k)]
    return [rng.randint(lo, hi) for _ in range(k)]


def _build(p: Param, k: int, mode: str, spec: Spec, rng: random.Random) -> Any:
    """One parameter's value for one family. `k` is the size budget."""
    if p.type in ("int", "float"):
        lo, hi = bounds_for(spec, p.name, size=False)
        lo = 0 if lo is None else lo
        hi = k if hi is None else min(hi, max(lo, k))
        if mode == "min":
            v = lo
        elif mode == "zero":
            v = _clamp(0, lo, hi)
        elif mode == "max":
            v = hi
        else:
            v = rng.randint(lo, hi)
        return float(v) if p.type == "float" else v

    if p.type == "bool":
        return rng.random() < 0.5

    lo_n, hi_n = bounds_for(spec, p.name, size=True)
    n = _clamp(k, lo_n, hi_n)
    if mode == "min":
        n = lo_n if lo_n is not None else 1
    elif mode == "empty":
        n = 0
    elif mode == "single":
        n = 1
    n = max(0, _clamp(n, lo_n, hi_n))

    if p.type == "str":
        letters = ALPHA[:max(1, min(26, k))]
        if mode == "identical":
            return letters[0] * n
        if mode == "sorted":
            return "".join(sorted(rng.choice(letters) for _ in range(n)))
        if mode == "dups":
            half = "".join(rng.choice(letters[:2]) for _ in range(max(1, n // 2)))
            return (half * 2)[:n]
        return "".join(rng.choice(letters) for _ in range(n))

    if p.type in ("List[List[int]]", "List[List[str]]"):
        inner = Param(p.name, ELEM[p.type])
        rows = max(1, min(n, max(1, int(math.sqrt(max(1, k)))))) if mode != "empty" else 0
        width = max(1, min(k, rows))
        return [_build(inner, width, "random" if mode in ("min", "empty") else mode,
                       spec, rng) for _ in range(rows)]

    elem = ELEM[p.type]
    if elem == "str":
        return ["".join(rng.choice(ALPHA[:5]) for _ in range(rng.randint(1, 3)))
                for _ in range(n)]
    if elem == "bool":
        return [rng.random() < 0.5 for _ in range(n)]
    lo_v, hi_v = bounds_for(spec, p.name, size=False)
    lo_v = 1 if lo_v is None else lo_v
    hi_v = k if hi_v is None else min(hi_v, max(lo_v, k))
    vals = _values(n, mode if mode in ("identical", "sorted", "reverse", "dups",
                                       "negatives", "extreme") else "random",
                   lo_v, hi_v, rng)
    return [float(v) for v in vals] if elem == "float" else vals


RECIPES = [
    ("minimum size", "min"),
    ("empty", "empty"),
    ("single element", "single"),
    ("all identical", "identical"),
    ("sorted ascending", "sorted"),
    ("reverse sorted", "reverse"),
    ("duplicates", "dups"),
    ("negative values", "negatives"),
    ("extreme values", "extreme"),
    ("largest the oracle can compute", "max"),
]


def families(spec: Spec, budget: int, sweep: int = 24, seed: int = 17
             ) -> list[tuple[str, dict]]:
    """Candidate hidden inputs, before any oracle has seen them.

    `budget` is the size the reference was measured to survive, not the
    statement's maximum. Labelling a case "constraint maximum" when the
    reference could not actually compute it would be the same sin as guessing
    an output, so the largest family says what it really is.
    """
    rng = random.Random(seed)
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    has_list = any(p.type in LIST_TYPES or p.type == "str" for p in spec.params)

    def offer(label: str, inputs: dict) -> None:
        key = json.dumps(inputs, sort_keys=True)
        if key in seen or not within_constraints(inputs, spec):
            return
        seen.add(key)
        out.append((label, inputs))

    for label, mode in RECIPES:
        if mode in ("empty", "single", "identical", "sorted", "reverse", "dups") \
                and not has_list:
            continue
        if mode == "negatives" and not any(
                (bounds_for(spec, p.name, size=False)[0] or 0) < 0 for p in spec.params):
            continue
        # `or 1` would swallow a legitimate lower bound of zero, which is the
        # only value this gate is looking for.
        if mode == "empty" and not any(
                bounds_for(spec, p.name, size=True)[0] == 0 for p in spec.params):
            continue
        inputs = {p.name: _build(p, budget, mode, spec, rng) for p in spec.params}
        _link_sizes(inputs, spec)
        offer(label, inputs)

    # A seeded random sweep is where the discriminating cases come from: the
    # families above are the shapes a human thinks of, and the traps are not.
    for i in range(sweep):
        k = rng.randint(1, max(1, budget))
        inputs = {p.name: _build(p, k, "random", spec, rng) for p in spec.params}
        _link_sizes(inputs, spec)
        offer("adversarial sweep", inputs)
    return out


def _link_sizes(inputs: dict, spec: Spec) -> None:
    """HackerRank statements pass the array and its length as two arguments. A
    length that disagrees with the array is not a hard test, it is a broken one."""
    lists = [p.name for p in spec.params if isinstance(inputs.get(p.name), list)]
    if len(lists) != 1:
        return
    n = len(inputs[lists[0]])
    for p in spec.params:
        if p.type == "int" and p.name.lower() in ("n", "m", "size", "count", "length"):
            inputs[p.name] = n


# ---------------------------------------------------------- model calls

SPEC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["style", "entry", "params", "returns", "examples", "constraints"],
    "properties": {
        "style": {"type": "string", "enum": ["leetcode", "stdin"]},
        "entry": {"type": "string"},
        "params": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "type"],
            "properties": {"name": {"type": "string"},
                           "type": {"type": "string", "enum": TYPES}}}},
        "returns": {"type": "string", "enum": TYPES + ["None"]},
        "unordered": {"type": "boolean"},
        "examples": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["inputs", "expected"],
            "properties": {"inputs": {"type": "object"}, "expected": {},
                           "note": {"type": "string"}}}},
        "constraints": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["expr"],
            "properties": {"expr": {"type": "string"}, "target": {"type": "string"},
                           "lo": {"type": "integer"}, "hi": {"type": "integer"}}}},
    },
}

EXTRACT = '''Transcribe the interface and the worked examples of a coding problem
into JSON. Transcribe only what the statement says: do not solve it, and do not
invent an example that is not written down.

  style     "leetcode" when the statement names a method or writes
            "Input: name = value"; "stdin" when it reads standard input.
  entry     the function name exactly as the statement names it.
  params    one per argument, in the statement's order. type is the type of
            the WHOLE argument: an array of numbers is List[int], not int.
  returns   the type of the returned value.
  unordered true only if the statement says the answer may be in any order.
  examples  one per worked example. inputs maps each parameter NAME to that
            example's value, written as a JSON literal of the declared type.
            expected is the stated output.
  constraints  one per line of the constraints section; expr verbatim, target
            the thing bounded (a parameter name, or name.length for an array
            length), and lo and hi the numeric bounds with any powers
            evaluated.

Worked illustration. For a statement reading

  Implement twoSum(nums, target) -> indices of the two numbers adding to
  target. Example 1: Input: nums = [2,7,11,15], target = 9  Output: [0,1]
  Constraints: 2 <= nums.length <= 10^4

you would answer

  {"style":"leetcode","entry":"twoSum",
   "params":[{"name":"nums","type":"List[int]"},{"name":"target","type":"int"}],
   "returns":"List[int]","unordered":false,
   "examples":[{"inputs":{"nums":[2,7,11,15],"target":9},"expected":[0,1]}],
   "constraints":[{"expr":"2 <= nums.length <= 10^4","target":"nums.length",
                   "lo":2,"hi":10000}]}

PROBLEM STATEMENT
{problem}'''

REF_SCHEMA = {"type": "object", "additionalProperties": False,
              "required": ["reason", "code"],
              "properties": {"reason": {"type": "string"},
                             "code": {"type": "string"}}}

REFERENCE = '''Write a REFERENCE SOLUTION for the problem below. It is not judged
on speed. It is judged only on being obviously, provably correct, so that a
reader can check it by eye.

Write the slowest, most literal solution you can:
- enumerate the whole search space and take the best; do not prune.
- prefer an exhaustive loop or a plain recursion over any formula, greedy rule
  or clever data structure. A greedy rule that "looks right" is the single most
  common way a reference solution is wrong.
- no imports except itertools, math, collections, heapq and functools.
- no input(), no print(), no files, no network, no randomness, no globals.

Signature, which you must match exactly:
{signature}

Return a JSON object with two fields:
  reason  one sentence saying why this code is exhaustive rather than clever.
  code    the complete function as one string, def line included, nothing else.

PROBLEM STATEMENT
{problem}'''


async def _complete(prompt: str, schema: dict, name: str, temperature: float,
                    max_tokens: int) -> dict:
    payload = {
        "model": OMNI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        # xgrammar constrains the decode, so the reply parses or the server
        # errors -- there is no half-JSON case to defend against here.
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "schema": schema,
                                            "strict": True}},
    }
    async with httpx.AsyncClient(timeout=300) as client:
        r = check(await client.post(f"{OMNI}/v1/chat/completions", json=payload))
        return json.loads(r.json()["choices"][0]["message"]["content"])


async def extract(problem: str, feedback: str = "") -> tuple[dict, list[str]]:
    """The statement's interface and its own examples, as JSON. Returns the raw
    object and whatever validate_spec found wrong with it."""
    prompt = EXTRACT.replace("{problem}", problem)
    if feedback:
        prompt += "\n\nEXTRACTION PROBLEMS with your previous answer\n" + feedback
    raw = await _complete(prompt, SPEC_SCHEMA, "spec", 0.0, 1600)
    raw["style"] = detect_style(problem, raw.get("style", "leetcode"))
    raw = retarget_constraints(raw, problem)
    return raw, validate_spec(raw, problem)


async def reference(problem: str, spec: Spec, temperature: float,
                    feedback: str = "") -> dict:
    prompt = REFERENCE.format(signature=spec.signature, problem=problem)
    if feedback:
        prompt += "\n\nYOUR PREVIOUS ATTEMPT WAS WRONG\n" + feedback
    return await _complete(prompt, REF_SCHEMA, "reference", temperature, 1400)


# ------------------------------------------------------------ orchestration

def check_examples(code: str, spec: Spec) -> tuple[bool, list[str], int]:
    """Run a candidate reference against the statement's own examples.

    Returns (passed, failure lines for the retry prompt, number failed). The
    count matters: a wrong algorithm fails some examples, a mis-parsed argument
    order fails all of them, and those want different repairs.
    """
    cases = [dict(ex.inputs) for ex in spec.examples]
    r = run_cases(code, spec.entry, cases, timeout=20.0, case_seconds=4.0)
    if not r["ok"]:
        return False, [f"your code did not run: {r['error']}"], len(cases)
    lines, failed = [], 0
    for ex, got in zip(spec.examples, r["results"]):
        if "error" in got:
            failed += 1
            lines.append(f"{_call(spec, ex.inputs)} raised {got['error']}")
        elif not compare(got["got"], ex.expected, spec):
            failed += 1
            lines.append(f"{_call(spec, ex.inputs)} -> your code returned "
                         f"{json.dumps(got['got'])}, the statement says "
                         f"{json.dumps(ex.expected)}")
    return (failed == 0), lines, failed


def _call(spec: Spec, inputs: dict) -> str:
    args = ", ".join(f"{p.name}={json.dumps(inputs.get(p.name))[:40]}"
                     for p in spec.params)
    return f"{spec.entry}({args})"


def probe_budget(code: str, spec: Spec, ceiling: float = 0.4) -> int:
    """The largest input size the reference actually finishes on.

    The brute force is exponential by construction, so the statement's maximum
    is fiction for our purposes: coin change allows amount <= 10000 and the
    reference tops out near 26. Doubling until it hurts finds the real number.

    Three shapes are timed at each size, not one. A single "max" input is not
    representative -- measured, it sailed through size 60 on coin change while
    the random inputs at that size ran for minutes -- and an over-large budget
    means every generated case is later dropped for timing out.
    """
    best = 1
    for size in (2, 4, 8, 12, 16, 20, 24, 30, 40, 60, 100):
        probes = []
        for i, mode in enumerate(("max", "random", "extreme")):
            inputs = {p.name: _build(p, size, mode, spec, random.Random(3 + i))
                      for p in spec.params}
            _link_sizes(inputs, spec)
            if within_constraints(inputs, spec):
                probes.append(inputs)
        if not probes:
            continue
        r = run_cases(code, spec.entry, probes, timeout=4.0,
                      case_seconds=ceiling * 2)
        if not r["ok"] or any("error" in x for x in r["results"]):
            break
        if max(x["ms"] for x in r["results"]) > ceiling * 1000:
            best = size
            break
        best = size
    return best


REF_TIMEOUT = 25.0


async def generate_tests(problem: str, budget_seconds: float = 90.0) -> TestSuite:
    """Everything, end to end. Never raises: a failure is a TestSuite that says
    it has no oracle, because the caller is a background task on a live
    interview and there is nothing useful for it to catch."""
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(_generate(problem, started), budget_seconds)
    except asyncio.TimeoutError:
        return TestSuite("none", f"generation exceeded {budget_seconds:.0f}s",
                         seconds=time.perf_counter() - started)
    except Exception as e:  # a dead vLLM must not take the interview with it
        return TestSuite("none", f"{type(e).__name__}: {e}",
                         seconds=time.perf_counter() - started)


async def _generate(problem: str, started: float) -> TestSuite:
    from . import sandbox

    log: list[str] = []

    def done(oracle: str, reason: str, **kw) -> TestSuite:
        return TestSuite(oracle, reason, seconds=time.perf_counter() - started,
                         sandbox_mode=sandbox.mode(), log=tuple(log), **kw)

    # --- 1. the statement's own interface and examples
    spec: Spec | None = None
    feedback = ""
    for attempt in range(2):
        raw, bad = await extract(problem, feedback)
        log.append(f"extract attempt {attempt + 1}: "
                   f"{'ok' if not bad else '; '.join(bad)[:200]}")
        if not bad:
            spec = to_spec(raw)
            break
        feedback = "\n".join("- " + b for b in bad)
    if spec is None:
        return done("none", f"could not read the statement: {feedback[:200]}")

    stub = stub_for(spec)

    # --- 2. two independent references, both gated on the examples
    refs = await asyncio.gather(
        _earn_reference(problem, spec, 0.0, log, "A"),
        _earn_reference(problem, spec, 0.8, log, "B"),
    )
    if any(r is None for r in refs):
        which = "A" if refs[0] is None else "B"
        return done("none",
                    f"reference {which} never reproduced the statement's examples",
                    spec=spec, examples=spec.examples, stub=stub)
    ref_a, ref_b = refs

    # --- 3. candidate inputs, sized to what the oracle can actually compute
    budget = await asyncio.to_thread(probe_budget, ref_a, spec)

    # The probe times ref A on three shapes; ref B may be slower, and the sweep
    # reaches inputs neither shape covers. Rather than trust one estimate, keep
    # halving until most of the suite survives -- a suite that is mostly
    # dropped cases is not much of a suite.
    for _ in range(4):
        candidates = families(spec, budget)
        if not candidates:
            return done("none", "no hidden input satisfied the stated constraints",
                        spec=spec, examples=spec.examples, stub=stub)
        inputs = [dict(i) for _, i in candidates]
        each = 1.0
        wall = 15.0 + each * len(inputs)
        # Both references at once, off the loop. Called directly they also
        # serialised each other, so the two-reference check cost double.
        ra, rb = await asyncio.gather(
            asyncio.to_thread(run_cases, ref_a, spec.entry, inputs, wall, each),
            asyncio.to_thread(run_cases, ref_b, spec.entry, inputs, wall, each),
        )
        if not ra["ok"] or not rb["ok"]:
            return done("none",
                        f"references would not run: {ra.get('error') or rb.get('error')}",
                        spec=spec, examples=spec.examples, stub=stub)
        reached = sum(1 for a, b in zip(ra["results"], rb["results"])
                      if "error" not in a and "error" not in b)
        log.append(f"budget {budget}: {reached}/{len(candidates)} within reach")
        if reached >= 0.75 * len(candidates) or budget <= 4:
            break
        budget = max(4, budget // 2)

    hidden: list[Case] = []
    disagreed = dropped = unusable = 0
    for (label, inp), a, b in zip(candidates, ra["results"], rb["results"]):
        if "error" in a or "error" in b:
            # An input the oracle cannot compute is not evidence of anything;
            # it is simply out of reach, so it leaves rather than accusing.
            dropped += 1
            continue
        if not compare(a["got"], b["got"], spec):
            disagreed += 1
            continue
        if not conforms(a["got"], spec.returns):
            # Both references agreeing on None only says the input broke a
            # precondition stated in prose -- "exactly one solution exists" --
            # which the numeric constraint check cannot see. Shipping it would
            # fail a correct answer, which is worse than having no test.
            unusable += 1
            continue
        hidden.append(Case(label, inp, a["got"], "generated"))

    log.append(f"agreement: {len(hidden)}/{len(candidates)} "
               f"(dropped {dropped}, disagreed {disagreed}, "
               f"off-type {unusable})")
    if disagreed:
        # An unpruned brute force is either right or wrong; it is not right on
        # 90% of inputs. Partial agreement means one of them is a different
        # algorithm, so nothing here is trustworthy -- take the honest loss.
        return done("none",
                    f"the two references disagreed on {disagreed} of "
                    f"{len(candidates)} generated inputs",
                    spec=spec, examples=spec.examples, stub=stub)

    if not hidden:
        return done("none", "no generated input was within the oracle's reach",
                    spec=spec, examples=spec.examples, stub=stub)
    return done("validated", "", spec=spec, examples=spec.examples,
                hidden=tuple(hidden), reference=ref_a, stub=stub)


async def _earn_reference(problem: str, spec: Spec, temperature: float,
                          log: list[str], tag: str) -> str | None:
    """Up to three attempts, each told exactly which example it got wrong."""
    feedback = ""
    for attempt in range(3):
        temp = temperature if attempt == 0 else max(0.6, temperature) + 0.1 * attempt
        try:
            out = await asyncio.wait_for(
                reference(problem, spec, temp, feedback), REF_TIMEOUT)
        except (asyncio.TimeoutError, RuntimeError, ValueError) as e:
            log.append(f"ref {tag} attempt {attempt + 1}: {type(e).__name__}")
            continue
        code = out.get("code", "")
        ok, lines, failed = await asyncio.to_thread(check_examples, code, spec)
        log.append(f"ref {tag} attempt {attempt + 1}: "
                   f"{'passes all examples' if ok else f'fails {failed}'}")
        if ok:
            return code
        feedback = ("REFERENCE FAILED\n" + "\n".join(lines[:4]) +
                    "\nYour approach is wrong, not just buggy. Enumerate every "
                    "possibility instead of applying a rule.")
    return None
