//
//  iTermCommandCenterView.m
//  iTerm2
//
//  (Fork) Placeholder content for the “Command Center” tab — see the header.
//

#import "iTermCommandCenterView.h"

@implementation iTermCommandCenterView

- (BOOL)isOpaque {
    return YES;
}

- (void)setFrameSize:(NSSize)newSize {
    [super setFrameSize:newSize];
    // The face is centered relative to bounds, so redraw whenever resized.
    [self setNeedsDisplay:YES];
}

- (void)drawRect:(NSRect)dirtyRect {
    [[NSColor controlBackgroundColor] setFill];
    NSRectFill(self.bounds);

    NSString *face = @"😀";
    const CGFloat side = MIN(NSWidth(self.bounds), NSHeight(self.bounds));
    NSFont *font = [NSFont systemFontOfSize:MAX(24.0, side * 0.5)];
    NSDictionary<NSAttributedStringKey, id> *attributes = @{ NSFontAttributeName: font };
    const NSSize textSize = [face sizeWithAttributes:attributes];
    const NSPoint origin = NSMakePoint(NSMidX(self.bounds) - textSize.width / 2.0,
                                       NSMidY(self.bounds) - textSize.height / 2.0);
    [face drawAtPoint:origin withAttributes:attributes];
}

@end
