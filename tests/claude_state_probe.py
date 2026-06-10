#!/usr/bin/env python3
"""Probe how Claude Code can report working/idle state to the iTerm2 fork.

The hermes status dot is driven by an in-band OSC 1337 escape code hermes writes
to /dev/tty at its own busy-flag transitions. We want the same dot for the
`claude` CLI. Claude Code can't push OSC 1337 through its supported
`terminalSequence` hook channel (the allowlist drops 1337), so the open question
is whether a Claude Code *command hook* can write OSC 1337 straight to /dev/tty
the way hermes does -- i.e. whether the async-hook process keeps a usable
controlling terminal.

This probe answers that empirically, without touching the user's real
~/.claude/settings.json and without disturbing a live claude:

  * It runs `claude` under a pseudo-terminal we own, injecting probe hooks via
    `--settings` (an extra settings file, merged on top of the user's).
  * The UserPromptSubmit hook records whether /dev/tty is writable and what its
    controlling tty is, then emits a sentinel `OSC 1337 ; AgentState=__probe__`
    to /dev/tty. Other lifecycle hooks (Stop/StopFailure/Notification/
    PermissionRequest/SessionStart) just log that they fired, with timestamps.
  * We then check whether the sentinel reached the pty master (i.e. would reach
    the terminal emulator) and dump the hook log.

VERDICT:
  tty OPEN + sentinel seen on master  -> Transport A (hook -> /dev/tty, like hermes)
  tty BLOCKED / sentinel not seen     -> Transport B (launcher wrapper + FIFO)

Usage:
  python3 tests/claude_state_probe.py            # -p (print) mode, fast
  python3 tests/claude_state_probe.py interactive # drive a TUI turn under the pty
  python3 tests/claude_state_probe.py fifo        # end-to-end FIFO working/idle
  python3 tests/claude_state_probe.py interrupt   # Item 3: what fires on a ^C mid-turn
  python3 tests/claude_state_probe.py --keep      # don't delete the scratch dir
"""

import os
import re
import sys
import pty
import json
import time
import select
import shutil
import signal
import tempfile

CLAUDE = os.path.expanduser("~/.local/bin/claude")
TRUSTED_CWD = os.path.expanduser("~")  # already in ~/.claude.json projects -> no trust prompt
SENTINEL_WORK = "AgentState=__probe__"
SENTINEL_STOP = "AgentState=__stop__"
READ_TIMEOUT = 75.0  # hard cap so we never hang


def build_settings(log_path):
    """Return a settings dict whose hooks log to log_path and probe /dev/tty."""

    def logline(marker, extra=""):
        # Append one timestamped marker line; never fail the turn (exit 0).
        return ('echo "[%s] t=$(date +%%s.%%N) %s" >> %s'
                % (marker, extra, _shq(log_path)))

    ups = (
        '{ if ( : > /dev/tty ) 2>/dev/null; then T=OPEN; else T=BLOCKED; fi; '
        'echo "[UserPromptSubmit] t=$(date +%s.%N) tty=$T ctty=$(tty 2>&1) '
        'sess=[$(ps -o sess=,tty= -p $$ 2>/dev/null)] '
        'env=[TERM_PROGRAM=$TERM_PROGRAM|ITERM_SESSION_ID=$ITERM_SESSION_ID|'
        'ITERM_AGENT_STATE_FIFO=$ITERM_AGENT_STATE_FIFO]" >> ' + _shq(log_path) + '; '
        "printf '\\033]1337;" + SENTINEL_WORK + "\\007' > /dev/tty 2>> " + _shq(log_path) + '; '
        '} ; exit 0'
    )
    stop = (
        '{ ' + logline("Stop") + '; '
        "printf '\\033]1337;" + SENTINEL_STOP + "\\007' > /dev/tty 2>> " + _shq(log_path) + '; '
        '} ; exit 0'
    )

    def simple(marker):
        return '{ ' + logline(marker) + '; } ; exit 0'

    def entry(cmd):
        return [{"hooks": [{"type": "command", "command": cmd}]}]

    return {
        "hooks": {
            "SessionStart": entry(simple("SessionStart")),
            "UserPromptSubmit": entry(ups),
            "Stop": entry(stop),
            "StopFailure": entry(simple("StopFailure")),
            "Notification": entry(simple("Notification")),
            "PermissionRequest": entry(simple("PermissionRequest")),
            # (Fork) Added for the `interrupt` mode: PreToolUse/PostToolUse are the
            # only mid-turn events claude emits (the heartbeat cadence that would
            # gate a silence-timeout), and SessionEnd is a possible end-of-turn
            # signal on an interrupt. Harmless in the other modes.
            "PreToolUse": entry(simple("PreToolUse")),
            "PostToolUse": entry(simple("PostToolUse")),
            "SessionEnd": entry(simple("SessionEnd")),
        }
    }


