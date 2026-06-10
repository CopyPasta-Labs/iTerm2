//
//  iTermAgentMessenger.h
//  iTerm2SharedARC
//
//  (Fork) Drives one programmatic message round-trip against a live agentic-CLI
//  session (hermes, claude, …). The owner (PTYSession) injects the message into
//  the pty and forwards the agent’s working/idle transitions here; once the turn
//  finishes this object reads the reply back out of band — so the live
//  interactive tab is never scraped or disturbed.
//
//  The round-trip orchestration (begin → wait for the working→idle edge → poll →
//  deliver) is identical across agents; only WHERE the reply text is read differs.
//  This is an abstract base: subclasses override -readReplyToMessage:since: (and
//  -agentName). See iTermHermesMessenger (reads hermes’s state.db) and
//  iTermClaudeMessenger (reads claude’s JSONL transcript).
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef void (^iTermAgentReplyCompletion)(NSString *_Nullable reply,
                                          NSError *_Nullable error);

@interface iTermAgentMessenger : NSObject

// YES while a round-trip is in flight.
@property (nonatomic, readonly, getter=isBusy) BOOL busy;

// Begin a round-trip. Returns NO (and does not call completion) if a send is
// already in flight or the agent is currently working. On YES the caller must
// then write `message` to the session and submit it; once the agent goes idle
// this object resolves the reply and invokes completion on the main queue.
- (BOOL)beginSendingMessage:(NSString *)message
                 completion:(iTermAgentReplyCompletion)completion;

// Forward each working|idle agent-state transition (the vocabulary that already
// drives the tab dot). Must be called on the main queue.
- (void)agentStateDidChange:(NSString *)state;

#pragma mark - For subclasses to override

// Read the reply to `message` (sent at/after `epoch`, unix time) from the agent’s
// own store. Runs on a private serial queue; may return nil/empty before the
// reply has landed (the base polls and retries). Abstract — must be overridden.
- (nullable NSString *)readReplyToMessage:(NSString *)message
                                    since:(NSTimeInterval)epoch;

// Lowercase agent name (e.g. @"hermes"); used in the error domain and messages.
- (NSString *)agentName;

@end

NS_ASSUME_NONNULL_END
