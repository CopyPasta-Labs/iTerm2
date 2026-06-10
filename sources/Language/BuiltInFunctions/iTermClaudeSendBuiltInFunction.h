//
//  iTermClaudeSendBuiltInFunction.h
//  iTerm2SharedARC
//
//  (Fork) Exposes iterm2.claude_send(message) so external automation (and the
//  tests/claude_send CLI) can send a message to a claude-code agent running in a
//  session and get its reply back. The reply is read from claude’s JSONL
//  transcript once the agent goes idle; the live interactive tab is driven, never
//  replaced.
//

#import <Foundation/Foundation.h>

#import "iTermBuiltInFunctions.h"

NS_ASSUME_NONNULL_BEGIN

@interface iTermClaudeSendBuiltInFunction : NSObject<iTermBuiltInFunction>
@end

NS_ASSUME_NONNULL_END
