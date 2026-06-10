# Sending to and receiving from a hermes tab — methods, KPIs, and results

*How this iTerm2 fork lets you send a message to a hermes agent running in a tab
and get its reply back programmatically, every capture method that was tried, and
the empirical bake-off that decided it.*

This is the messaging companion to [`hermes-state-detection.md`](hermes-state-detection.md).
Read that first: it established the in-band `OSC 1337 ; HermesState=working|idle`
edge that this feature reuses as its “the turn is finished” trigger.

---

## TL;DR (the verdict)

Drive the **live, interactive** hermes tab: inject the message into its pty
exactly as if typed (so a human can still watch, type, and interrupt), wait for
the agent’s own working→idle edge, then read the reply back **out of band from
hermes’s `state.db`** — never by scraping the terminal.

In a head-to-head bake-off over a 6-scenario message corpus, reading the reply
from `state.db` scored **mean fidelity 1.00 and 6/6 known-answer matches**, with
the reply available within milliseconds of the idle edge. Every method that
recovers the reply *from the terminal* lost badly — screen-scrape **0.13**, raw
output-stream **0.03** — because hermes is a `prompt_toolkit` TUI that repaints
the whole viewport every frame, so the reply arrives buried in the welcome
banner, the echoed prompt, box-drawing rules, spinners, and a `⚕ gpt-5.5 │ … │ 7%`
status bar. A fresh `hermes chat -q` one-shot is exact but is disqualified by
construction: it spawns a new ~8 s process and abandons the interactive tab.

---

## The problem

The fork manages agentic CLIs, one per tab. Beyond *showing* whether a hermes tab
is busy (the red/green dot), we want to **talk to it programmatically**: send it a
message and reliably get its reply text back — without tearing down the live
session the user is interacting with.

Sending is easy and already interactive-safe: write the bytes to the pty
(`-[PTYSession writeTaskNoBroadcast:]`), exactly as if typed. The hard half is
**capture**: once the turn ends, what did hermes actually *say*? Two facts make
this awkward to read off the screen:

1. **hermes is a full-redraw TUI.** `prompt_toolkit` repaints the visible region
   on every render, so the byte stream between “send” and “idle” is dominated by
   cursor moves, repeated frames, spinners, a live token/▒-bar status line, and
   the echoed prompt — not a clean transcript.
2. **The reply can scroll.** A long answer moves through the viewport; whatever a
   scrape sees depends on timing and window size.

Knowing *when* the turn ends is already solved — hermes emits
`OSC 1337 ; HermesState=idle` at its real `_agent_running` flip, scored
100 %/0 fp/0 fn in the state bake-off. This feature reuses that edge and only has
to answer “what was said.”

## KPIs

A capture method is judged on:

| # | KPI | What good looks like |
|---|-----|----------------------|
| 1 | **Fidelity** | captured text equals what hermes actually said (vs an oracle / a known answer) |
| 2 | **Completeness** | no truncation of long, multi-line, or streamed replies |
| 3 | **Round-trip success** | the right reply is bound to the right send |
| 4 | **Latency** | reply available soon after the idle edge |
| 5 | **Interactive-safe** | the live tab keeps working; capture is invisible and read-only |
| 6 | **Context preservation** | the reply reflects the tab’s ongoing conversation, not a fresh session |
| 7 | **Robustness to updates** | survives TUI/`hermes update` churn; not bound to fragile rendering |

## Methods evaluated

Send is identical for all (write to the pty). They differ only in capture:

| Key | Method | Mechanism |
|-----|--------|-----------|
| **STATEDB** | state.db read *(shipped)* | after the idle edge, read the `role='assistant'` rows hermes appended to `~/.hermes/state.db` after our message |
| **SCRAPE** | screen scrape | render the pty byte window through a terminal emulator (pyte) and read the display grid, minus prompt/echo chrome |
| **STREAM** | raw output strip | strip ANSI/control from the raw `send..idle` byte window |
| **ONESHOT** | fresh one-shot *(reference)* | spawn `hermes chat -q <msg> -Q` — **not** the live tab |

## The bake-off

**Harness:** [`tests/hermes_message_bakeoff.py`](../tests/hermes_message_bakeoff.py)
forks the state bake-off’s pty driver: it launches `hermes --cli` under a pseudo-
terminal, sends each message on one live session, reuses the OSC working→idle edge
as the “reply complete” trigger, and captures the reply four ways on the same
wall clock.

**Oracle.** `state.db` is hermes’s own canonical record of what it said, so STATEDB
is ≈ the oracle by construction — stated plainly here. Scenarios **M1/M4/M5b/M6**
also use **known-answer** prompts whose expected reply we control (e.g. “reply
with exactly `ZQ7-DELTA`”), an oracle-*independent* check every method — STATEDB
included — must pass.

**Message corpus:**

- **M1** short, known-answer (`ZQ7-DELTA`)
- **M2** long streamed reply (~400 words) — the truncation killer
- **M3** fenced code block containing ANSI escapes — the de-ANSI killer
- **M4** 12-line verbatim echo, known-answer — exact multi-line fidelity
- **M5a/M5b** remember a code word, then recall it — the **live-tab / context** test
- **M6a/M6b** two quick turns — correct turn-binding (`ALPHA-ONE` vs `BETA-TWO`)

### Results (mean over the corpus; higher fidelity is better)

| Method | mean fidelity vs oracle | known-answer match | mean capture latency | drives the live tab? |
|--------|:--:|:--:|:--:|:--:|
| **STATEDB** | **1.00** | **6/6** | ~0 s (one 4.7 s DB-write lag) | ✅ read-only, invisible |
| SCRAPE | 0.13 | 5/6 | 0.07 s | ✅ but unusable |
| STREAM | 0.03 | 5/6 | 0.00 s | ✅ but unusable |
| ONESHOT | 1.00 | 3/3 | 8.1 s | ❌ fresh process, not the tab |

