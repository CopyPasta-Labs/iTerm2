#!/usr/bin/env python3
"""
hermes_message_bakeoff.py — empirically compare ways to CAPTURE hermes's reply
text after sending it a message in a live `hermes --cli` tab, and pick the best
by KPI.

Companion to hermes_state_bakeoff.py (which decided how to detect working/idle).
That question — "is the turn done?" — is already solved by the in-band
`OSC 1337 ; HermesState=working|idle` edge (scored 100%/0fp/0fn there). This
harness REUSES that edge as the "reply complete" trigger and asks the next
question: once the turn is done, what did hermes actually SAY, and which capture
method recovers it most reliably?

The send side is identical for every method: write the prompt to the pty. The
methods differ only in how they capture the reply:

  STATEDB  — after the idle edge, read role='assistant' rows appended to
             ~/.hermes/state.db since a pre-send MAX(id) watermark.   (hypothesis: best)
  SCRAPE   — render the pty byte window [send..idle] through a terminal emulator
             (pyte) and read the display grid, minus prompt/echo chrome.
  STREAM   — strip ANSI/control from the raw pty byte window and de-chrome it.
  ONESHOT  — reference, NOT the live tab: spawn `hermes chat -q <p> -Q` fresh.

Oracle: state.db is hermes's own canonical record of what it said, so STATEDB is
≈ the oracle by construction — stated plainly. Scenarios M1/M4/M5b/M6 also use
KNOWN-ANSWER prompts whose expected reply we control: an oracle-INDEPENDENT
fidelity check every method (STATEDB included) must pass.

Usage:
  <py-with-pyte> tests/hermes_message_bakeoff.py --probe
  <py-with-pyte> tests/hermes_message_bakeoff.py --corpus --json tmp/msg-bakeoff.json

pyte is optional; without it SCRAPE reports "unavailable" (STREAM still runs).
"""

import argparse
import difflib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

# Reuse the proven pty harness + OSC machinery from the state bake-off.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hermes_state_bakeoff import HermesSession, find_hermes, now  # noqa: E402

try:
    import pyte  # optional; SCRAPE degrades to "unavailable" without it
except Exception:
    pyte = None

DB = os.path.expanduser("~/.hermes/state.db")

# ANSI / control strippers for STREAM.
CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ESC_RE = re.compile(rb"\x1b[@-Z\\-_]")
PROMPT_GLYPHS = ("❯", "⚕", "⚠", "◉", "●", "🔐", "🔑", "✎", "?")

# A 12-line verbatim-echo block (M4): distinct, plain, no markdown.
M4_BLOCK = "\n".join("echo-line-%02d alpha bravo charlie" % i for i in range(1, 13))


# ----------------------------- state.db (read-only) -----------------------------

def _connect_ro():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5.0)


def find_live_session(launch_time, tol=8.0, verbose=False):
    """The session a freshly-launched hermes tab created: the earliest cli
    session whose started_at is at/after we launched it. The row is written
    lazily (on first message), so call this after the first turn completes."""
    con = _connect_ro()
    try:
        row = con.execute(
            "select id from sessions where source='cli' and started_at >= ? "
            "order by started_at asc limit 1", (launch_time - tol,)).fetchone()
        if row:
            return row[0]
        if verbose:
            print("  [no cli session >= launch; recent sessions:]")
            for r in con.execute("select id, source, started_at from sessions "
                                 "order by started_at desc limit 5").fetchall():
                print("    ", r, "(delta %.1fs)" % (r[2] - launch_time))
        return None
    finally:
        con.close()


def max_msg_id(session_id):
    con = _connect_ro()
    try:
        return con.execute("select coalesce(max(id),0) from messages where session_id=?",
                           (session_id,)).fetchone()[0]
    finally:
        con.close()


def assistant_since(session_id, watermark):
    """Concatenate non-empty assistant message text appended since the watermark
    (zero-length assistant rows are tool-call turns and are skipped)."""
    con = _connect_ro()
    try:
        rows = con.execute(
            "select content from messages where session_id=? and id>? "
            "and role='assistant' and content is not null and length(content)>0 "
            "order by id", (session_id, watermark)).fetchall()
        return "\n".join(r[0] for r in rows)
    finally:
        con.close()


# ----------------------------- capture methods -----------------------------

def _dechrome(text, prompt_text):
    """Drop prompt-symbol lines, the echoed prompt, and blank runs."""
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
        if len(s) <= 2 and any(s.startswith(g) for g in PROMPT_GLYPHS):
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


def capture_statedb(session_id, watermark, timeout=12.0):
    """Poll state.db until the assistant reply for this turn lands."""
    deadline = now() + timeout
    while now() < deadline:
        txt = assistant_since(session_id, watermark)
        if txt.strip():
            return txt
        time.sleep(0.2)
    return ""


