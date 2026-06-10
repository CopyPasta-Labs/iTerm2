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
  python3 tests/claude_state_probe.py --keep      # don't delete the scratch dir
"""

import os
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


def main():
    args = [a for a in sys.argv[1:]]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    mode = args[0] if args else "print"
    if mode not in ("print", "interactive", "fifo"):
        print("usage: claude_state_probe.py [print|interactive|fifo] [--keep]")
        sys.exit(2)
    if not os.path.exists(CLAUDE):
        print("claude not found at %s" % CLAUDE)
        sys.exit(1)
    if mode == "fifo":
        run_fifo(keep)
    else:
        run(mode, keep)


if __name__ == "__main__":
    main()