Per-scenario fidelity (STATEDB / SCRAPE / STREAM):

| Scenario | STATEDB | SCRAPE | STREAM |
|----------|:--:|:--:|:--:|
| M1 short | **1.00** | 0.019 | 0.002 |
| M2 long stream | **1.00** | 0.816 | 0.244 |
| M3 code+ANSI | **1.00** | 0.122 | 0.012 |
| M4 verbatim echo | **1.00** (known ✓) | 0.011 (known ✗) | 0.001 (known ✗) |
| M5b context recall | **1.00** (known ✓) | 0.043 (known ✓) | 0.005 (known ✓) |

### Reading the table

- **STATEDB** is exact everywhere, including the 12-line verbatim echo (M4) and the
  context recall (M5b: it pulled `PURPLE-WALRUS-42` out of the **live** session,
  proving the reply reflects the tab’s ongoing conversation). M6a/M6b confirm
  turn-binding: each send’s reply is isolated by a `MAX(id)` watermark / by
  matching the exact message text.
- **SCRAPE/STREAM** *contain* the answer for short prompts (so they can pass a
  loose known-answer substring check) but their fidelity collapses because they
  also capture the banner, the `● <your echoed prompt>` line, `────` rules, and
  the `⚕ gpt-5.5 │ 18.1K/272K │ [█░░░] 7% │ 33s` status bar. On the 12-line echo
  (M4) both **failed** the known-answer outright — the reply was lost in repaint
  noise. SCRAPE’s best case (0.82 on the long prose M2) is still far from usable.
- **ONESHOT** ties STATEDB on fidelity but is disqualified by KPIs 5–6: it spawns
  a fresh ~8 s process and does not talk to the live tab. (It even answered the
  M5b context question — evidence it implicitly continues/observes recent session
  state, which is exactly the silent cross-talk we want to avoid.)

## Why STATEDB wins on every KPI

1. **Fidelity / completeness** — it reads hermes’s own verbatim record; 1.00 across
   the corpus, no truncation, no ANSI, including long and multi-line replies.
2. **Round-trip success** — the reply is bound to the send by matching our exact
   message text and taking the assistant rows that follow it in that session.
3. **Latency** — the row is present within milliseconds of the idle edge (we poll
   briefly; the only lag observed was 4.7 s for the large multi-line echo).
4. **Interactive-safe** — the message is injected into the live pty; the read is a
   read-only `SQLITE_OPEN_READONLY` open of `state.db`. The terminal is never
   touched for capture, so a human can keep using the tab.
5. **Context preservation** — it is the live session, so the conversation carries.
6. **Robustness** — it does not depend on the TUI’s rendering, only on hermes’s
   stable `messages(session_id, role, content, id)` schema; a TUI/skin/spinner
   change cannot break it.

The trade is coupling to hermes’s `state.db` schema. That is acceptable here
(this feature is hermes-only for now); a terminal-mediated fallback (SCRAPE) would
be the path to generalize to other agents, at the fidelity cost shown above.

## What was built

**iTerm side** (the feature is owned by iTerm2):

1. `iTermHermesMessenger` (`sources/PTYSession/`) — orchestrates one round-trip:
   on `beginSendingMessage:` it records the send time; it watches the
   working→idle transitions forwarded from `screenSetAgentState:`; on idle it
   reads `state.db` (read-only, via FMDB) for the assistant rows that followed our
   exact message, polling briefly for the row to land, then calls back on the main
   queue. A 240 s safety timeout can never strand the caller.
2. `-[PTYSession sendHermesMessage:completion:]` — lazily owns the messenger,
   injects the message into the live pty (`writeTaskNoBroadcast:`; multi-line
   messages are bracketed-pasted so `prompt_toolkit` doesn’t submit at the first
   newline), and returns the reply.
3. `iTermHermesSendBuiltInFunction` — registers `iterm2.hermes_send(message)` in
   the session context, so external automation can invoke the round-trip; the
   `session_id` is filled from scope, like the built-in `paste`.

**Client side:** [`tests/hermes_send`](../tests/hermes_send) — a small CLI over the
iTerm2 API that invokes `hermes_send` in a session and prints the reply. Focus the
hermes tab (or pass `--session`) and run it.

## Reproducing the bake-off

```sh
python3 -m venv tmp/msgbakeoff-venv
tmp/msgbakeoff-venv/bin/pip install pyte           # for the SCRAPE method
tmp/msgbakeoff-venv/bin/python tests/hermes_message_bakeoff.py --probe       # one round-trip, de-risk
tmp/msgbakeoff-venv/bin/python tests/hermes_message_bakeoff.py --corpus --json tmp/msg-bakeoff.json
```

`--corpus` prints the table above and writes the JSON.

## Limitations / future

- **Session binding** is by matching the exact message text (robust across tabs and
  however the tab was launched). Two tabs sent the identical message at the same
  instant would be ambiguous — rare; a per-tab session marker is the hardening.
- **Multi-line submission** relies on `prompt_toolkit` honoring bracketed paste;
  if a future hermes disables it, send would need a different submit strategy.
- **hermes-only.** Other agents (e.g. claude) have no `state.db`; generalizing
  means the SCRAPE fallback (and its fidelity cost) or an agent-specific store.
- **Window restoration** of a hermes tab is the same follow-up class as elsewhere:
  the messenger is per-session and not re-created on restore.
