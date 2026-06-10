//
//  iTermHermesMessenger.m
//  iTerm2SharedARC
//
//  (Fork) See iTermHermesMessenger.h. Only the reply read is hermes-specific; the
//  round-trip orchestration lives in iTermAgentMessenger.
//

#import "iTermHermesMessenger.h"

#import "FMDatabase.h"

#import <sqlite3.h>  // SQLITE_OPEN_READONLY (FMDatabase.h does not re-export it)

@implementation iTermHermesMessenger {
    NSString *_databasePath;
}

- (instancetype)initWithDatabasePath:(NSString *)databasePath {
    self = [super init];
    if (self) {
        _databasePath = [databasePath copy];
    }
    return self;
}

- (NSString *)agentName {
    return @"hermes";
}

// Runs on the base’s read queue. One read-only pass: find the session that
// received our exact message, then concatenate the assistant text that followed
// it in that session.
- (NSString *)readReplyToMessage:(NSString *)message
                           since:(NSTimeInterval)epoch {
    FMDatabase *db = [FMDatabase databaseWithPath:_databasePath];
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

@end