def capture_scrape(window_bytes, cols, prompt_text):
    if pyte is None:
        return None
    screen = pyte.Screen(cols, 2000)
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


def capture_oneshot(hermes_path, prompt, timeout=180.0):
    env = dict(os.environ)
    env["TERM_PROGRAM"] = "iTerm.app"
    try:
        p = subprocess.run([hermes_path, "chat", "-q", prompt, "-Q"],
                           capture_output=True, text=True, timeout=timeout, env=env)
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
    """Known-answer check: the expected string appears in the captured reply
    (normalized), tolerant of surrounding chrome the method failed to strip."""
    c, k = norm(captured) or "", norm(known) or ""
    return k != "" and k in c


# ----------------------------- one live turn -----------------------------

def run_turn(sess, sid_box, launch_time, scn, cols, hermes_path):
    prompt = scn["prompt"]
    settle = scn.get("settle", 90.0)
    session_id = sid_box[0]
    # The session row is created lazily on first message; for the first turn the
    # watermark is 0 (brand-new session), thereafter MAX(id) before this send.
    watermark = max_msg_id(session_id) if session_id else 0
    t_send = now()
    sess.send(prompt + "\r")

    def done():
        ev = [s for (t, s) in sess.osc_events if t >= t_send - 0.2]
        return ev.count("working") >= 1 and ev[-1:] == ["idle"]

    sess.pump(settle, until=done)
    sess.pump(1.0)

    # Bind the state.db session once it exists (after the first turn persists).
    if session_id is None:
        session_id = find_live_session(launch_time, verbose=True)
        sid_box[0] = session_id
        print("  bound state.db session:", session_id)

    ev = [(t, s) for (t, s) in sess.osc_events if t >= t_send - 0.2]
    t_idle = next((t for t, s in reversed(ev) if s == "idle"), now())
    detect_latency = round(t_idle - t_send, 2)
    window = b"".join(d for (t, d) in sess.chunks if t_send - 0.05 <= t <= t_idle + 0.2)

    # capture (time each)
    caps = {}
    t0 = now(); caps["STATEDB"] = (capture_statedb(session_id, watermark), round(now() - t0, 2))
    t0 = now(); caps["SCRAPE"] = (capture_scrape(window, cols, prompt), round(now() - t0, 2))
    t0 = now(); caps["STREAM"] = (capture_stream(window, prompt), round(now() - t0, 2))
    if scn.get("oneshot"):
        t0 = now(); caps["ONESHOT"] = (capture_oneshot(hermes_path, prompt), round(now() - t0, 2))

    oracle = caps["STATEDB"][0]
    known = scn.get("known")
    methods = {}
    for name, (text, cap_lat) in caps.items():
        if text is None:
            methods[name] = {"available": False}
            continue
        # ONESHOT runs in a fresh session, so free-form oracle comparison is unfair;
        # it is only meaningfully scored on known-answer prompts.
        fidelity = None if (name == "ONESHOT" and not known) else round(ratio(text, oracle), 3)
        completeness = (round(min(1.0, (len(norm(text)) / len(norm(oracle))), ), 3)
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
        "detect_latency_s": detect_latency,
        "oracle_chars": len(norm(oracle) or ""),
        "known": known if (known and len(known) < 40) else (known[:37] + "..." if known else None),
        "methods": methods,
    }


# ----------------------------- corpus -----------------------------

CORPUS = [
    dict(id="M1", desc="short known-answer", oneshot=True, settle=120,
         prompt="Reply with exactly the token ZQ7-DELTA and nothing else — no quotes, no punctuation.",
         known="ZQ7-DELTA"),
    dict(id="M2", desc="long streamed reply (~400 words)", oneshot=False, settle=180,
         prompt="Write about 400 words of plain prose on the history of terminal emulators. "
                "No headings, no bullet lists.",
         known=None),
    dict(id="M3", desc="fenced code block w/ ANSI content", oneshot=False, settle=150,
         prompt="Output only a fenced bash code block containing a script that prints the word "
                "RED in red using ANSI escape codes. No text before or after the code block.",
         known=None),
    dict(id="M4", desc="12-line verbatim echo (known-answer)", oneshot=True, settle=150,
         prompt="Repeat the following 12 lines back to me verbatim — nothing before or after, "
                "no code fence:\n" + M4_BLOCK,
         known=M4_BLOCK),
    dict(id="M5a", desc="context setup", oneshot=False, settle=120,
         prompt="Remember this code word for later: PURPLE-WALRUS-42. Reply with only: OK",
         known="OK"),
    dict(id="M5b", desc="context recall (live-tab proof)", oneshot=True, settle=120,
         prompt="What was the code word I told you to remember? Reply with only the code word.",
         known="PURPLE-WALRUS-42"),
    dict(id="M6a", desc="rapid turn A (turn-binding)", oneshot=False, settle=120,
         prompt="Reply with only: ALPHA-ONE", known="ALPHA-ONE"),
    dict(id="M6b", desc="rapid turn B (turn-binding)", oneshot=False, settle=120,
         prompt="Reply with only: BETA-TWO", known="BETA-TWO"),
]


