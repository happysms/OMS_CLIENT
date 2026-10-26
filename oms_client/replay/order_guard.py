"""Order admission from observed feed delay; not an RTT or exchange health oracle."""
from collections import deque
from ..market_data.timestamps import timestamp_delta_ms


class FeedDelayGuard:
    def __init__(self,parameters):
        # Missing mode identifies an immutable legacy strategy revision.
        self.enabled=parameters.get('ping_guard_mode','disabled')=='enabled'
        self.maximum=parameters.get('max_feed_delay_ms',500)
        self.spike=parameters.get('feed_delay_spike_ms',250)
        self.resume=parameters.get('feed_delay_resume_ms',200)
        self.stable_ms=parameters.get('feed_delay_stable_ms',1000)
        self.cancel_working=parameters.get('ping_existing_orders','cancel')=='cancel'
        self.last_sample=None;self.delta=None;self.jump=None;self.previous_delta=None
        self.recovery_since=None;self.blocked_since=None
        self.blocked=self.enabled;self.reason='waiting_sample' if self.enabled else 'disabled'
        self.has_sample=False;self.episodes=0;self.events=deque(maxlen=100)
        self.blocked_submissions=0

    def _state(self,blocked,reason,now):
        if self.blocked!=blocked or self.reason!=reason:
            if blocked and (not self.blocked or self.reason=='waiting_sample'):
                self.episodes+=1;self.blocked_since=now
            if not blocked:self.blocked_since=None
            self.blocked=blocked;self.reason=reason
            self.events.append(dict(timestamp=now,blocked=blocked,reason=reason,delta_ms=self.delta,jump_ms=self.jump))

    def observe(self,desktop_ms,server_ms,max_age_ms):
        if not self.enabled:return
        # A feed gap interrupts the required continuous healthy interval.
        gap=self.last_sample is not None and desktop_ms-self.last_sample>max_age_ms
        if gap:
            self.recovery_since=None;self._state(True,'stale_sample',desktop_ms)
        self.last_sample=desktop_ms
        delta=timestamp_delta_ms(desktop_ms,server_ms)
        self.jump=delta-self.previous_delta if delta is not None and self.previous_delta is not None else None
        self.delta=delta
        if delta is not None and delta>=0:self.previous_delta=delta
        bad=('missing_timestamp' if delta is None else 'negative_delay' if delta<0 else
             'delay_limit' if delta>self.maximum else
             'delay_spike' if self.jump is not None and self.jump>=self.spike else None)
        if bad:
            self.has_sample=True;self.recovery_since=None
            if self.blocked_since is None:self.blocked_since=desktop_ms
            self._state(True,bad,desktop_ms);return
        if not self.has_sample:
            self.has_sample=True;self._state(False,'healthy',desktop_ms);return
        if self.blocked:
            if delta>self.resume:
                self.recovery_since=None;self._state(True,'above_resume',desktop_ms);return
            if self.recovery_since is None:self.recovery_since=desktop_ms
            if desktop_ms-self.recovery_since>=self.stable_ms:
                self.recovery_since=None;self._state(False,'healthy',desktop_ms)
            else:self._state(True,'recovering',desktop_ms)

    def check(self,now,max_age_ms):
        if self.enabled and self.last_sample is not None and now-self.last_sample>max_age_ms:
            self.recovery_since=None;self._state(True,'stale_sample',now)
        return not self.blocked

    def snapshot(self,now):
        remaining=(max(0,self.stable_ms-(now-self.recovery_since)) if self.recovery_since is not None else None)
        return dict(enabled=self.enabled,blocked=self.blocked,reason=self.reason,basis='desktopTime - serverTime',
                    delta_ms=self.delta,jump_ms=self.jump,last_sample_ms=self.last_sample,
                    blocked_since_ms=self.blocked_since,recovery_since_ms=self.recovery_since,
                    recovery_remaining_ms=remaining,block_episodes=self.episodes,
                    blocked_submissions=self.blocked_submissions,existing_orders='cancel' if self.cancel_working else 'keep',
                    max_feed_delay_ms=self.maximum,feed_delay_spike_ms=self.spike,
                    feed_delay_resume_ms=self.resume,feed_delay_stable_ms=self.stable_ms)
