//
//  iTermClaudeMessenger.m
//  iTerm2SharedARC
//
//  (Fork) See iTermClaudeMessenger.h. Only the reply read is claude-specific; the
//  round-trip orchestration lives in iTermAgentMessenger. The store is read-only:
//  this never writes anything under ~/.claude.
//

#import "iTermClaudeMessenger.h"

#import "DebugLogging.h"

#import <math.h>  // NAN, isnan

// Files modified before (send time − slack) can’t hold this turn; skip them.
static const NSTimeInterval iTermClaudeTranscriptSlack = 5.0;

// The "content" of a transcript line object (message.content), or nil. A genuine
// user prompt has a string content; tool-result user lines have an array.
static id iTermClaudeContentOf(NSDictionary *lineObject) {
    id message = lineObject[@"message"];
    if (![message isKindOfClass:[NSDictionary class]]) {
        return nil;
    }
    return ((NSDictionary *)message)[@"content"];
}

@implementation iTermClaudeMessenger {
    NSString *_projectsRoot;
}

- (instancetype)initWithProjectsRoot:(NSString *)projectsRoot {
    self = [super init];
    if (self) {
        _projectsRoot = [projectsRoot copy];
    }
    return self;
}

- (NSString *)agentName {
    return @"claude";
}

// Runs on the base’s read queue. Find the transcript that received our exact
// message and concatenate the assistant text that followed it. claude appends
// every turn to ~/.claude/projects/<munged-cwd>/<uuid>.jsonl, one JSON object per
// line; the reply is the text blocks of the assistant lines after our prompt.
- (NSString *)readReplyToMessage:(NSString *)message
                           since:(NSTimeInterval)epoch {
    const NSTimeInterval cutoff = epoch - iTermClaudeTranscriptSlack;
    for (NSString *path in [self recentTranscriptPathsSince:cutoff]) {
        NSString *reply = [self replyInTranscript:path message:message minEpoch:cutoff];
        if (reply.length > 0) {
            return reply;
        }
    }
    return nil;
}

// project-dir/<uuid>.jsonl files touched at/after `cutoff`, newest first. The
// single directory level excludes subagent transcripts (one level deeper).
- (NSArray<NSString *> *)recentTranscriptPathsSince:(NSTimeInterval)cutoff {
    NSFileManager *fm = [NSFileManager defaultManager];
    NSArray<NSString *> *projectDirs = [fm contentsOfDirectoryAtPath:_projectsRoot error:nil];
    if (projectDirs == nil) {
        return @[];
    }
    NSMutableArray<NSArray *> *entries = [NSMutableArray array];  // @[ @(mtime), path ]
    for (NSString *project in projectDirs) {
        NSString *projectPath = [_projectsRoot stringByAppendingPathComponent:project];
        BOOL isDir = NO;
        if (![fm fileExistsAtPath:projectPath isDirectory:&isDir] || !isDir) {
            continue;
        }
        NSArray<NSString *> *files = [fm contentsOfDirectoryAtPath:projectPath error:nil];
        for (NSString *file in files) {
            if (![file hasSuffix:@".jsonl"]) {
                continue;
            }
            NSString *full = [projectPath stringByAppendingPathComponent:file];
            NSDate *mtime = [fm attributesOfItemAtPath:full error:nil][NSFileModificationDate];
            if (mtime == nil) {
                continue;
            }
            const NSTimeInterval mt = [mtime timeIntervalSince1970];
            if (mt >= cutoff) {
                [entries addObject:@[@(mt), full]];
            }
        }
    }
    [entries sortUsingComparator:^NSComparisonResult(NSArray *a, NSArray *b) {
        return [b[0] compare:a[0]];  // mtime descending
    }];
    NSMutableArray<NSString *> *paths = [NSMutableArray array];
    for (NSArray *entry in entries) {
        [paths addObject:entry[1]];
    }
    return paths;
}

