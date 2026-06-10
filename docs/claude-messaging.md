# Sending to and receiving from a claude-code tab — methods, KPIs, and results

*How this iTerm2 fork lets you send a message to a claude-code agent running in a
tab and get its reply back programmatically, every capture method that was tried,
and the empirical bake-off that decided it.*

This is the claude counterpart of [`hermes-messaging.md`](hermes-messaging.md) and
the messaging companion to [`claude-state-detection.md`](claude-state-detection.md).
Read the latter first: it established the working/idle signal this feature reuses
as its “the turn is finished” trigger. Unlike hermes (which emits an in-band
`OSC 1337` code), claude can’t write to its own controlling tty, so the fork feeds
it a private FIFO: claude’s own hooks write `working` (on `UserPromptSubmit`) and
`idle` (on `Stop`/`StopFailure`) to `$ITERM_AGENT_STATE_FIFO`, which lands in the
same per-session `agentState` that paints the tab dot.

---

## TL;DR (the verdict)

Drive the **live, interactive** claude tab: inject the message into its pty
exactly as if typed (so a human can still watch, type, and interrupt), wait for
the agent’s own working→idle edge, then read the reply back **out of band from
claude’s JSONL transcript** — never by scraping the terminal.

In a head-to-head bake-off over an 8-turn message corpus, reading the reply from
the transcript scored **mean fidelity 1.00 and 6/6 known-answer matches**, with the
reply available within ~4 ms of the idle edge. Every method that recovers the
reply *from the terminal* lost badly — screen-scrape **0.21**, raw output-stream
**0.11** — because claude is an Ink TUI that repaints the whole viewport, collapses
pasted input to a `[Pasted text #1 +12 lines]` placeholder, and frames everything
in box-drawing rules, a spinner, and a `bypass permissions · … tokens` status bar.
A `claude -p` one-shot is exact on simple prompts but is disqualified by
construction: it spawns a fresh ~6 s process with **no memory of the tab’s
conversation** (it answered the context-recall turn with “there’s nothing earlier
in our conversation”).

---

## The problem

The fork manages agentic CLIs, one per tab. Beyond *showing* whether a claude tab
is busy (the red/green dot), we want to **talk to it programmatically**: send it a
message and reliably get its reply text back — without tearing down the live
session the user is interacting with.

Sending is easy and already interactive-safe: write the bytes to the pty
(`-[PTYSession writeTaskNoBroadcast:]`), exactly as if typed. The hard half is
**capture**: once the turn ends, what did claude actually *say*? Two facts make
this awkward to read off the screen:

1. **claude is a full-redraw Ink TUI.** It repaints the visible region constantly,
   so the byte stream between “send” and “idle” is dominated by cursor moves,
   repeated frames, a spinner (`✻ Cogitating…`), a live `bypass permissions ·
   … tokens · esc to interrupt` status line, and the echoed prompt — and it
   collapses multi-line pasted input into a `[Pasted text #1 +12 lines]`
   placeholder, so the prompt itself isn’t even on screen.
2. **The reply can scroll.** A long answer moves through the viewport; whatever a
   scrape sees depends on timing and window size.

Knowing *when* the turn ends is already solved — claude’s `Stop` hook fires `idle`
on the FIFO at the real end of the turn (claude’s own lifecycle event, not a
heuristic). This feature reuses that edge and only has to answer “what was said.”

## KPIs

A capture method is judged on:

| # | KPI | What good looks like |
|---|-----|----------------------|
| 1 | **Fidelity** | captured text equals what claude actually said (vs an oracle / a known answer) |
| 2 | **Completeness** | no truncation of long, multi-line, or streamed replies |
| 3 | **Round-trip success** | the right reply is bound to the right send |
| 4 | **Latency** | reply available soon after the idle edge |
| 5 | **Interactive-safe** | the live tab keeps working; capture is invisible and read-only |
| 6 | **Context preservation** | the reply reflects the tab’s ongoing conversation, not a fresh session |
| 7 | **Robustness to updates** | survives TUI churn; not bound to fragile rendering |

A claude-specific unknown sits *before* capture: claude’s input model differs from
hermes’s `prompt_toolkit`, so will injecting `text` + carriage-return actually
submit? The bake-off measures this too — a submit claude accepts fires
`UserPromptSubmit` → `working` on the FIFO, so a missing `working` after a send is
a positive signal the incantation didn’t take. (Result below: it took on **8/8**
turns, single- and multi-line.)

## Methods evaluated

Send is identical for all (write to the pty). They differ only in capture:

| Key | Method | Mechanism |
|-----|--------|-----------|
| **TRANSCRIPT** | transcript read *(shipped)* | after the idle edge, read the assistant `text` blocks claude appended to `~/.claude/projects/<munged-cwd>/<uuid>.jsonl` after our message |
| **SCRAPE** | screen scrape | render the pty byte window through a terminal emulator (pyte) and read the display grid, minus the Ink chrome |
| **STREAM** | raw output strip | strip ANSI/control from the raw `send..idle` byte window |
| **ONESHOT** | fresh one-shot *(reference)* | spawn `claude -p <msg>` — **not** the live tab |