def _shq(s):
    """Single-quote a string for POSIX sh."""
    return "'" + s.replace("'", "'\\''") + "'"


def run(mode, keep):
    scratch = tempfile.mkdtemp(prefix="agentprobe.")
    log_path = os.path.join(scratch, "hooks.log")
    settings_path = os.path.join(scratch, "settings.json")
    raw_path = os.path.join(scratch, "master.raw")
    open(log_path, "w").close()
    with open(settings_path, "w") as f:
        json.dump(build_settings(log_path), f, indent=2)

    if mode == "interactive":
        argv = [CLAUDE, "--settings", settings_path,
                "--permission-mode", "bypassPermissions"]
    else:
        argv = [CLAUDE, "--settings", settings_path,
                "--permission-mode", "bypassPermissions",
                "-p", "Reply with exactly one word: ok"]

    print("scratch:   %s" % scratch)
    print("settings:  %s" % settings_path)
    print("mode:      %s" % mode)
    print("argv:      %s" % " ".join(argv))
    print("-" * 60)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["TERM_PROGRAM"] = "iTerm.app"  # mimic the hermes guard's condition
    env["ITERM_SESSION_ID"] = "w0t0p0:PROBE"
    env.pop("ITERM_AGENT_STATE_FIFO", None)  # confirm it is NOT inherited from us

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(TRUSTED_CWD)
        except OSError:
            pass
        os.execvpe(CLAUDE, argv, env)
        os._exit(127)

    # Parent: pump prompt (interactive) and capture master until child exit/timeout.
    raw = bytearray()
    deadline = time.time() + READ_TIMEOUT
    sent_prompt = False
    prompt_at = time.time() + 4.0  # let the TUI settle before typing
    child_done = False
    status = None
    while time.time() < deadline:
        if mode == "interactive" and not sent_prompt and time.time() >= prompt_at:
            try:
                os.write(master, b"Reply with exactly one word: ok\r")
            except OSError:
                pass
            sent_prompt = True
        try:
            r, _, _ = select.select([master], [], [], 0.5)
        except select.error:
            break
        if master in r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
        # Reap without blocking.
        try:
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                child_done = True
                status = st
                # drain any final bytes
                try:
                    r, _, _ = select.select([master], [], [], 0.5)
                    if master in r:
                        raw += os.read(master, 65536)
                except (OSError, select.error):
                    pass
                break
        except ChildProcessError:
            child_done = True
            break
        # Interactive: once we've seen the stop sentinel, we're done.
        if mode == "interactive" and SENTINEL_STOP.encode() in raw:
            break

    if not child_done:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
                time.sleep(0.5)
                wpid, st = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    status = st
                    break
            except (ProcessLookupError, ChildProcessError):
                break
    try:
        os.close(master)
    except OSError:
        pass

    with open(raw_path, "wb") as f:
        f.write(raw)

    # ---- Analyze ----
    work_seen = SENTINEL_WORK.encode() in raw
    stop_seen = SENTINEL_STOP.encode() in raw
    with open(log_path) as f:
        log = f.read()

    tty_state = "UNKNOWN"
    for line in log.splitlines():
        if line.startswith("[UserPromptSubmit]"):
            if "tty=OPEN" in line:
                tty_state = "OPEN"
            elif "tty=BLOCKED" in line:
                tty_state = "BLOCKED"

    print("=== HOOK LOG (%s) ===" % log_path)
    print(log.rstrip() or "(empty -- no hooks fired)")
    print()
    print("=== MASTER STREAM ===")
    print("captured %d bytes (raw at %s)" % (len(raw), raw_path))
    print("sentinel AgentState=__probe__ on master (UserPromptSubmit): %s" % work_seen)
    print("sentinel AgentState=__stop__  on master (Stop):            %s" % stop_seen)
    print("child exit status: %s" % (status,))
    print()
    print("=== VERDICT ===")
    if tty_state == "OPEN" and work_seen:
        print("TRANSPORT A: a command hook CAN write OSC 1337 to /dev/tty and it")
        print("reaches the terminal. Ship the hermes-style hooks; no wrapper needed.")
    elif tty_state == "BLOCKED" or (log and not work_seen):
        print("TRANSPORT B: the hook cannot reach the terminal via /dev/tty.")
        print("Use the launcher-wrapper + FIFO relay.")
    else:
        print("INCONCLUSIVE: no hooks fired (log empty). Hooks may be disabled in")
        print("this mode -- try `interactive`, or check the settings schema.")
    print()
    if keep:
        print("scratch kept at %s" % scratch)
    else:
        shutil.rmtree(scratch, ignore_errors=True)
        print("scratch removed")


