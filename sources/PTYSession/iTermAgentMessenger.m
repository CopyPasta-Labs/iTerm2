//
//  iTermAgentMessenger.m
//  iTerm2SharedARC
//
//  (Fork) See iTermAgentMessenger.h. The round-trip state machine; subclasses
//  supply only the reply read (-readReplyToMessage:since:) and -agentName.
//

#import "iTermAgentMessenger.h"

#import "DebugLogging.h"

// The store is written a beat after the idle edge for large replies; poll briefly.
static const NSTimeInterval iTermAgentReplyPollTimeout = 15.0;
// Safety net so a missed idle edge can’t strand the caller forever.
static const NSTimeInterval iTermAgentRoundTripTimeout = 240.0;

@implementation iTermAgentMessenger {
    dispatch_queue_t _readQueue;

    // Round-trip state — touched only on the main queue.
    NSString *_pendingMessage;
    NSTimeInterval _sendEpoch;
    BOOL _sawWorking;
    NSString *_lastState;
    iTermAgentReplyCompletion _completion;
    id _inFlightToken;  // identifies the current round-trip for the timeout
}

- (instancetype)init {
    self = [super init];
    if (self) {
        _readQueue = dispatch_queue_create("com.copypastalabs.iterm2.agent.read",
                                           DISPATCH_QUEUE_SERIAL);
    }
    return self;
}

- (BOOL)isBusy {
    return _completion != nil;
}

- (BOOL)beginSendingMessage:(NSString *)message
                 completion:(iTermAgentReplyCompletion)completion {
    if (_completion != nil) {
        DLog(@"%@: refused send — a round-trip is already in flight", self.agentName);
        return NO;
    }
    if ([_lastState isEqualToString:@"working"] || [_lastState isEqualToString:@"waiting"]) {
        // working = mid-turn; waiting = blocked on a permission prompt, where
        // injected text would not answer the prompt. Either way, don’t send.
        DLog(@"%@: refused send — the agent is busy (%@)", self.agentName, _lastState);
        return NO;
    }
    _pendingMessage = [message copy];
    _sendEpoch = [[NSDate date] timeIntervalSince1970];
    _sawWorking = NO;
    _completion = [completion copy];

    id token = [NSObject new];
    _inFlightToken = token;
    __weak __typeof(self) weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,
                                 (int64_t)(iTermAgentRoundTripTimeout * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        __typeof(self) strongSelf = weakSelf;
        if (strongSelf != nil && strongSelf->_inFlightToken == token) {
            [strongSelf finishWithReply:nil
                                  error:[strongSelf errorWithMessage:
                                            [NSString stringWithFormat:@"Timed out waiting for the %@ reply",
                                                strongSelf.agentName]]];
        }
    });
    return YES;
}

- (void)agentStateDidChange:(NSString *)state {
    _lastState = [state copy];
    if (_completion == nil) {
        return;
    }
    if ([state isEqualToString:@"working"]) {
        _sawWorking = YES;
        return;
    }
    if ([state isEqualToString:@"idle"] && _sawWorking) {
        [self captureReply];
    }
}

- (void)captureReply {
    NSString *message = _pendingMessage;
    NSTimeInterval epoch = _sendEpoch;
    dispatch_async(_readQueue, ^{
        NSString *reply = [self pollForReplyToMessage:message since:epoch];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self->_completion == nil) {
                return;  // already delivered (e.g. via timeout) or cancelled
            }
            if (reply.length > 0) {
                [self finishWithReply:reply error:nil];
            } else {
                [self finishWithReply:nil
                                error:[self errorWithMessage:
                                          [NSString stringWithFormat:@"%@ went idle but no reply was found",
                                              self.agentName]]];
            }
        });
    });
}

// Runs on _readQueue.
- (NSString *)pollForReplyToMessage:(NSString *)message
                              since:(NSTimeInterval)epoch {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:iTermAgentReplyPollTimeout];
    while ([deadline timeIntervalSinceNow] > 0) {
        NSString *reply = [self readReplyToMessage:message since:epoch];
        if (reply.length > 0) {
            return reply;
        }
        [NSThread sleepForTimeInterval:0.2];
    }
    return [self readReplyToMessage:message since:epoch];
}

// Runs on the main queue.
- (void)finishWithReply:(NSString *)reply error:(NSError *)error {
    iTermAgentReplyCompletion completion = _completion;
    _completion = nil;
    _inFlightToken = nil;
    _pendingMessage = nil;
    _sawWorking = NO;
    if (completion != nil) {
        completion(reply, error);
    }
}

- (NSError *)errorWithMessage:(NSString *)message {
    NSString *domain = [NSString stringWithFormat:@"com.copypastalabs.iterm2.%@", self.agentName];
    return [NSError errorWithDomain:domain
                               code:1
                           userInfo:@{ NSLocalizedDescriptionKey: message }];
}

#pragma mark - Abstract

- (NSString *)readReplyToMessage:(NSString *)message since:(NSTimeInterval)epoch {
    DLog(@"%@: readReplyToMessage:since: not overridden", self.agentName);
    return nil;
}

- (NSString *)agentName {
    return @"agent";
}

@end
