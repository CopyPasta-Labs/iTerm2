#!/usr/bin/env python3
"""
hermes_state_bakeoff.py — empirically compare ways to detect whether the
`hermes` CLI is *working* (running a turn / calling the LLM / running a tool)
vs *idle* (at the prompt, waiting for the user).

It launches `hermes --cli` inside a pseudo-terminal, drives a scripted
interaction corpus, and records — on one wall clock — what each candidate
*detection method* would report, then scores every method against an
INDEPENDENT ground truth.

Ground truth
------------
The instrumented hermes (cli.py `_emit_iterm_state`) appends every real
`_agent_running` transition to a debug log (a file, via a channel the terminal
renderer cannot swallow) when HERMES_STATE_DEBUG=1. That log is the authority
for *how many* transitions happened and *in what order*.

Methods under test (all observed from outside, exactly as iTerm would)
----------------------------------------------------------------------
  A. OSC      — the in-band `OSC 1337 ; HermesState=...` escape code we added,
                read straight out of the pty byte stream. (The shipping method.)
  D. OUTPUT   — iTerm's existing model: "working" iff bytes arrived from the
                child within the last `--idle-window` seconds.
  E. CPU      — "working" iff the hermes process-tree CPU% exceeds a threshold.
  F. SPINNER  — "working" iff a spinner/"thinking" glyph was seen recently
                (pattern-match on output).

Method A's correctness is validated against the debug log (did the OSC stream
carry the same transitions, losslessly?). The heuristics D/E/F are then scored
for latency and false-positive/negative against the OSC transition timeline
(valid because the OSC fires synchronously at the real flag flip, as the log
confirms).

Usage
-----
  python3 tests/hermes_state_bakeoff.py --probe       # one trivial turn; de-risk
  python3 tests/hermes_state_bakeoff.py --corpus       # full S1-S7 corpus
  python3 tests/hermes_state_bakeoff.py --probe --json results.json

Run it with the hermes venv python so psutil is available for the CPU method:
  ~/.hermes/hermes-agent/venv/bin/python tests/hermes_state_bakeoff.py --probe
"""

import argparse
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time