def run_fifo(keep):
    """Simulate iTerm's iTermAgentStateChannel end to end: create a FIFO, inject
    its path as ITERM_AGENT_STATE_FIFO, launch the REAL claude (so the user's
    installed ~/.claude/settings.json hooks fire), drive a turn, and report the
    working/idle messages read off the FIFO. PASS here means the dot will work."""
    scratch = tempfile.mkdtemp(prefix="agentfifo.")
    fifo_path = os.path.join(scratch, "state")
    os.mkfifo(fifo_path, 0o600)
    # O_RDWR | O_NONBLOCK exactly as iTermAgentStateChannel opens it.
    fifo_fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)

    argv = [CLAUDE, "--permission-mode", "bypassPermissions"]
    print("scratch: %s" % scratch)
    print("fifo:    %s" % fifo_path)
    print("argv:    %s" % " ".join(argv))
    print("-" * 60)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["TERM_PROGRAM"] = "iTerm.app"
    env["ITERM_AGENT_STATE_FIFO"] = fifo_path

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(TRUSTED_CWD)
        except OSError:
            pass
        os.execvpe(CLAUDE, argv, env)
        os._exit(127)

    events = []  # (elapsed, state)
    buf = b""
    deadline = time.time() + READ_TIMEOUT
    sent = False
    prompt_at = time.time() + 4.0
    start = time.time()
    while time.time() < deadline:
        if not sent and time.time() >= prompt_at:
            try:
                os.write(master, b"Reply with exactly one word: ok\r")
            except OSError:
                pass
            sent = True
        try:
            r, _, _ = select.select([master, fifo_fd], [], [], 0.5)
        except select.error:
            break
        if master in r:
            try:
                os.read(master, 65536)  # drain; content not needed
            except OSError:
                pass
        if fifo_fd in r:
            try:
                data = os.read(fifo_fd, 4096)
            except OSError:
                data = b""
            if data:
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", "replace").strip()
                    if s:
                        events.append((time.time() - start, s))
                        print("  FIFO <- %-8s (+%.2fs)" % (s, time.time() - start))
        states = [s for _, s in events]
        if "working" in states and states and states[-1] == "idle":
            break

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
            time.sleep(0.4)
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                break
        except (ProcessLookupError, ChildProcessError):
            break
    try:
        os.close(master)
    except OSError:
        pass
    os.close(fifo_fd)

    print()
    print("=== VERDICT ===")
    states = [s for _, s in events]
    if "working" in states and "idle" in states:
        print("PASS: claude’s hooks drove the FIFO working→idle. The iTerm dot will work.")
    elif states:
        print("PARTIAL: saw %s but not a full working/idle cycle." % states)
    else:
        print("FAIL: no messages on the FIFO. Check ~/.claude/settings.json hooks.")
    if keep:
        print("scratch kept at %s" % scratch)
    else:
        shutil.rmtree(scratch, ignore_errors=True)