def setup_session(hermes_path, cols=120, rows=40):
    gt_log = os.path.join(os.path.expanduser("~/.hermes"), "hermes-state-debug.log")
    sess = HermesSession(hermes_path, gt_log, rows=rows, cols=cols)
    launch_time = now()
    sess.start()
    print("launched pid", sess.pid, "— waiting for startup idle...")

    def ready():
        return len(sess.osc_events) >= 1
    sess.pump(75.0, until=ready)
    if not ready():
        print("FAIL: hermes never signalled ready (no OSC) within 75s")
        sess.stop()
        return None, None
    # state.db binding is deferred until after the first turn (lazy row creation).
    return sess, launch_time


def print_turn(r):
    print("\n--- %s: %s  (detect %.2fs, oracle %d chars) ---"
          % (r["id"], r["desc"], r["detect_latency_s"], r["oracle_chars"]))
    if r["known"]:
        print("    known-answer: %r" % r["known"])
    hdr = "    %-8s avail fid    compl  known  cap(s)  sample"
    print(hdr % "method")
    for name in ("STATEDB", "SCRAPE", "STREAM", "ONESHOT"):
        m = r["methods"].get(name)
        if not m:
            continue
        if not m.get("available"):
            print("    %-8s  no   (unavailable)" % name)
            continue
        print("    %-8s  yes  %-6s %-6s %-6s %-6s  %s" % (
            name,
            m["fidelity_vs_oracle"], m["completeness"],
            m["known_match"], m["capture_latency_s"], m["sample"]))


def run_probe(hermes_path):
    print("== PROBE: one round-trip (M1) ==")
    sess, launch_time = setup_session(hermes_path)
    if not sess:
        return 1
    sid_box = [None]
    r = run_turn(sess, sid_box, launch_time, CORPUS[0], 120, hermes_path)
    sess.stop()
    print_turn(r)
    db = r["methods"].get("STATEDB", {})
    ok = db.get("available") and db.get("known_match")
    print("\n== VERDICT ==")
    print("  STATEDB recovered the reply and matched known-answer:", bool(ok))
    return 0 if ok else 2


def run_corpus(hermes_path, json_path):
    print("== CORPUS M1-M6 (single live session) ==")
    sess, launch_time = setup_session(hermes_path)
    if not sess:
        return 1
    sid_box = [None]
    results = []
    for scn in CORPUS:
        r = run_turn(sess, sid_box, launch_time, scn, 120, hermes_path)
        results.append(r)
        print_turn(r)
    sess.stop()

    # aggregate: mean fidelity / completeness / known-match per method
    agg = {}
    for name in ("STATEDB", "SCRAPE", "STREAM", "ONESHOT"):
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
    print("    %-8s  mean-fid  mean-compl  known-match  mean-cap(s)" % "method")
    for name in ("STATEDB", "SCRAPE", "STREAM", "ONESHOT"):
        a = agg[name]
        print("    %-8s  %-8s  %-10s  %-11s  %s" % (
            name, a["mean_fidelity"], a["mean_completeness"],
            a["known_match_rate"], a["mean_capture_latency_s"]))

    if json_path:
        try:
            os.makedirs(os.path.dirname(json_path), exist_ok=True)
        except Exception:
            pass
        with open(json_path, "w") as f:
            json.dump({"results": results, "aggregate": agg, "pyte": pyte is not None}, f, indent=2)
        print("\nwrote", json_path)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="one round-trip (M1); de-risk")
    ap.add_argument("--corpus", action="store_true", help="full M1-M6 corpus")
    ap.add_argument("--hermes", default=None, help="path to hermes launcher")
    ap.add_argument("--json", default=None, help="write results JSON here")
    args = ap.parse_args()

    hermes_path = args.hermes or find_hermes()
    print("hermes:", hermes_path)
    print("state.db:", DB, "(exists:", os.path.exists(DB), ")")
    print("pyte (SCRAPE) available:", pyte is not None)

    if args.corpus:
        sys.exit(run_corpus(hermes_path, args.json))
    sys.exit(run_probe(hermes_path))


if __name__ == "__main__":
    main()
