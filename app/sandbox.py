"""Running model-written Python without trusting it.

The threat model is an accident, not an adversary: the code comes from the
local model, from a statement the user pasted, through a JSON field that holds
one function. What actually happens is an unpruned recursion that never
returns, a four-gigabyte allocation, or a stray open(..., "w") -- so the guards
are resource limits, network isolation and a write-deny audit hook, layered so
that no single one of them is load-bearing.

bwrap is the intended jail and stays the default the moment it works. On this
box it cannot: Ubuntu 24.04's unprivileged_userns AppArmor profile denies every
capability to a process that creates a user namespace, and bwrap is not setuid
here, so the uid-map write fails with EPERM. A canary discovers that once, per
process, and falls back rather than pretending.
"""

from __future__ import annotations

import json
import contextlib
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import atexit
from dataclasses import dataclass
from pathlib import Path

# The interpreter running the app, not a guessed path: in a container Python
# lives under /usr/local, and inside a virtualenv it is somewhere else again.
PYTHON = sys.executable or "/usr/bin/python3"
BWRAP = "/usr/bin/bwrap"
PRLIMIT = "/usr/bin/prlimit"
UNSHARE = "/usr/bin/unshare"

def limits(cpu: int = 5) -> list[str]:
    """SIGKILL on `cpu` CPU-seconds. It is blind to sleep(), so the parent's
    wall clock below is the real ceiling and this is the backstop for a spin."""
    return [
        "--as=1073741824",  # 1 GiB of address space
        f"--cpu={cpu}",
        "--nofile=64",
        "--fsize=1048576",
        "--nproc=1",  # nothing in the chain forks, so this is a flat denial
        "--core=0",
    ]

