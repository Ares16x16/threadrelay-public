#import <AppKit/AppKit.h>

// Process-lifetime observer, registered on the AppKit setup thread. The host
// callback only revokes/schedules generation cleanup and never blocks AppKit.
@interface ThreadRelayPowerObserver : NSObject
@property(nonatomic, assign) void *context;
@property(nonatomic, assign) void (*callback)(void *, int);
- (void)sleep:(NSNotification *)notification;
- (void)wake:(NSNotification *)notification;
@end
@implementation ThreadRelayPowerObserver
- (void)sleep:(NSNotification *)notification { (void)notification; self.callback(self.context, 1); }
- (void)wake:(NSNotification *)notification { (void)notification; self.callback(self.context, 0); }
@end
int threadrelay_register_power(void *context, void (*callback)(void *, int)) {
    if (!context || !callback || ![NSThread isMainThread]) return 0;
    NSNotificationCenter *center = [[NSWorkspace sharedWorkspace] notificationCenter];
    if (!center) return 0;
    ThreadRelayPowerObserver *observer = [ThreadRelayPowerObserver new];
    if (!observer) return 0;
    observer.context = context; observer.callback = callback;
    [center addObserver:observer selector:@selector(sleep:) name:NSWorkspaceWillSleepNotification object:nil];
    [center addObserver:observer selector:@selector(wake:) name:NSWorkspaceDidWakeNotification object:nil];
    return 1;
}
