//
//  iTermClaudeSendBuiltInFunction.m
//  iTerm2SharedARC
//
//  (Fork) See iTermClaudeSendBuiltInFunction.h.
//

#import "iTermClaudeSendBuiltInFunction.h"

#import "iTermController.h"
#import "iTermVariables.h"
#import "PTYSession.h"

@implementation iTermClaudeSendBuiltInFunction

+ (void)registerBuiltInFunction {
    iTermBuiltInFunction *func =
    [[iTermBuiltInFunction alloc] initWithName:@"claude_send"
                                     arguments:@{ @"message": [NSString class] }
                             optionalArguments:[NSSet set]
                                 defaultValues:@{ @"session_id": iTermVariableKeySessionID }
                                       context:iTermVariablesSuggestionContextSession
                        sideEffectsPlaceholder:@"[claude_send]"
                                         block:
     ^(NSDictionary *_Nonnull parameters,
       iTermBuiltInFunctionCompletionBlock _Nonnull completion) {
         [self sendMessage:parameters[@"message"]
                 sessionID:parameters[@"session_id"]
                completion:completion];
     }];
    [[iTermBuiltInFunctions sharedInstance] registerFunction:func namespace:@"iterm2"];
}

+ (NSError *)errorWithMessage:(NSString *)message {
    return [NSError errorWithDomain:@"com.copypastalabs.iterm2.claude"
                               code:1
                           userInfo:@{ NSLocalizedDescriptionKey: message }];
}

+ (void)sendMessage:(id)message
          sessionID:(NSString *)sessionID
         completion:(iTermBuiltInFunctionCompletionBlock)completion {
    if (![message isKindOfClass:[NSString class]] || [(NSString *)message length] == 0) {
        completion(nil, [self errorWithMessage:@"claude_send requires a non-empty message"]);
        return;
    }
    PTYSession *session = [[iTermController sharedInstance] sessionWithGUID:sessionID];
    if (session == nil) {
        completion(nil, [self errorWithMessage:@"No such session"]);
        return;
    }
    [session sendClaudeMessage:(NSString *)message
                    completion:^(NSString *_Nullable reply, NSError *_Nullable error) {
        completion(reply, error);
    }];
}

@end