DRIVER = r'''
"""Runs inside the sandbox. Reads one JSON payload on stdin, writes one JSON
object on stdout. The audit hook is belt to the sandbox's braces: under the
rlimit-only fallback it is the only filesystem guard, and it is installed after
the payload is compiled so that a syntax error is reported rather than blocked.
"""
import io, json, signal, sys, time, typing

# Annotations are evaluated at def time, so a signature written the way the
# statement writes it -- def f(nums: List[int]) -> int -- is a NameError unless
# the typing names are already in scope. Measured: every reference the model
# wrote failed on this before the names were injected.
TYPING = {n: getattr(typing, n) for n in
          ("List", "Dict", "Set", "Tuple", "Optional", "Any", "Union", "Deque",
           "Iterable", "Sequence", "Callable", "DefaultDict", "Counter")}

BLOCKED = {"socket.connect", "socket.bind", "socket.getaddrinfo",
           "socket.gethostbyname", "os.system", "subprocess.Popen", "os.exec",
           "os.fork", "os.forkpty", "os.posix_spawn", "ctypes.dlopen",
           "urllib.Request", "os.putenv", "os.remove", "os.rename",
           "os.rmdir", "os.mkdir", "os.chmod", "shutil.copyfile"}


def _guard(event, args):
    if event in BLOCKED:
        raise PermissionError("blocked: %s" % event)
    if event == "open":
        # Two callers raise this one event with different shapes: io.open
        # passes a mode string, os.open passes mode=None and O_* flags. Reading
        # only args[1] lets os.open(p, O_CREAT|O_WRONLY) straight through --
        # measured, it created the file.
        path, mode, flags = args[0], args[1], (args[2] if len(args) > 2 else 0)
        if mode and any(c in mode for c in "wxa+"):
            raise PermissionError("blocked: write to %r" % (path,))
        if isinstance(flags, int) and flags & 0x2C3:
            # O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND on Linux
            raise PermissionError("blocked: write to %r" % (path,))


def fail(msg):
    # Always the real stdout: candidate code may have replaced sys.stdout, and
    # the parent reads this line as the whole result.
    print(json.dumps({"ok": False, "error": msg}), file=sys.__stdout__)
    sys.exit(0)


payload = json.load(sys.stdin)
try:
    compiled = compile(payload["code"], "<untrusted>", "exec")
except SyntaxError as e:
    fail("SyntaxError: %s" % e)

if payload.get("entry"):
    # Call mode: define the code, bind the entry point, then run the cases.
    # A candidate who left a print() or a `if __name__` demo in the file would
    # otherwise write it into the middle of the result JSON, and the whole run
    # came back as "driver produced no JSON" -- measured, from the page.
    g = {"__name__": "__untrusted__", **TYPING}
    printed = io.StringIO()
    sys.stdout = printed
    # The hook guards module-level code too. Installed after exec, every
    # top-level statement ran unguarded -- and under the prlimit fallback the
    # hook is the only filesystem guard there is.
    sys.addaudithook(_guard)
    try:
        exec(compiled, g)
    except BaseException as e:
        fail("%s: %s" % (type(e).__name__, e))
    fn = g.get(payload["entry"])
    if fn is None and "Solution" in g:
        # LeetCode statements wrap the entry point in a class.
        try:
            fn = getattr(g["Solution"](), payload["entry"], None)
        except BaseException as e:
            fail("%s: %s" % (type(e).__name__, e))
    if not callable(fn):
        fail("no entry point %r" % (payload["entry"],))
    sys.stdin = io.StringIO("")   # a stray input() should raise, not hang

    # An exhaustive reference is exponential by construction, so one oversized
    # case would otherwise burn RLIMIT_CPU and take the whole batch down with
    # it -- measured, and it cost every result. SIGALRM bounds each call on its
    # own, so a case the oracle cannot compute is dropped and the rest survive.
    each = float(payload.get("case_seconds") or 0)

    class Overran(Exception):
        pass

    def _alarm(signum, frame):
        raise Overran()

    if each:
        signal.signal(signal.SIGALRM, _alarm)

    results = []
    for case in payload["cases"]:
        t = time.perf_counter()
        if each:
            signal.setitimer(signal.ITIMER_REAL, each)
        try:
            got = fn(**case)
            json.dumps(got)       # unserialisable answers are a failure here,
            results.append({"got": got,                 # not a crash upstream
                            "ms": round((time.perf_counter() - t) * 1000, 3)})
        except Overran:
            results.append({"error": "timeout",
                            "ms": round((time.perf_counter() - t) * 1000, 3)})
        except BaseException as e:
            results.append({"error": "%s: %s" % (type(e).__name__, e),
                            "ms": round((time.perf_counter() - t) * 1000, 3)})
        finally:
            if each:
                signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout = sys.__stdout__
    print(json.dumps({"ok": True, "results": results,
                      "stdout": printed.getvalue()[:2000]}))
else:
    # Script mode: run it as a program, with its stdin fed and stdout captured.
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    sys.stdin = io.StringIO(payload.get("stdin", ""))
    sys.addaudithook(_guard)
    t = time.perf_counter()
    try:
        exec(compiled, {"__name__": "__main__", **TYPING})
    except BaseException as e:
        sys.stdout = real
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                          "stdout": out.getvalue(),
                          "ms": round((time.perf_counter() - t) * 1000, 3)}))
        sys.exit(0)
    sys.stdout = real
    print(json.dumps({"ok": True, "stdout": out.getvalue(),
                      "ms": round((time.perf_counter() - t) * 1000, 3)}))
'''


@dataclass(frozen=True)
class Result:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    ms: float = 0.0
    mode: str = ""
    results: tuple = ()   # per-case outcomes, in call mode only


_DRIVER_DIR: Path | None = None
_MODE: str | None = None
_BWRAP_WHY: str = ""


def driver_dir() -> Path:
    """A read-only home for the driver, written once per process."""
    global _DRIVER_DIR
    if _DRIVER_DIR is None:
        d = Path(tempfile.mkdtemp(prefix="prep-sandbox-"))
        (d / "driver.py").write_text(DRIVER)
        atexit.register(shutil.rmtree, d, True)
        _DRIVER_DIR = d
    return _DRIVER_DIR


def bwrap_command(work: Path, cpu: int = 5) -> list[str]:
    """The jail we want. --clearenv must precede every --setenv or it wipes
    them; nothing is bound writable and the system python is used, so no path
    under /home is visible at all.

    RLIMIT_NPROC is left off here and only here: bwrap forks to enter its own
    pid namespace, so --nproc=1 would strangle the jail rather than the payload.
    Its own --unshare-pid does that job instead.
    """
    return [
        PRLIMIT, *[l for l in limits(cpu) if not l.startswith("--nproc")],
        BWRAP,
        "--unshare-all", "--die-with-parent", "--new-session",
        "--clearenv",
        "--setenv", "PATH", str(Path(PYTHON).parent),
        "--setenv", "LC_ALL", "C",
        "--setenv", "PYTHONHASHSEED", "0",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        # Only what exists: aarch64 Debian has no /lib64, and a bind of a
        # missing path makes bwrap refuse the whole sandbox.
        *[a for d in ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache",
                      str(Path(PYTHON).resolve().parent.parent))
          if Path(d).exists() and d != "/"
          for a in ("--ro-bind", d, d)],
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--ro-bind", str(work), "/work",
        "--chdir", "/tmp",
        PYTHON, "-I", "-S", "-B", "/work/driver.py",
    ]