// In one transcript: find the (most recent) genuine user prompt whose string
// content == message, then concatenate the text blocks of the assistant lines
// that follow it, stopping at the next genuine user prompt. Tool-result user
// lines and thinking/tool_use blocks are skipped.
- (NSString *)replyInTranscript:(NSString *)path
                        message:(NSString *)message
                       minEpoch:(NSTimeInterval)minEpoch {
    NSString *contents = [NSString stringWithContentsOfFile:path
                                                   encoding:NSUTF8StringEncoding
                                                      error:nil];
    if (contents.length == 0) {
        return nil;
    }
    NSArray<NSString *> *lines = [contents componentsSeparatedByString:@"\n"];
    NSInteger found = -1;
    for (NSInteger i = 0; i < (NSInteger)lines.count; i++) {
        NSString *line = lines[i];
        // Cheap pre-filter: only user-type lines can match. Avoids JSON-parsing
        // the assistant/attachment lines that dominate a long transcript.
        if (![line containsString:@"\"user\""]) {
            continue;
        }
        NSDictionary *object = [self objectForLine:line];
        if (![object[@"type"] isEqual:@"user"]) {
            continue;
        }
        id content = iTermClaudeContentOf(object);
        if (![content isKindOfClass:[NSString class]] ||
            ![(NSString *)content isEqualToString:message]) {
            continue;
        }
        const NSTimeInterval ts = [self epochForTimestamp:object[@"timestamp"]];
        if (!isnan(ts) && ts < minEpoch) {
            continue;
        }
        found = i;  // keep the last (most recent) match
    }
    if (found < 0) {
        return nil;
    }
    NSMutableArray<NSString *> *parts = [NSMutableArray array];
    for (NSInteger i = found + 1; i < (NSInteger)lines.count; i++) {
        NSDictionary *object = [self objectForLine:lines[i]];
        if (object == nil) {
            continue;
        }
        id type = object[@"type"];
        id content = iTermClaudeContentOf(object);
        if ([type isEqual:@"user"]) {
            if ([content isKindOfClass:[NSString class]]) {
                break;  // next genuine prompt — turn boundary
            }
            continue;  // tool-result — keep scanning
        }
        if ([type isEqual:@"assistant"] && [content isKindOfClass:[NSArray class]]) {
            for (id block in (NSArray *)content) {
                if (![block isKindOfClass:[NSDictionary class]] ||
                    ![block[@"type"] isEqual:@"text"]) {
                    continue;
                }
                NSString *text = block[@"text"];
                if ([text isKindOfClass:[NSString class]] && text.length > 0) {
                    [parts addObject:text];
                }
            }
        }
    }
    NSString *joined = [parts componentsJoinedByString:@"\n"];
    return [joined stringByTrimmingCharactersInSet:
               [NSCharacterSet whitespaceAndNewlineCharacterSet]];
}

- (NSDictionary *)objectForLine:(NSString *)line {
    NSString *trimmed = [line stringByTrimmingCharactersInSet:
                            [NSCharacterSet whitespaceCharacterSet]];
    if (trimmed.length == 0) {
        return nil;
    }
    NSData *data = [trimmed dataUsingEncoding:NSUTF8StringEncoding];
    if (data == nil) {
        return nil;
    }
    id object = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [object isKindOfClass:[NSDictionary class]] ? object : nil;
}

// Parse an ISO-8601 timestamp (e.g. 2026-06-10T08:29:51.182Z) to unix time, or
// NAN if absent/unparseable (callers then don’t filter on it).
- (NSTimeInterval)epochForTimestamp:(id)timestamp {
    if (![timestamp isKindOfClass:[NSString class]]) {
        return NAN;
    }
    static NSISO8601DateFormatter *withFractional;
    static NSISO8601DateFormatter *withoutFractional;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        withFractional = [[NSISO8601DateFormatter alloc] init];
        withFractional.formatOptions = (NSISO8601DateFormatWithInternetDateTime |
                                        NSISO8601DateFormatWithFractionalSeconds);
        withoutFractional = [[NSISO8601DateFormatter alloc] init];
        withoutFractional.formatOptions = NSISO8601DateFormatWithInternetDateTime;
    });
    NSDate *date = [withFractional dateFromString:timestamp];
    if (date == nil) {
        date = [withoutFractional dateFromString:timestamp];
    }
    return date ? [date timeIntervalSince1970] : NAN;
}

@end
