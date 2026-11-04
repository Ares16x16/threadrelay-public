#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>

// Called only in Tauri's with_webview/main-thread callbacks. Retention covers
// the close queue/fence; it does not grant an independent browsing authority.
int tr_embedded_visible(void *raw) {
    if (!raw || ![NSThread isMainThread]) return 0;
    NSView *view=(NSView *)raw; NSWindow *window=[view window];
    return window && [window isVisible] && ![window isMiniaturized] &&
        ![view isHiddenOrHasHiddenAncestor] && !NSIsEmptyRect([view visibleRect]);
}
void *tr_embedded_retain(void *raw) {
    if (!raw || ![NSThread isMainThread]) return NULL;
    return [(NSView *)raw retain];
}
int tr_embedded_detached_release(void *raw) {
    if (!raw || ![NSThread isMainThread]) return 0;
    NSView *view=(NSView *)raw;
    int detached=[view superview]==nil && [view window]==nil;
    [view release]; return detached;
}
int tr_embedded_distinct_store(void *mainRaw,void *childRaw) {
    if (!mainRaw || !childRaw || ![NSThread isMainThread]) return 0;
    if (@available(macOS 14.0,*)) {
        NSUUID *mainID=[[(WKWebView *)mainRaw configuration] websiteDataStore].identifier;
        NSUUID *childID=[[(WKWebView *)childRaw configuration] websiteDataStore].identifier;
        return childID && (!mainID || ![childID isEqual:mainID]);
    }
    return 0;
}

// A visibility ACK observes the child itself, independent of window minimize.
int tr_embedded_set_visible(void *raw,int show) {
    if (!raw || ![NSThread isMainThread]) return 0;
    NSView *view=(NSView *)raw;
    if (![view superview] || ![view window]) return 0;
    if (show) [[view superview] addSubview:view positioned:NSWindowAbove relativeTo:nil];
    [view setHidden:!show];
    if (!show) return [view isHidden];
    if (tr_embedded_visible(raw)) return 1;
    // A failed Show ACK must leave the child hidden, including minimize or
    // ancestor-visibility races. It can be retried after the viewport returns.
    [view setHidden:YES];
    return 0;
}

// Existing capture can continue while an internal page hides its child view.
int tr_embedded_attached(void *raw) {
    if (!raw || ![NSThread isMainThread]) return 0;
    NSView *view=(NSView *)raw; NSWindow *window=[view window];
    NSRect frame=[view frame];
    return [view superview] && window && [window isVisible] && ![window isMiniaturized] &&
        frame.size.width>0 && frame.size.height>0;
}

// Put the owned view behind the app page without suspending WebKit's rendering
// clock. The main app webview remains above it and receives all interaction.
int tr_embedded_set_background(void *raw) {
    if (!raw || ![NSThread isMainThread]) return 0;
    NSView *view=(NSView *)raw; NSView *parent=[view superview];
    if (!parent || [[parent subviews] count]<2 || ![view window]) return 0;
    WKWebView *mainView=nil;
    for (NSView *sibling in [[parent subviews] reverseObjectEnumerator]) {
        if (sibling!=view && [sibling isKindOfClass:[WKWebView class]]) { mainView=(WKWebView *)sibling; break; }
    }
    if (!mainView) return 0;
    [parent addSubview:view positioned:NSWindowBelow relativeTo:nil];
    [view setHidden:NO];
    NSRect bounds=[view bounds];
    NSPoint point=[view convertPoint:NSMakePoint(NSMidX(bounds),NSMidY(bounds)) toView:[parent superview]];
    NSView *hit=[parent hitTest:point];
    if (!hit || hit==view || [hit isDescendantOf:view]) return 0;
    if (![[view window] makeFirstResponder:mainView]) return 0;
    return [[parent subviews] firstObject]==view && [[parent subviews] count]>=2 && tr_embedded_attached(raw);
}
