//
//  iTermHermesMessenger.h
//  iTerm2SharedARC
//
//  (Fork) Drives one programmatic message round-trip against a live hermes
//  session. The owner (PTYSession) injects the message into the pty and forwards
//  the agent’s working/idle transitions here; this object reads the reply back
//  out of band from hermes’s own state.db once the turn finishes — so the live
//  interactive tab is never scraped or disturbed.
//
//  Why state.db: a head-to-head bake-off (tests/hermes_message_bakeoff.py,
//  docs/hermes-messaging.md) scored reading the reply from state.db at fidelity
//  1.0 across the corpus, vs ~0.13 (screen scrape) / ~0.03 (raw stream) for the
//  terminal-mediated alternatives, which drown the reply in TUI repaint chrome.
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef void (^iTermHermesReplyCompletion)(NSString *_Nullable reply,
                                           NSError *_Nullable error);

@interface iTermHermesMessenger : NSObject

// Path to hermes’s state.db (typically ~/.hermes/state.db).
- (instancetype)initWithDatabasePath:(NSString *)databasePath NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

// YES while a round-trip is in flight.
@property (nonatomic, readonly, getter=isBusy) BOOL busy;

// Begin a round-trip. Returns NO (and does not call completion) if a send is
// already in flight or the agent is currently working. On YES the caller must
// then write `message` to the session and submit it; once the agent goes idle
// this object resolves the reply from state.db and invokes completion on the
// main queue.
- (BOOL)beginSendingMessage:(NSString *)message
                 completion:(iTermHermesReplyCompletion)completion;

// Forward each working|idle agent-state transition (the raw OSC vocabulary that
// already drives the tab dot). Must be called on the main queue.
- (void)agentStateDidChange:(NSString *)state;

@end

NS_ASSUME_NONNULL_END