def run_interrupt(keep):
    """Characterize what claude does on a Ctrl-C interrupt mid-turn, plus the
    PreToolUse/PostToolUse heartbeat cadence. This decides Item 3: drive a slow
    tool-using turn under a pty (bypassPermissions, so the tool auto-runs), ^C it
    while the tool is running, and report which lifecycle hooks fire afterward.
    If an end-of-turn hook fires on interrupt we map it -> idle (event-driven, no
    timer); if none does, a generous silence-timeout backstop is the only option."""
    scratch = tempfile.mkdtemp(prefix="agentintr.")
    log_path = os.path.join(scratch, "hooks.log")
    settings_path = os.path.join(scratch, "settings.json")
    open(log_path, "w").close()
    with open(settings_path, "w") as f:
        json.dump(build_settings(log_path), f, indent=2)

    # A turn that runs a slow shell tool, giving a wide window to interrupt.
    prompt = "Run this exact shell command and report its output: sleep 25; echo SLEEP_DONE"
    argv = [CLAUDE, "--settings", settings_path, "--permission-mode", "bypassPermissions"]
    print("scratch:  %s" % scratch)
    print("argv:     %s" % " ".join(argv))
    print("prompt:   %s" % prompt)
    print("-" * 60)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["TERM_PROGRAM"] = "iTerm.app"
    env.pop("ITERM_AGENT_STATE_FIFO", None)

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(TRUSTED_CWD)
        except OSError:
            pass
        os.execvpe(CLAUDE, argv, env)
        os._exit(127)

    def read_log():
        try:
            with open(log_path) as f:
                return f.read()
        except OSError:
            return ""

    start = time.time()
    deadline = start + 110.0
    sent = False
    cr_done = False
    cr_at = None
    prompt_at = start + 6.0  # let the TUI fully settle before typing
    interrupted_at = None
    while time.time() < deadline:
        if not sent and time.time() >= prompt_at:
            try:
                os.write(master, prompt.encode())  # body first...
            except OSError:
                pass
            sent = True
            cr_at = time.time() + 0.4
        # ...then a SEPARATE carriage return: claude's TUI treats text+CR arriving
        # in one write as a paste (the CR is not a submit), but a lone CR is Enter.
        if sent and not cr_done and time.time() >= cr_at:
            try:
                os.write(master, b"\r")
            except OSError:
                pass
            cr_done = True
        try:
            r, _, _ = select.select([master], [], [], 0.5)
        except select.error:
            break
        if master in r:
            try:
                if not os.read(master, 65536):
                    break
            except OSError:
                break
        log = read_log()
        submitted = "[UserPromptSubmit]" in log
        # Interrupt only after the turn was accepted: once a tool is running (best),
        # else a while after submit (the model is answering without a tool).
        if interrupted_at is None and submitted:
            ready = ("[PreToolUse]" in log) or (cr_done and (time.time() - cr_at) > 12.0)
            if ready:
                time.sleep(2.0)
                try:
                    os.write(master, b"\x03")  # Ctrl-C: interrupt the running turn
                except OSError:
                    pass
                interrupted_at = time.time()
                why = "PreToolUse seen" if "[PreToolUse]" in log else "post-submit timeout"
                print("  sent ^C at +%.2fs (%s)" % (interrupted_at - start, why))
        # Capture ~12s after the interrupt for any end-of-turn hook, then stop.
        if interrupted_at is not None and time.time() - interrupted_at > 12.0:
            break

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
            time.sleep(0.4)
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                break
        except (ProcessLookupError, ChildProcessError):
            break
    try:
        os.close(master)
    except OSError:
        pass

    log = read_log()
    rows = []
    for line in log.splitlines():
        m = re.match(r"\[(\w+)\] t=([0-9.]+)", line)
        if m:
            rows.append((m.group(1), float(m.group(2))))
    markers = [r[0] for r in rows]

    print("=== TIMELINE (marker @ +s from first hook) ===")
    if rows:
        t0 = rows[0][1]
        intr_offset = (interrupted_at - start) if interrupted_at else None
        for name, t in rows:
            print("  +%6.2fs  %s" % (t - t0, name))
        if intr_offset is not None:
            # interrupt wall-clock relative to the same t0 is approximate (start vs first hook).
            print("  (^C sent ~+%.2fs after launch)" % intr_offset)
    else:
        print("(no hooks fired)")
    print()
    print("=== VERDICT (Item 3) ===")
    if "UserPromptSubmit" not in markers:
        print("INCONCLUSIVE: the prompt never submitted (no UserPromptSubmit hook) —")
        print("the TUI didn't accept the typed turn, so the interrupt wasn't exercised.")
        print()
    print("PreToolUse fired (heartbeat available):  %s" % ("PreToolUse" in markers))
    print("PostToolUse fired:                       %s" % ("PostToolUse" in markers))
    print("SLEEP_DONE in log/markers (tool finished): %s" % ("SLEEP_DONE" in log))
    end_hooks = [m for m in ("Stop", "StopFailure", "Notification", "SessionEnd") if m in markers]
    if end_hooks:
        print("End-of-turn hook(s) seen after interrupt: %s" % ", ".join(end_hooks))
        print("-> PREFERRED: map that hook -> idle (event-driven, no timer).")
    else:
        print("No end-of-turn hook fired on interrupt.")
        print("-> Silence-timeout backstop only (mind the long-tool false-idle caveat).")
    if keep:
        print("scratch kept at %s" % scratch)
    else:
        shutil.rmtree(scratch, ignore_errors=True)


def main():
    args = [a for a in sys.argv[1:]]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    mode = args[0] if args else "print"
    if mode not in ("print", "interactive", "fifo", "interrupt"):
        print("usage: claude_state_probe.py [print|interactive|fifo|interrupt] [--keep]")
        sys.exit(2)
    if not os.path.exists(CLAUDE):
        print("claude not found at %s" % CLAUDE)
        sys.exit(1)
    if mode == "fifo":
        run_fifo(keep)
    elif mode == "interrupt":
        run_interrupt(keep)
    else:
        run(mode, keep)


if __name__ == "__main__":
    main()
