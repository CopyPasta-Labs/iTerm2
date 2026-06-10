//
//  iTermHermesMessenger.m
//  iTerm2SharedARC
//
//  (Fork) See iTermHermesMessenger.h.
//

#import "iTermHermesMessenger.h"

#import "DebugLogging.h"
#import "FMDatabase.h"

#import <sqlite3.h>  // SQLITE_OPEN_READONLY (FMDatabase.h does not re-export it)

static NSString *const iTermHermesErrorDomain = @"com.copypastalabs.iterm2.hermes";

// state.db is written a beat after the idle edge for large replies; poll briefly.
static const NSTimeInterval iTermHermesReplyDBPollTimeout = 15.0;
// Safety net so a missed idle edge can’t strand the caller forever.
static const NSTimeInterval iTermHermesRoundTripTimeout = 240.0;

@implementation iTermHermesMessenger {
    NSString *_databasePath;
    dispatch_queue_t _dbQueue;

    // Round-trip state — touched only on the main queue.
    NSString *_pendingMessage;
    NSTimeInterval _sendEpoch;
    BOOL _sawWorking;
    NSString *_lastState;
    iTermHermesReplyCompletion _completion;
    id _inFlightToken;  // identifies the current round-trip for the timeout
}

- (instancetype)initWithDatabasePath:(NSString *)databasePath {
    self = [super init];
    if (self) {
        _databasePath = [databasePath copy];
        _dbQueue = dispatch_queue_create("com.copypastalabs.iterm2.hermes.db",
                                         DISPATCH_QUEUE_SERIAL);
    }
    return self;
}

- (BOOL)isBusy {
    return _completion != nil;
}

- (BOOL)beginSendingMessage:(NSString *)message
                 completion:(iTermHermesReplyCompletion)completion {
    if (_completion != nil) {
        DLog(@"hermes: refused send — a round-trip is already in flight");
        return NO;
    }
    if ([_lastState isEqualToString:@"working"]) {
        DLog(@"hermes: refused send — the agent is mid-turn");
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
                                 (int64_t)(iTermHermesRoundTripTimeout * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        __typeof(self) strongSelf = weakSelf;
        if (strongSelf != nil && strongSelf->_inFlightToken == token) {
            [strongSelf finishWithReply:nil
                                  error:[strongSelf errorWithMessage:@"Timed out waiting for the hermes reply"]];
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
    NSString *dbPath = _databasePath;
    dispatch_async(_dbQueue, ^{
        NSString *reply = [self pollForReplyToMessage:message since:epoch databasePath:dbPath];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self->_completion == nil) {
                return;  // already delivered (e.g. via timeout) or cancelled
            }
            if (reply.length > 0) {
                [self finishWithReply:reply error:nil];
            } else {
                [self finishWithReply:nil
                                error:[self errorWithMessage:@"hermes went idle but no reply was found in state.db"]];
            }
        });
    });
}

// Runs on _dbQueue.
- (NSString *)pollForReplyToMessage:(NSString *)message
                              since:(NSTimeInterval)epoch
                       databasePath:(NSString *)dbPath {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:iTermHermesReplyDBPollTimeout];
    while ([deadline timeIntervalSinceNow] > 0) {
        NSString *reply = [self readReplyToMessage:message since:epoch databasePath:dbPath];
        if (reply.length > 0) {
            return reply;
        }
        [NSThread sleepForTimeInterval:0.2];
    }
    return [self readReplyToMessage:message since:epoch databasePath:dbPath];
}

// Runs on _dbQueue. One read-only pass: find the session that received our exact
// message, then concatenate the assistant text that followed it in that session.
- (NSString *)readReplyToMessage:(NSString *)message
                           since:(NSTimeInterval)epoch
                    databasePath:(NSString *)dbPath {
    FMDatabase *db = [FMDatabase databaseWithPath:dbPath];
    if (![db openWithFlags:SQLITE_OPEN_READONLY]) {
        return nil;
    }
    NSString *sessionID = nil;
    long long userRowID = 0;
    FMResultSet *rs =
        [db executeQuery:@"SELECT id, session_id FROM messages "
                         @"WHERE role='user' AND content=? AND timestamp>=? "
                         @"ORDER BY id DESC LIMIT 1"
            withArgumentsInArray:@[message, @(epoch - 2.0)]];
    if ([rs next]) {
        userRowID = [rs longLongIntForColumn:@"id"];
        sessionID = [rs stringForColumn:@"session_id"];
    }
    [rs close];
    if (sessionID == nil) {
        [db close];
        return nil;
    }
    NSMutableArray<NSString *> *parts = [NSMutableArray array];
    FMResultSet *rs2 =
        [db executeQuery:@"SELECT content FROM messages "
                         @"WHERE session_id=? AND id>? AND role='assistant' "
                         @"AND content IS NOT NULL AND length(content)>0 "
                         @"ORDER BY id"
            withArgumentsInArray:@[sessionID, @(userRowID)]];
    while ([rs2 next]) {
        NSString *content = [rs2 stringForColumn:@"content"];
        if (content.length > 0) {
            [parts addObject:content];
        }
    }
    [rs2 close];
    [db close];
    return [parts componentsJoinedByString:@"\n"];
}

// Runs on the main queue.
- (void)finishWithReply:(NSString *)reply error:(NSError *)error {
    iTermHermesReplyCompletion completion = _completion;
    _completion = nil;
    _inFlightToken = nil;
    _pendingMessage = nil;
    _sawWorking = NO;
    if (completion != nil) {
        completion(reply, error);
    }
}

- (NSError *)errorWithMessage:(NSString *)message {
    return [NSError errorWithDomain:iTermHermesErrorDomain
                               code:1
                           userInfo:@{ NSLocalizedDescriptionKey: message }];
}

@end