OSC_RE = re.compile(rb"\x1b\]1337;HermesState=(working|idle)\x07")
# A few spinner/"thinking" glyphs hermes (and rich/prompt_toolkit) commonly use.
SPINNER_BYTES = [c.encode("utf-8") for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒|/-\\"] + [b"Thinking", b"thinking"]

try:
    import psutil  # optional; CPU method degrades to "unavailable" without it
except Exception:
    psutil = None


def now():
    return time.time()


class HermesSession:
    """Runs `hermes --cli` under a pty and records the raw byte stream with
    per-chunk wall-clock timestamps."""

    def __init__(self, hermes_path, debug_log, rows=40, cols=120, extra_args=None):
        self.hermes_path = hermes_path
        self.debug_log = debug_log
        self.rows = rows
        self.cols = cols
        self.extra_args = extra_args or []
        self.pid = None
        self.master_fd = None
        # (timestamp, bytes) chunks, append-only from the read loop.
        self.chunks = []
        self.osc_events = []  # (timestamp, "working"|"idle") as seen in the stream
        self._buf = b""       # rolling tail for OSC reassembly across chunk edges
        self._dead = False
        self.cpu_samples = []  # (timestamp, tree_cpu_percent)
        self._stop_cpu = False
        self._cpu_thread = None

    def start(self):
        env = dict(os.environ)
        env["TERM_PROGRAM"] = "iTerm.app"          # arm the OSC guard
        env["TERM"] = "xterm-256color"
        env["HERMES_STATE_DEBUG"] = "1"
        env["HERMES_STATE_DEBUG_LOG"] = self.debug_log
        env["LINES"] = str(self.rows)
        env["COLUMNS"] = str(self.cols)
        # truncate any prior log
        try:
            open(self.debug_log, "w").close()
        except Exception:
            pass

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: slave is stdio + controlling tty. exec hermes.
            try:
                os.environ.clear()
                os.environ.update(env)
            except Exception:
                pass
            args = [self.hermes_path, "--cli", "--yolo"] + self.extra_args
            try:
                os.execvp(self.hermes_path, args)
            except Exception:
                os._exit(127)
        self.pid = pid
        self.master_fd = master_fd
        # Set window size on the master (shared with slave).
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except Exception:
            pass
        if psutil is not None:
            import threading
            self._cpu_thread = threading.Thread(target=self._cpu_loop, daemon=True)
            self._cpu_thread.start()

    def _cpu_loop(self):
        """Sample summed CPU% of the hermes process tree (~150 ms cadence)."""
        cache = {}
        while not self._stop_cpu:
            try:
                root = psutil.Process(self.pid)
                pids = [self.pid] + [c.pid for c in root.children(recursive=True)]
            except Exception:
                pids = [self.pid]
            total = 0.0
            for pid in pids:
                p = cache.get(pid)
                if p is None:
                    try:
                        p = psutil.Process(pid)
                        p.cpu_percent(None)  # prime; contributes next round
                        cache[pid] = p
                    except Exception:
                        pass
                    continue
                try:
                    total += p.cpu_percent(None)
                except Exception:
                    cache.pop(pid, None)
            self.cpu_samples.append((now(), total))
            time.sleep(0.15)

    def pump(self, seconds, until=None):
        """Read from the pty for up to `seconds`. If `until` (a callable
        returning bool) is given, return early once it is true."""
        deadline = now() + seconds
        while now() < deadline:
            if until is not None and until():
                return True
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                self._dead = True
                return False
            if self.master_fd in r:
                try:
                    data = os.read(self.master_fd, 65536)
                except OSError:
                    self._dead = True
                    return False
                if not data:
                    self._dead = True
                    return False
                t = now()
                self.chunks.append((t, data))
                self._scan_osc(t, data)
        return until() if until is not None else False

    def _scan_osc(self, t, data):
        self._buf += data
        # keep the buffer bounded but large enough to span an OSC across chunks
        if len(self._buf) > 8192:
            self._buf = self._buf[-8192:]
        for m in OSC_RE.finditer(self._buf):
            self.osc_events.append((t, m.group(1).decode()))
        # drop everything up to the last match end to avoid double-counting
        last = None
        for m in OSC_RE.finditer(self._buf):
            last = m
        if last is not None:
            self._buf = self._buf[last.end():]

    def send(self, text):
        try:
            os.write(self.master_fd, text.encode("utf-8") if isinstance(text, str) else text)
        except OSError:
            self._dead = True

    def all_bytes(self):
        return b"".join(d for _, d in self.chunks)

    def stop(self):
        self._stop_cpu = True
        # Try a graceful exit, then escalate.
        for attempt in (b"/exit\r", b"\x03", b"\x04"):
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
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass

    def _exited(self):
        try:
            wpid, _ = os.waitpid(self.pid, os.WNOHANG)
            return wpid == self.pid
        except OSError:
            return True


def read_ground_truth(path):
    """Return list of (monotonic_ts, state) transitions from the debug log."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) == 2:
                    try:
                        out.append((float(parts[0]), parts[1]))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return out


# ---- heuristic method reconstructions, computed over the recorded chunks ----

def method_output_timeline(chunks, idle_window):
    """iTerm's output-activity model: working iff output arrived within the last
    `idle_window` seconds. Returns list of (timestamp, verdict) verdict changes."""
    timeline = []
    last_output = None
    verdict = "idle"
    # Walk chunk arrivals; also synthesise idle-decay checkpoints.
    events = sorted(t for t, _ in chunks)
    i = 0
    t = events[0] if events else now()
    end = events[-1] if events else t
    step = idle_window / 2.0
    cur = t
    while cur <= end + idle_window:
        while i < len(events) and events[i] <= cur:
            last_output = events[i]
            i += 1
        new = "working" if (last_output is not None and cur - last_output < idle_window) else "idle"
        if new != verdict:
            timeline.append((cur, new))
            verdict = new
        cur += step
    return timeline


def method_spinner_timeline(chunks, idle_window):
    """working iff a spinner/'thinking' glyph was seen within `idle_window`."""
    timeline = []
    last_spin = None
    verdict = "idle"
    seq = []
    for t, d in chunks:
        if any(g in d for g in SPINNER_BYTES):
            seq.append(t)
    spins = sorted(seq)
    if not chunks:
        return timeline
    start = chunks[0][0]
    end = chunks[-1][0]
    step = idle_window / 2.0
    j = 0
    cur = start
    while cur <= end + idle_window:
        while j < len(spins) and spins[j] <= cur:
            last_spin = spins[j]
            j += 1
        new = "working" if (last_spin is not None and cur - last_spin < idle_window) else "idle"
        if new != verdict:
            timeline.append((cur, new))
            verdict = new
        cur += step
    return timeline


def osc_transitions(osc_events):
    """Collapse consecutive duplicate states into transition points."""
    out = []
    prev = None
    for t, s in osc_events:
        if s != prev:
            out.append((t, s))
            prev = s
    return out


def score_heuristic(truth_transitions, heur_timeline, tolerance=8.0):
    """For each ground-truth transition (from OSC), find the heuristic's matching
    verdict change within `tolerance` seconds after it; report latency, misses,
    and spurious flips."""
    results = []
    used = [False] * len(heur_timeline)
    for tt, ts in truth_transitions:
        best = None
        for k, (ht, hs) in enumerate(heur_timeline):
            if used[k]:
                continue
            if hs == ts and -1.0 <= (ht - tt) <= tolerance:
                if best is None or (ht - tt) < (heur_timeline[best][0] - tt):
                    best = k
        if best is not None:
            used[best] = True
            results.append({"to": ts, "latency_s": round(heur_timeline[best][0] - tt, 3)})
        else:
            results.append({"to": ts, "latency_s": None, "missed": True})
    spurious = sum(1 for k, u in enumerate(used) if not u)
    matched = [r for r in results if r.get("latency_s") is not None]
    missed = [r for r in results if r.get("missed")]
    avg = round(sum(r["latency_s"] for r in matched) / len(matched), 3) if matched else None
    return {
        "transitions": len(truth_transitions),
        "matched": len(matched),
        "missed": len(missed),
        "spurious_flips": spurious,
        "avg_latency_s": avg,
        "detail": results,
    }


def wait_ready(sess, gt_log, timeout=60.0):
    """hermes emits the startup 'idle' once it reaches app.run(); use it (or any
    OSC, or the first ground-truth line) as the readiness signal."""
    def ready():
        if sess.osc_events:
            return True
        return len(read_ground_truth(gt_log)) >= 1
    sess.pump(timeout, until=ready)
    return ready()


def run_probe(hermes_path, gt_log):
    print("== PROBE: one trivial turn ==")
    sess = HermesSession(hermes_path, gt_log)
    sess.start()
    print("launched pid", sess.pid, "— waiting for ready (startup idle)...")
    if not wait_ready(sess, gt_log, timeout=75.0):
        print("FAIL: hermes never signalled ready within 75s")
        print("  ground-truth log:", read_ground_truth(gt_log))
        print("  first 800 bytes:\n", sess.all_bytes()[:800])
        sess.stop()
        return 1
    print("ready. OSC seen so far:", sess.osc_events)
    n_before = len(osc_transitions(sess.osc_events))
    sess.send("say hi in one word\r")

    def saw_turn():
        gt = read_ground_truth(gt_log)
        states = [s for _, s in gt]
        return states.count("working") >= 1 and states[-1:] == ["idle"] and len(gt) >= 3
    sess.pump(90.0, until=saw_turn)

    gt = read_ground_truth(gt_log)
    osc_tx = osc_transitions(sess.osc_events)
    print("\n-- ground-truth log (file, independent) --")
    for t, s in gt:
        print("   %.3f  %s" % (t, s))
    print("\n-- OSC transitions seen in pty byte stream --")
    for t, s in osc_tx:
        print("   %.3f  %s" % (t, s))

    sess.stop()

    gt_states = [s for _, s in gt]
    osc_states = [s for _, s in osc_tx]
    lossless = gt_states == osc_states and len(gt_states) >= 3
    saw_working = "working" in osc_states
    print("\n== VERDICT ==")
    print("  ground-truth transitions:", gt_states)
    print("  OSC transitions          :", osc_states)
    print("  OSC carried 'working'    :", saw_working)
    print("  OSC == ground truth      :", lossless)
    if lossless:
        print("\nPASS: OSC survives prompt_toolkit intact and matches ground truth.")
        return 0
    if saw_working and osc_states:
        print("\nPARTIAL: OSC reached the terminal but did not exactly match the log "
              "(check for tearing/dedup). Mechanism works; refine emit path.")
        return 0
    print("\nFAIL: OSC did not reach the byte stream. Switch emit to "
          "app.output via call_soon_threadsafe.")
    return 2


def method_cpu_timeline(cpu_samples, threshold):
    """working iff tree CPU% > threshold at the sample."""
    timeline = []
    verdict = "idle"
    for t, c in cpu_samples:
        new = "working" if c > threshold else "idle"
        if new != verdict:
            timeline.append((t, new))
            verdict = new
    return timeline


def label_at(transitions, t, initial="idle"):
    s = initial
    for tt, ss in transitions:
        if tt <= t:
            s = ss
        else:
            break
    return s


def grid_score(truth_tx, method_tx, t0, t1, step=0.2):
    """Time-coverage agreement between a method's verdict timeline and ground
    truth over [t0, t1]. Returns agreement%, false-negative secs (truth=working,
    method=idle), false-positive secs (truth=idle, method=working), and the
    leading-edge latency for the first working transition."""
    if t1 <= t0:
        return None
    n = 0
    agree = 0
    fn = 0.0
    fp = 0.0
    t = t0
    while t <= t1:
        gt = label_at(truth_tx, t)
        mt = label_at(method_tx, t)
        n += 1
        if gt == mt:
            agree += 1
        elif gt == "working" and mt == "idle":
            fn += step
        elif gt == "idle" and mt == "working":
            fp += step
        t += step
    # leading-edge latency: first truth working → first method working at/after it
    lat = None
    first_w = next((tt for tt, ss in truth_tx if ss == "working"), None)
    if first_w is not None:
        mw = next((tt for tt, ss in method_tx if ss == "working" and tt >= first_w - 1.0), None)
        if mw is not None:
            lat = round(mw - first_w, 3)
    return {
        "agreement_pct": round(100.0 * agree / n, 1) if n else None,
        "false_negative_s": round(fn, 1),
        "false_positive_s": round(fp, 1),
        "lead_latency_s": lat,
    }


CORPUS = [
    {"id": "S1", "desc": "quick reply (~short LLM turn)",
     "prompt": "say hi in exactly one word", "settle": 60.0},
    {"id": "S2", "desc": "long LLM turn (streaming, network-bound, low CPU)",
     "prompt": "Write about 400 words on the history of terminal emulators.",
     "settle": 90.0},
    {"id": "S3", "desc": "silent tool turn (CPU-idle, output-quiet)  [output/CPU killer]",
     "prompt": "Run this exact shell command and then say done: sleep 15",
     "settle": 60.0},
    {"id": "S4", "desc": "idle at the prompt (must stay idle)  [false-positive test]",
     "prompt": None, "settle": 14.0},
]


def run_scenario(hermes_path, gt_log, scn, idle_window):
    sess = HermesSession(hermes_path, gt_log)
    sess.start()
    if not wait_ready(sess, gt_log, timeout=75.0):
        sess.stop()
        return {"id": scn["id"], "error": "never ready"}
    t_start = now()
    if scn["prompt"] is not None:
        sess.send(scn["prompt"] + "\r")

        def saw_idle_after_work():
            gt = read_ground_truth(gt_log)
            states = [s for _, s in gt]
            return states.count("working") >= 1 and states[-1:] == ["idle"]
        sess.pump(scn["settle"], until=saw_idle_after_work)
        # small tail so post-turn idle is observed
        sess.pump(3.0)
    else:
        sess.pump(scn["settle"])
    t_end = now()
    sess.stop()

    osc_tx = osc_transitions(sess.osc_events)
    out_tx = method_output_timeline(sess.chunks, idle_window)
    spin_tx = method_spinner_timeline(sess.chunks, idle_window)
    cpu_tx = method_cpu_timeline(sess.cpu_samples, threshold=8.0)
    gt = read_ground_truth(gt_log)

    # Working-interval stats for context (CPU during working).
    cpu_during_working = []
    for t, c in sess.cpu_samples:
        if label_at(osc_tx, t) == "working":
            cpu_during_working.append(c)
    cpu_work_avg = round(sum(cpu_during_working) / len(cpu_during_working), 1) if cpu_during_working else None
    cpu_work_max = round(max(cpu_during_working), 1) if cpu_during_working else None

    scores = {
        "OSC": {"agreement_pct": 100.0, "false_negative_s": 0.0, "false_positive_s": 0.0,
                "lead_latency_s": 0.0, "note": "ground truth (validated lossless vs log)"},
        "OUTPUT": grid_score(osc_tx, out_tx, t_start, t_end),
        "CPU": grid_score(osc_tx, cpu_tx, t_start, t_end) if psutil else {"note": "psutil unavailable"},
        "SPINNER": grid_score(osc_tx, spin_tx, t_start, t_end),
    }
    return {
        "id": scn["id"],
        "desc": scn["desc"],
        "ground_truth_log_transitions": [s for _, s in gt],
        "osc_transitions": [s for _, s in osc_tx],
        "osc_lossless_vs_log": [s for _, s in gt] == [s for _, s in osc_tx],
        "cpu_during_working_avg_pct": cpu_work_avg,
        "cpu_during_working_max_pct": cpu_work_max,
        "scores": scores,
    }


def run_corpus(hermes_path, gt_log, idle_window, json_path):
    print("== CORPUS S1-S4 ==")
    results = []
    for scn in CORPUS:
        print("\n--- %s: %s ---" % (scn["id"], scn["desc"]))
        r = run_scenario(hermes_path, gt_log, scn, idle_window)
        results.append(r)
        if "error" in r:
            print("  ERROR:", r["error"])
            continue
        print("  ground-truth log :", r["ground_truth_log_transitions"])
        print("  OSC stream       :", r["osc_transitions"], "(lossless vs log:", r["osc_lossless_vs_log"], ")")
        print("  CPU during work  : avg %s%%  max %s%%" % (r["cpu_during_working_avg_pct"], r["cpu_during_working_max_pct"]))
        for m in ("OSC", "OUTPUT", "CPU", "SPINNER"):
            s = r["scores"].get(m) or {}
            print("    %-8s agree=%-5s fn=%-5ss fp=%-5ss lead=%-6ss %s" % (
                m, s.get("agreement_pct"), s.get("false_negative_s"),
                s.get("false_positive_s"), s.get("lead_latency_s"), s.get("note", "")))
    if json_path:
        try:
            with open(json_path, "w") as f:
                json.dump(results, f, indent=2)
            print("\nwrote", json_path)
        except Exception as e:
            print("could not write json:", e)
    return results


def find_hermes():
    for cand in (os.path.expanduser("~/.local/bin/hermes"),
                 "/usr/local/bin/hermes", "hermes"):
        if cand == "hermes" or os.path.exists(cand):
            return cand
    return "hermes"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="single trivial turn (de-risk)")
    ap.add_argument("--corpus", action="store_true", help="full S1-S7 corpus")
    ap.add_argument("--hermes", default=None, help="path to hermes launcher")
    ap.add_argument("--idle-window", type=float, default=2.0,
                    help="output/spinner idle window seconds (iTerm default ~2)")
    ap.add_argument("--json", default=None, help="write results JSON to this path")
    args = ap.parse_args()

    hermes_path = args.hermes or find_hermes()
    gt_log = os.path.join(os.path.expanduser("~/.hermes"), "hermes-state-debug.log")
    print("hermes:", hermes_path)
    print("ground-truth log:", gt_log)
    print("psutil available (CPU method):", psutil is not None)

    if args.corpus:
        run_corpus(hermes_path, gt_log, args.idle_window, args.json)
        sys.exit(0)

    rc = run_probe(hermes_path, gt_log)
    sys.exit(rc)


if __name__ == "__main__":
    main()
