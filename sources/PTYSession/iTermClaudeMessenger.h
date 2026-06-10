//
//  iTermClaudeMessenger.h
//  iTerm2SharedARC
//
//  (Fork) An iTermAgentMessenger that reads the reply from claude-code’s own JSONL
//  transcript once the turn finishes — so the live interactive tab is never
//  scraped. Claude has no SQLite store like hermes; it appends every turn to
//  ~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl (ANSI-free, the canonical
//  record of what it said).
//
//  Why the transcript: a head-to-head bake-off (tests/claude_message_bakeoff.py,
//  docs/claude-messaging.md) scored reading the reply from the transcript at
//  fidelity 1.0 across the corpus, vs the terminal-scrape alternatives, which
//  drown the reply in the Ink TUI’s repaint chrome.
//

#import "iTermAgentMessenger.h"

NS_ASSUME_NONNULL_BEGIN

@interface iTermClaudeMessenger : iTermAgentMessenger

// Root of claude’s per-project transcript directories (typically
// ~/.claude/projects).
- (instancetype)initWithProjectsRoot:(NSString *)projectsRoot NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

@end

NS_ASSUME_NONNULL_END