## The bake-off

**Harness:** [`tests/claude_message_bakeoff.py`](../tests/claude_message_bakeoff.py)
launches `claude` under a pseudo-terminal with a private FIFO wired exactly like
the fork’s (`mkfifo` + `ITERM_AGENT_STATE_FIFO`), sends each message on one live
session, reuses the working→idle edge as the “reply complete” trigger, and
captures the reply four ways on the same wall clock.

**Oracle.** The transcript is claude’s own canonical, ANSI-free record of what it
said, so TRANSCRIPT is ≈ the oracle by construction — stated plainly here.
Scenarios **C1/C4/C5b/C6** also use **known-answer** prompts whose expected reply
we control (e.g. “reply with exactly `ZQ7-DELTA`”), an oracle-*independent* check
every method — TRANSCRIPT included — must pass.

**Message corpus:**

- **C1** short, known-answer (`ZQ7-DELTA`)
- **C2** long streamed reply (~400 words) — the truncation killer
- **C3** fenced code block containing ANSI escapes — the de-ANSI killer
- **C4** 12-line verbatim echo, known-answer — exact multi-line fidelity
- **C5a/C5b** remember a code word, then recall it — the **live-tab / context** test
- **C6a/C6b** two quick turns — correct turn-binding (`ALPHA-ONE` vs `BETA-TWO`)

### Results (mean over the corpus; higher fidelity is better)

| Method | mean fidelity vs oracle | known-answer match | mean capture latency | drives the live tab? |
|--------|:--:|:--:|:--:|:--:|
| **TRANSCRIPT** | **1.00** | **6/6** | ~0.004 s | ✅ read-only, invisible |
| SCRAPE | 0.21 | 5/6 | 0.09 s | ✅ but unusable |
| STREAM | 0.11 | 5/6 | 0.00 s | ✅ but unusable |
| ONESHOT | 0.67 | 2/3 | 6.0 s | ❌ fresh process, not the tab |

Per-scenario fidelity (TRANSCRIPT / SCRAPE / STREAM):

| Scenario | TRANSCRIPT | SCRAPE | STREAM |
|----------|:--:|:--:|:--:|
| C1 short | **1.00** | 0.062 | 0.033 |
| C2 long stream | **1.00** | 0.918 | 0.493 |
| C3 code+ANSI | **1.00** | 0.249 | 0.128 |
| C4 verbatim echo | **1.00** (known ✓) | 0.011 (known ✗) | 0.007 (known ✗) |
| C5b context recall | **1.00** (known ✓) | 0.168 (known ✓) | 0.074 (known ✓) |
| C6a / C6b turn-binding | **1.00** (known ✓) | 0.14 (known ✓) | 0.05 (known ✓) |

Submit accepted on **8/8** turns (every corpus turn’s `UserPromptSubmit` fired),
including the multi-line C4 sent via bracketed paste.

### Reading the table

- **TRANSCRIPT** is exact everywhere, including the 12-line verbatim echo (C4) and
  the context recall (C5b: it pulled `PURPLE-WALRUS-42` out of the **live** session,
  proving the reply reflects the tab’s ongoing conversation). C6a/C6b confirm
  turn-binding: each send’s reply is isolated by matching the exact message text
  and reading the assistant `text` blocks that follow it.
- **SCRAPE/STREAM** *contain* the answer for short prompts (so they can pass a loose
  known-answer substring check) but their fidelity collapses because they also
  capture the echoed prompt, `────` rules, the `⏺` reply marker, a spinner, and the
  `bypass permissions on (shift+tab to cycle) · … tokens` status bar — and even
  cross-turn leakage (C5b’s scrape caught a later “now print GREEN” line). On the
  12-line echo (C4) both **failed** the known-answer outright: claude rendered the
  pasted prompt as `[Pasted text #1 +12 lines]`, so there was nothing on screen to
  scrape. SCRAPE’s best case (0.92 on the long prose C2) is still far from usable.
- **ONESHOT** is exact on simple known-answer prompts but is disqualified by KPIs
  5–6: it spawns a fresh ~6 s process and does not talk to the live tab. It
  **failed C5b** — “No code word. There’s nothing earlier in our conversation” —
  the clearest possible proof that a one-shot is not the live session.

## Why TRANSCRIPT wins on every KPI

1. **Fidelity / completeness** — it reads claude’s own verbatim record; 1.00 across
   the corpus, no truncation, no ANSI, including long, code, and multi-line replies.
2. **Round-trip success** — the reply is bound to the send by matching our exact
   message text (a genuine user prompt is a line with *string* `content`; tool
   results are arrays and are skipped) and taking the assistant `text` blocks that
   follow it, across any tool-use sub-turns, up to the next user prompt.
3. **Latency** — the line is present within milliseconds of the idle edge (we poll
   briefly; mean capture 4 ms).
