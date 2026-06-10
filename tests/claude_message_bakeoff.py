#!/usr/bin/env python3
"""
claude_message_bakeoff.py — empirically compare ways to CAPTURE claude-code's
reply text after sending it a message in a live interactive `claude` tab, and
pick the best by KPI. The claude analog of hermes_message_bakeoff.py.

"Is the turn done?" is already solved for claude by the fork's FIFO side channel:
claude's own hooks (~/.claude/settings.json) write "working" on UserPromptSubmit
and "idle" on Stop/StopFailure to $ITERM_AGENT_STATE_FIFO. This harness REPLICATES
that channel (mkfifo + the env var, exactly as PseudoTerminal.m's
iTermAgentStateChannel does) and REUSES the working->idle edge as the "reply
complete" trigger, then asks the next question: once the turn is done, what did
claude actually SAY, and which capture method recovers it most reliably?

The send side is identical for every method: inject the prompt into the live pty
(plain text + CR; multi-line via bracketed paste + CR — the same incantation the
shipped feature uses). The methods differ only in how they capture the reply:

  TRANSCRIPT — after the idle edge, read claude's own JSONL transcript under
               ~/.claude/projects/<munged-cwd>/<uuid>.jsonl: find the user line
               whose string content == what we sent, then concatenate the
               following assistant text blocks.                  (hypothesis: best)
  SCRAPE     — render the pty byte window [send..idle] through a terminal emulator
               (pyte) and read the display grid, minus the Ink TUI chrome.
  STREAM     — strip ANSI/control from the raw pty byte window and de-chrome it.
  ONESHOT    — reference, NOT the live tab: spawn `claude -p <prompt>` fresh.

Oracle: the transcript is claude's own canonical, ANSI-free record of what it
said, so TRANSCRIPT is ~= the oracle by construction — stated plainly. Scenarios
C1/C4/C5b/C6 also use KNOWN-ANSWER prompts whose expected reply we control: an
oracle-INDEPENDENT fidelity check every method (TRANSCRIPT included) must pass.

A submit that claude accepts fires UserPromptSubmit -> "working" on the FIFO, so a
missing "working" after a send is a positive signal that the submit incantation
did not take — which is exactly the claude-specific unknown --probe de-risks.

Usage:
  <py-with-pyte> tests/claude_message_bakeoff.py --probe
  <py-with-pyte> tests/claude_message_bakeoff.py --corpus --json tmp/claude-msg-bakeoff.json

pyte is optional; without it SCRAPE reports "unavailable" (STREAM still runs).
"""

import argparse
import difflib
import fcntl
import glob
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime

PROJECTS = os.path.expanduser("~/.claude/projects")

try:
    import pyte  # optional; SCRAPE degrades to "unavailable" without it
except Exception:
    pyte = None

# ANSI / control strippers for STREAM.
CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ESC_RE = re.compile(rb"\x1b[@-Z\\-_]")
# Box-drawing + prompt chrome claude's Ink TUI paints around the input/output.
CHROME_GLYPHS = ("│", "─", "╭", "╮", "╰", "╯", "▌", "▐", "●", "✻", "✽", "·", "⏵", "⎿", ">")
SPINNER_WORDS = ("esc to interrupt", "tokens", "Context left", "? for shortcuts",
                 "Welcome back", "cwd:", "Bypassing Permissions")

# A 12-line verbatim-echo block (C4): distinct, plain, no markdown.
C4_BLOCK = "\n".join("echo-line-%02d alpha bravo charlie" % i for i in range(1, 13))


def now():
    return time.time()


# ----------------------------- live claude under a pty -----------------------------

