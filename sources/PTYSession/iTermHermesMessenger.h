//
//  iTermHermesMessenger.h
//  iTerm2SharedARC
//
//  (Fork) An iTermAgentMessenger that reads the reply from hermes’s own state.db
//  once the turn finishes — so the live interactive tab is never scraped.
//
//  Why state.db: a head-to-head bake-off (tests/hermes_message_bakeoff.py,
//  docs/hermes-messaging.md) scored reading the reply from state.db at fidelity
//  1.0 across the corpus, vs ~0.13 (screen scrape) / ~0.03 (raw stream) for the
//  terminal-mediated alternatives, which drown the reply in TUI repaint chrome.
//

#import "iTermAgentMessenger.h"

NS_ASSUME_NONNULL_BEGIN

@interface iTermHermesMessenger : iTermAgentMessenger

// Path to hermes’s state.db (typically ~/.hermes/state.db).
- (instancetype)initWithDatabasePath:(NSString *)databasePath NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

@end

NS_ASSUME_NONNULL_END