4. **Interactive-safe** — the message is injected into the live pty; the read is a
   read-only scan of `~/.claude/projects` (nothing under `~/.claude` is ever
   written). The terminal is never touched for capture.
5. **Context preservation** — it is the live session, so the conversation carries.
6. **Robustness** — it does not depend on the Ink TUI’s rendering, only on claude’s
   stable JSONL transcript shape; a TUI/skin/spinner change cannot break it.

The trade is coupling to claude’s transcript layout. That is acceptable here; a
terminal-mediated fallback (SCRAPE) would be the path to an agent with no such
store, at the fidelity cost shown above.

## What was built

The round-trip orchestration is identical for hermes and claude — begin, wait for
the working→idle edge, poll, deliver — so it lives once in a shared base, and each
agent supplies only *where the reply is read*:

**iTerm side** (the feature is owned by iTerm2):

1. `iTermAgentMessenger` (`sources/PTYSession/`) — the shared round-trip state
   machine: on `beginSendingMessage:` it records the send time; it watches the
   working→idle transitions forwarded from `screenSetAgentState:`; on idle it polls
   the agent’s store and calls back on the main queue. A 240 s safety timeout can
   never strand the caller. Subclasses override one method, `readReplyToMessage:since:`.
2. `iTermClaudeMessenger` — the claude subclass: reads the JSONL transcript
   (newest project-dir file touched since the send, content-matched to our exact
   message, read-only via `NSJSONSerialization`). (`iTermHermesMessenger` is the
   sibling that reads hermes’s `state.db`.)
3. `-[PTYSession sendClaudeMessage:completion:]` — lazily owns the messenger,
   injects the message into the live pty (`writeTaskNoBroadcast:`), then submits
   it with a **separate** carriage return a beat later. This split matters:
   claude’s TUI treats a `text`+CR burst arriving in one read as a *paste* (the CR
   becomes literal and doesn’t submit), whereas a lone CR reads as Enter — so the
   text and the Enter must arrive as two reads. Multi-line messages are
   bracketed-pasted so they aren’t split at their own newlines. (hermes, by
   contrast, submits on a combined write — hence the per-agent send paths.)
4. `iTermClaudeSendBuiltInFunction` — registers `iterm2.claude_send(message)` in
   the session context, so external automation can invoke the round-trip; the
   `session_id` is filled from scope, like the built-in `paste`.

**Client side:** [`tests/claude_send`](../tests/claude_send) — a small CLI over the
iTerm2 API that invokes `claude_send` in a session and prints the reply. Focus the
claude tab (or pass `--session`) and run it.

## Reproducing the bake-off

```sh
python3 -m venv tmp/msgbakeoff-venv
tmp/msgbakeoff-venv/bin/pip install pyte           # for the SCRAPE method
tmp/msgbakeoff-venv/bin/python tests/claude_message_bakeoff.py --probe       # one round-trip, de-risk
tmp/msgbakeoff-venv/bin/python tests/claude_message_bakeoff.py --corpus --json tmp/claude-msg-bakeoff.json
```

`--corpus` prints the table above and writes the JSON. The harness launches claude
in a trusted directory (the repo root by default; override with `--cwd`) so it
boots straight to the prompt without the workspace-trust dialog.

## Limitations / future

- **Session binding** is by matching the exact message text in the most recently
  written transcript (robust across tabs and however the tab was launched). Two
  tabs sent the identical message at the same instant would be ambiguous — rare; a
  per-tab session-id marker (claude’s `--session-id`) is the hardening.
- **Submit** depends on two things claude’s TUI does today: a lone carriage
  return reads as Enter (so the in-app send writes the CR as its own write, a beat
  after the text), and bracketed paste keeps multi-line input from submitting at
  its own newlines. A change to either would need a different submit strategy. (The
  bake-off harness already wrote the text and the CR as separate writes, which is
  why it didn’t surface this — the in-app path had to match it.)
- **Permission prompts.** Mid-turn, claude’s `PermissionRequest` hook now drives an
  amber `waiting` state. The round-trip ignores it for the working→idle edge (so a
  send that hits a permission prompt waits correctly through to the eventual idle),
  and a new send is refused while the agent is working *or* waiting — injected text
  wouldn’t answer the prompt anyway.
- **No programmatic way to open a claude tab yet.** The working/idle FIFO is wired
  only by the sparkle (Claude) button, so a claude tab can’t be created from
  AppleScript / the API the way a hermes tab can (hermes is in-band). A built-in
  function / menu action that opens a claude tab with the FIFO wired is the
  obvious follow-up.
- **Large transcripts.** The reader scans whole transcript files touched since the
  send; a cheap `"user"` pre-filter keeps this fast, and the reply usually lands on
  the first poll. A byte-offset tail read is the optimization if it ever matters.
- **Window restoration** is now moot for a claude tab: an ✦-launched agent tab is
  excluded from saved arrangements and macOS system restoration (it can’t be
  meaningfully restored — agent gone, FIFO path stale), so there is no half-restored
  tab with a missing messenger to worry about.