class ClaudeSession:
    """Runs `claude` (interactive) under a pty, with a private FIFO side channel
    fed by claude's working/idle hooks — the same mechanism the fork's claude tab
    uses. Records the raw byte stream (for SCRAPE/STREAM) and the FIFO state
    transitions (for the turn-complete trigger)."""

    def __init__(self, claude_path, cwd, rows=40, cols=120):
        self.claude_path = claude_path
        self.cwd = cwd
        self.rows = rows
        self.cols = cols
        self.pid = None
        self.master_fd = None
        self.chunks = []        # (timestamp, bytes) from the pty, append-only
        self.fifo_events = []   # (timestamp, "working"|"idle") from the FIFO
        self._fifo_dir = None
        self._fifo_path = None
        self._fifo_fd = -1
        self._fifo_buf = b""
        self._dead = False

    def start(self):
        # Private FIFO, mirroring iTermAgentStateChannel: O_RDWR|O_NONBLOCK so the
        # hooks' writes never block on "no reader" and we never see a spurious EOF.
        unique = "%d-%d" % (os.getpid(), int(now() * 1000) % 1000000)
        self._fifo_dir = os.path.join("/tmp", "iterm-agent-bakeoff-" + unique)
        os.makedirs(self._fifo_dir, mode=0o700, exist_ok=True)
        self._fifo_path = os.path.join(self._fifo_dir, "state")
        os.mkfifo(self._fifo_path, 0o600)
        self._fifo_fd = os.open(self._fifo_path, os.O_RDWR | os.O_NONBLOCK)

        env = dict(os.environ)
        env["TERM_PROGRAM"] = "iTerm.app"
        env["TERM"] = "xterm-256color"
        env["ITERM_AGENT_STATE_FIFO"] = self._fifo_path
        env["LINES"] = str(self.rows)
        env["COLUMNS"] = str(self.cols)

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                os.environ.clear()
                os.environ.update(env)
            except Exception:
                pass
            # Interactive claude — NO -p. Runs in a trusted cwd so no trust dialog.
            try:
                os.execvp(self.claude_path, [self.claude_path])
            except Exception:
                os._exit(127)
        self.pid = pid
        self.master_fd = master_fd
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except Exception:
            pass

    def pump(self, seconds, until=None):
        """Read the pty AND the FIFO for up to `seconds`; return early once
        `until` (a callable -> bool) is true."""
        deadline = now() + seconds
        while now() < deadline:
            if until is not None and until():
                return True
            fds = [self.master_fd, self._fifo_fd]
            try:
                r, _, _ = select.select(fds, [], [], 0.1)
            except (OSError, ValueError):
                self._dead = True
                return False
            if self._fifo_fd in r:
                self._drain_fifo()
            if self.master_fd in r:
                try:
                    data = os.read(self.master_fd, 65536)
                except OSError:
                    self._dead = True
                    return False
                if not data:
                    self._dead = True
                    return False
                self.chunks.append((now(), data))
        return until() if until is not None else False

    def _drain_fifo(self):
        try:
            while True:
                buf = os.read(self._fifo_fd, 256)
                if not buf:
                    break
                self._fifo_buf += buf
        except OSError:
            pass  # EAGAIN — drained
        while b"\n" in self._fifo_buf:
            line, self._fifo_buf = self._fifo_buf.split(b"\n", 1)
            s = line.decode("utf-8", "replace").strip()
            if s in ("working", "idle"):
                self.fifo_events.append((now(), s))

    def submit(self, message, delay=0.0):
        """Inject a message exactly as the shipped feature will: bracketed paste
        for multi-line (so embedded newlines are literal), else plain; then CR to
        submit. `delay` inserts a gap before the CR if the TUI needs it."""
        if "\n" in message:
            body = "\x1b[200~" + message + "\x1b[201~"
        else:
            body = message
        self._write(body)
        if delay > 0:
            time.sleep(delay)
        self._write("\r")

    def nudge_enter(self):
        self._write("\r")

    def _write(self, text):
        try:
            os.write(self.master_fd, text.encode("utf-8"))
        except OSError:
            self._dead = True

    def all_bytes(self):
        return b"".join(d for _, d in self.chunks)

    def stop(self):
        for attempt in (b"\x03", b"\x03", b"\x04"):
            if self._exited():
                break
            try:
                os.write(self.master_fd, attempt)
            except OSError:
                break
            self.pump(2.0, until=self._exited)
        if not self._exited():
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
            self.pump(2.0, until=self._exited)
        if not self._exited():
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
        for fn, arg in ((os.waitpid, (self.pid, os.WNOHANG)),
                        (os.close, (self.master_fd,)),
                        (os.close, (self._fifo_fd,)),
                        (os.unlink, (self._fifo_path,)),
                        (os.rmdir, (self._fifo_dir,))):
            try:
                fn(*arg)
            except OSError:
                pass

    def _exited(self):
        try:
            wpid, _ = os.waitpid(self.pid, os.WNOHANG)
            return wpid == self.pid
        except OSError:
            return True


# ----------------------------- TRANSCRIPT (read-only JSONL) -----------------------------
# This mirrors, line for line, what iTermClaudeMessenger will do in ObjC.

def _iso_to_epoch(ts):
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def _recent_transcripts(since_epoch, slack=5.0):
    """project-dir/<uuid>.jsonl files touched at/after send time, newest first.
    The single-level glob excludes subagent transcripts (one dir deeper)."""
    cands = []
    for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt >= since_epoch - slack:
            cands.append((mt, p))
    cands.sort(reverse=True)
    return [p for _, p in cands]