def fallback_command(work: Path, cpu: int = 5) -> list[str]:
    """No filesystem isolation -- the process keeps uid 1000 -- so the driver's
    audit hook carries that half. unshare -Un is the one namespace obtainable
    unprivileged here, and it costs no uid map."""
    return [
        PRLIMIT, *limits(cpu),
        UNSHARE, "-Un",
        PYTHON, "-I", "-S", "-B", str(work / "driver.py"),
    ]


def mode() -> str:
    """"bwrap" or "prlimit", decided once by running an empty payload under
    bwrap. Cached for the process, and the refusal is kept so the reason is
    visible rather than mysterious."""
    global _MODE, _BWRAP_WHY
    if _MODE is None:
        work = driver_dir()
        try:
            p = subprocess.run(
                bwrap_command(work), input='{"code":"pass"}',
                capture_output=True, text=True, timeout=5,
                env={"LC_ALL": "C"},
            )
            if p.returncode == 0 and '"ok": true' in p.stdout:
                _MODE = "bwrap"
            else:
                _MODE, _BWRAP_WHY = "prlimit", (p.stderr.strip() or "no output")[:200]
        except (OSError, subprocess.SubprocessError) as e:
            _MODE, _BWRAP_WHY = "prlimit", f"{type(e).__name__}: {e}"
    return _MODE


def why_not_bwrap() -> str:
    mode()
    return _BWRAP_WHY


def _execute(payload: dict, timeout: float) -> Result:
    cpu = max(2, int(timeout) + 2)
    work = driver_dir()
    cmd = bwrap_command(work, cpu) if mode() == "bwrap" else fallback_command(work, cpu)
    # A throwaway cwd, because the fallback can still see the real filesystem.
    with tempfile.TemporaryDirectory(prefix="prep-run-") as cwd:
        started = time.perf_counter()
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=cwd,
            env={"LC_ALL": "C"},
            start_new_session=True,  # so the whole group can be killed at once
        )
        try:
            out, err = p.communicate(json.dumps(payload), timeout=timeout)
        except subprocess.TimeoutExpired:
            # RLIMIT_CPU does not see sleep(); the wall clock does.
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return Result(False, error="timeout",
                          ms=round((time.perf_counter() - started) * 1000, 1),
                          mode=mode())
        ms = round((time.perf_counter() - started) * 1000, 1)

    if p.returncode != 0:
        # -9 is RLIMIT_CPU or the OOM path; the stderr says which.
        return Result(False, stderr=err.strip()[:400], mode=mode(), ms=ms,
                      error=f"exit {p.returncode}: {err.strip()[:200]}")
    try:
        parsed = json.loads(out)
    except ValueError:
        return Result(False, stdout=out[:400], stderr=err.strip()[:400],
                      error="driver produced no JSON", ms=ms, mode=mode())
    return Result(bool(parsed.get("ok")), stdout=parsed.get("stdout", ""),
                  stderr=err.strip()[:400], error=parsed.get("error"),
                  ms=ms, mode=mode(),
                  results=tuple(parsed.get("results", ())))


def run_python(code: str, stdin: str = "", timeout: float = 10.0) -> Result:
    """Run a program, feed it stdin, hand back what it printed."""
    return _execute({"code": code, "stdin": stdin}, timeout)


def run_cases(code: str, entry: str, cases: list[dict], timeout: float = 15.0,
              case_seconds: float = 0.0) -> dict:
    """Call one function over many argument dicts in a single process.

    Returns {"ok": True, "results": [{"got"|"error", "ms"}, ...]} or
    {"ok": False, "error": ...}. A whole suite costs one process, which is why
    the 8 ms of sandbox overhead never shows up.
    """
    r = _execute({"code": code, "entry": entry, "cases": cases,
                  "case_seconds": case_seconds}, timeout)
    if not r.ok:
        return {"ok": False, "error": r.error or "failed", "mode": r.mode}
    return {"ok": True, "results": list(r.results), "mode": r.mode, "ms": r.ms}
