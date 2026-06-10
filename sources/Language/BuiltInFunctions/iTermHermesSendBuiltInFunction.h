//
//  iTermHermesSendBuiltInFunction.h
//  iTerm2SharedARC
//
//  (Fork) Exposes iterm2.hermes_send(message) so external automation (and the
//  tests/hermes_send CLI) can send a message to a hermes agent running in a
//  session and get its reply back. The reply is read from hermes’s state.db once
//  the agent goes idle; the live interactive tab is driven, never replaced.
//

#import <Foundation/Foundation.h>

#import "iTermBuiltInFunctions.h"

NS_ASSUME_NONNULL_BEGIN

@interface iTermHermesSendBuiltInFunction : NSObject<iTermBuiltInFunction>
@end

NS_ASSUME_NONNULL_END