def _scan_transcript(path, message, min_epoch):
    """In one transcript, find the (most recent) genuine user prompt whose string
    content == message, then concatenate the assistant text blocks that follow it
    until the next genuine (string-content) user prompt. Tool-result user lines
    (array content) and thinking/tool_use blocks are skipped."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    found = None
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if not isinstance(c, str) or c != message:
            continue
        ts = _iso_to_epoch(o.get("timestamp"))
        if ts is not None and ts < min_epoch:
            continue
        found = i  # keep the last match
    if found is None:
        return ""
    parts = []
    for line in lines[found + 1:]:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        c = (o.get("message") or {}).get("content")
        if t == "user":
            if isinstance(c, str):
                break  # next genuine prompt — turn boundary
            continue   # tool-result — keep scanning
        if t == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    txt = b.get("text") or ""
                    if txt:
                        parts.append(txt)
    return "\n".join(parts).strip()


def read_reply_transcript(message, since_epoch, slack=5.0):
    for path in _recent_transcripts(since_epoch, slack):
        reply = _scan_transcript(path, message, since_epoch - slack)
        if reply:
            return reply
    return ""


def capture_transcript(message, since_epoch, timeout=15.0):
    deadline = now() + timeout
    while now() < deadline:
        txt = read_reply_transcript(message, since_epoch)
        if txt.strip():
            return txt
        time.sleep(0.25)
    return read_reply_transcript(message, since_epoch)


# ----------------------------- terminal-scrape methods -----------------------------

def _dechrome(text, prompt_text):
    """Drop Ink box-drawing/chrome lines, the echoed prompt, spinner/status, and
    collapse blank runs."""
    prompt_lines = set(l.strip() for l in (prompt_text or "").splitlines() if l.strip())
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            if out and out[-1] == "":
                continue
            out.append("")
            continue
        if s in prompt_lines:
            continue
        if any(w in s for w in SPINNER_WORDS):
            continue
        stripped = s.lstrip("".join(CHROME_GLYPHS) + " ")
        if not stripped:
            continue  # pure chrome line
        out.append(stripped)
    return "\n".join(out).strip("\n")


def capture_scrape(window_bytes, cols, prompt_text):
    if pyte is None:
        return None
    screen = pyte.Screen(cols, 4000)
    stream = pyte.ByteStream(screen)
    try:
        stream.feed(window_bytes)
    except Exception:
        pass
    text = "\n".join(l.rstrip() for l in screen.display)
    return _dechrome(text, prompt_text)


def capture_stream(window_bytes, prompt_text):
    b = OSC_RE.sub(b"", window_bytes)
    b = CSI_RE.sub(b"", b)
    b = ESC_RE.sub(b"", b)
    text = b.decode("utf-8", "replace").replace("\r", "\n")
    return _dechrome(text, prompt_text)


def capture_oneshot(claude_path, prompt, cwd, timeout=180.0):
    env = dict(os.environ)
    env["TERM_PROGRAM"] = "iTerm.app"
    try:
        p = subprocess.run([claude_path, "-p", prompt],
                           capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=cwd)
        return (p.stdout or "").strip()
    except Exception:
        return None


# ----------------------------- scoring -----------------------------

def norm(s):
    if s is None:
        return None
    s = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)
    s = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines())
    return re.sub(r"\n{2,}", "\n", s).strip()


def ratio(a, b):
    a, b = norm(a) or "", norm(b) or ""
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def contains_known(captured, known):
    c, k = norm(captured) or "", norm(known) or ""
    return k != "" and k in c


# ----------------------------- one live turn -----------------------------

def run_turn(sess, scn, cols, claude_path, first_turn=False):
    prompt = scn["prompt"]
    settle = scn.get("settle", 120.0)
    submit_wait = 30.0 if first_turn else 18.0

    t_send = now()
    sess.submit(prompt, delay=scn.get("submit_delay", 0.0))

    def saw_working():
        return any(s == "working" for (t, s) in sess.fifo_events if t >= t_send - 0.1)

    sess.pump(submit_wait, until=saw_working)
    submit_ok = saw_working()
    if not submit_ok:
        # The submit may not have registered; nudge a lone Enter once.
        sess.nudge_enter()
        sess.pump(submit_wait, until=saw_working)
        submit_ok = saw_working()

    def done():
        ev = [s for (t, s) in sess.fifo_events if t >= t_send - 0.1]
        return ev.count("working") >= 1 and ev[-1:] == ["idle"]

    sess.pump(settle, until=done)
    sess.pump(1.0)

    ev = [(t, s) for (t, s) in sess.fifo_events if t >= t_send - 0.1]
    t_idle = next((t for t, s in reversed(ev) if s == "idle"), now())
    detect_latency = round(t_idle - t_send, 2)
    window = b"".join(d for (t, d) in sess.chunks if t_send - 0.05 <= t <= t_idle + 0.2)

    caps = {}
    t0 = now(); caps["TRANSCRIPT"] = (capture_transcript(prompt, t_send), round(now() - t0, 2))
    t0 = now(); caps["SCRAPE"] = (capture_scrape(window, cols, prompt), round(now() - t0, 2))
    t0 = now(); caps["STREAM"] = (capture_stream(window, prompt), round(now() - t0, 2))
    if scn.get("oneshot"):
        t0 = now(); caps["ONESHOT"] = (capture_oneshot(claude_path, prompt, sess.cwd), round(now() - t0, 2))

    oracle = caps["TRANSCRIPT"][0]
    known = scn.get("known")
    methods = {}
    for name, (text, cap_lat) in caps.items():
        if text is None:
            methods[name] = {"available": False}
            continue
        fidelity = None if (name == "ONESHOT" and not known) else round(ratio(text, oracle), 3)
        completeness = (round(min(1.0, (len(norm(text)) / len(norm(oracle)))), 3)
                        if oracle and norm(oracle) and name != "ONESHOT" else None)
        methods[name] = {
            "available": True,
            "chars": len(norm(text) or ""),
            "fidelity_vs_oracle": fidelity,
            "completeness": completeness,
            "known_match": contains_known(text, known) if known else None,
            "capture_latency_s": cap_lat,
            "sample": (norm(text) or "")[:80],
        }
    return {
        "id": scn["id"], "desc": scn["desc"],
        "submit_ok": submit_ok,
        "detect_latency_s": detect_latency,
        "oracle_chars": len(norm(oracle) or ""),
        "known": known if (known and len(known) < 40) else (known[:37] + "..." if known else None),
        "methods": methods,
    }


# ----------------------------- corpus -----------------------------

CORPUS = [
    dict(id="C1", desc="short known-answer", oneshot=True, settle=120,
         prompt="Reply with exactly the token ZQ7-DELTA and nothing else — no quotes, no punctuation.",
         known="ZQ7-DELTA"),
    dict(id="C2", desc="long streamed reply (~400 words)", oneshot=False, settle=180,
         prompt="Write about 400 words of plain prose on the history of terminal emulators. "
                "No headings, no bullet lists.",
         known=None),
    dict(id="C3", desc="fenced code block w/ ANSI content", oneshot=False, settle=150,
         prompt="Output only a fenced bash code block containing a script that prints the word "
                "RED in red using ANSI escape codes. No text before or after the code block.",
         known=None),
    dict(id="C4", desc="12-line verbatim echo (known-answer)", oneshot=True, settle=150,
         prompt="Repeat the following 12 lines back to me verbatim — nothing before or after, "
                "no code fence:\n" + C4_BLOCK,
         known=C4_BLOCK),
    dict(id="C5a", desc="context setup", oneshot=False, settle=120,
         prompt="Remember this code word for later: PURPLE-WALRUS-42. Reply with only: OK",
         known="OK"),
    dict(id="C5b", desc="context recall (live-tab proof)", oneshot=True, settle=120,
         prompt="What was the code word I told you to remember? Reply with only the code word.",
         known="PURPLE-WALRUS-42"),
    dict(id="C6a", desc="rapid turn A (turn-binding)", oneshot=False, settle=120,
         prompt="Reply with only: ALPHA-ONE", known="ALPHA-ONE"),
    dict(id="C6b", desc="rapid turn B (turn-binding)", oneshot=False, settle=120,
         prompt="Reply with only: BETA-TWO", known="BETA-TWO"),
]


def setup_session(claude_path, cwd, cols=120, rows=40):
    sess = ClaudeSession(claude_path, cwd, rows=rows, cols=cols)
    sess.start()
    print("launched pid", sess.pid, "in", cwd, "— booting claude (FIFO:", sess._fifo_path, ")")
    # No FIFO line on startup; give the TUI a moment to reach its input prompt.
    sess.pump(8.0)
    return sess


def print_turn(r):
    print("\n--- %s: %s  (submit_ok=%s, detect %.2fs, oracle %d chars) ---"
          % (r["id"], r["desc"], r["submit_ok"], r["detect_latency_s"], r["oracle_chars"]))
    if r["known"]:
        print("    known-answer: %r" % r["known"])
    print("    %-10s avail fid    compl  known  cap(s)  sample" % "method")
    for name in ("TRANSCRIPT", "SCRAPE", "STREAM", "ONESHOT"):
        m = r["methods"].get(name)
        if not m:
            continue
        if not m.get("available"):
            print("    %-10s  no   (unavailable)" % name)
            continue
        print("    %-10s  yes  %-6s %-6s %-6s %-6s  %s" % (
            name, m["fidelity_vs_oracle"], m["completeness"],
            m["known_match"], m["capture_latency_s"], m["sample"]))


def run_probe(claude_path, cwd):
    print("== PROBE: one round-trip (C1) ==")
    sess = setup_session(claude_path, cwd)
    r = run_turn(sess, CORPUS[0], 120, claude_path, first_turn=True)
    sess.stop()
    print_turn(r)
    tr = r["methods"].get("TRANSCRIPT", {})
    ok = r["submit_ok"] and tr.get("available") and tr.get("known_match")
    print("\n== VERDICT ==")
    print("  submit incantation accepted (UserPromptSubmit fired):", r["submit_ok"])
    print("  TRANSCRIPT recovered the reply and matched known-answer:", bool(ok))
    return 0 if ok else 2


def run_corpus(claude_path, cwd, json_path):
    print("== CORPUS C1-C6 (single live session) ==")
    sess = setup_session(claude_path, cwd)
    results = []
    for i, scn in enumerate(CORPUS):
        r = run_turn(sess, scn, 120, claude_path, first_turn=(i == 0))
        results.append(r)
        print_turn(r)
    sess.stop()

    agg = {}
    for name in ("TRANSCRIPT", "SCRAPE", "STREAM", "ONESHOT"):
        fid, comp, known_ok, known_n, cap = [], [], 0, 0, []
        for r in results:
            m = r["methods"].get(name)
            if not m or not m.get("available"):
                continue
            if m["fidelity_vs_oracle"] is not None:
                fid.append(m["fidelity_vs_oracle"])
            if m["completeness"] is not None:
                comp.append(m["completeness"])
            if m["known_match"] is not None:
                known_n += 1
                known_ok += 1 if m["known_match"] else 0
            cap.append(m["capture_latency_s"])
        agg[name] = {
            "mean_fidelity": round(sum(fid) / len(fid), 3) if fid else None,
            "mean_completeness": round(sum(comp) / len(comp), 3) if comp else None,
            "known_match_rate": ("%d/%d" % (known_ok, known_n)) if known_n else None,
            "mean_capture_latency_s": round(sum(cap) / len(cap), 3) if cap else None,
        }
    print("\n== AGGREGATE ==")
    print("    %-10s  mean-fid  mean-compl  known-match  mean-cap(s)" % "method")
    for name in ("TRANSCRIPT", "SCRAPE", "STREAM", "ONESHOT"):
        a = agg[name]
        print("    %-10s  %-8s  %-10s  %-11s  %s" % (
            name, a["mean_fidelity"], a["mean_completeness"],
            a["known_match_rate"], a["mean_capture_latency_s"]))
    submit_fails = [r["id"] for r in results if not r["submit_ok"]]
    print("\n  submit accepted on every turn:", not submit_fails,
          ("" if not submit_fails else "(failed: %s)" % submit_fails))

    if json_path:
        try:
            os.makedirs(os.path.dirname(json_path), exist_ok=True)
        except Exception:
            pass
        with open(json_path, "w") as f:
            json.dump({"results": results, "aggregate": agg, "pyte": pyte is not None}, f, indent=2)
        print("\nwrote", json_path)
    return 0


def find_claude():
    for cand in (os.path.expanduser("~/.local/bin/claude"),
                 "/usr/local/bin/claude", "claude"):
        if cand == "claude" or os.path.exists(cand):
            return cand
    return "claude"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="one round-trip (C1); de-risk")
    ap.add_argument("--corpus", action="store_true", help="full C1-C6 corpus")
    ap.add_argument("--claude", default=None, help="path to claude launcher")
    ap.add_argument("--cwd", default=None,
                    help="working dir to launch claude in (must be trusted to skip the trust dialog)")
    ap.add_argument("--json", default=None, help="write results JSON here")
    args = ap.parse_args()

    claude_path = args.claude or find_claude()
    # Default to a trusted dir so claude boots straight to the prompt (no trust dialog).
    cwd = args.cwd or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("claude:", claude_path)
    print("cwd:", cwd)
    print("projects root:", PROJECTS, "(exists:", os.path.isdir(PROJECTS), ")")
    print("pyte (SCRAPE) available:", pyte is not None)

    if args.corpus:
        sys.exit(run_corpus(claude_path, cwd, args.json))
    sys.exit(run_probe(claude_path, cwd))


if __name__ == "__main__":
    main()
