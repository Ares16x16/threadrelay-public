"""Generation-bound pipeline using the existing classifier and routing gates.

The owning collector thread calls this with visible, extracted observations.
Slow inference happens outside the lifecycle lock. A cancellation or lifecycle
transition prevents a late result from creating any routing receipt or send.
"""


class Pipeline:
    def __init__(self, session, cancelled, posts, config, classifier, router, sender, *, on_history=None, on_activity=None):
        self.session, self.cancelled = session, cancelled
        self.posts, self.config, self.classifier = posts, config, classifier
        self.router, self.sender = router, sender
        self.on_history = on_history
        self.on_activity = on_activity

    def _live(self):
        return not self.cancelled.is_set() and self.session.can_process_observation()

    def process(self, post):
        with self.session.authority_lock:
            if self.cancelled.is_set() or not self.session.observe_post(defer_completion=True):
                return
        try:
            return self._process_admitted(post)
        finally:
            self.session.complete_observation()

    def _process_admitted(self, post):
        with self.session.authority_lock:
            if not self._live():
                return
            from .storage.archive import ArchiveStore
            archive = ArchiveStore(self.posts.db)
            revision = archive.observe(post)
            self.posts.upsert(post)
            if self.posts.get(post['post_id']) != post:
                return  # out-of-order input must not restore or classify old content
            topics = self.config.topics()
            run = archive.start_run('collection', revision, {'post': post}, topics, self.classifier.metadata)
        # Native inference is cancellable by the owned runtime and never holds
        # the lifecycle lock, so Pause/Stop and local reads remain responsive.
        try:
            decision = self.classifier.classify_bound(post, topics)
        except Exception:
            archive.finish_run(run, 'cancelled' if not self._live() else 'failed',
                error_code='session_cancelled' if not self._live() else 'classification_failed')
            raise
        with self.session.authority_lock:
            if not self._live():
                archive.finish_run(run, 'cancelled', error_code='session_cancelled')
                return
            try:
                result = decision.for_snapshot(post, topics, self.classifier)
            except Exception:
                archive.finish_run(run, 'failed', error_code='classification_context_mismatch')
                raise
            if self.classifier.last_reason == 'inference_unavailable':
                archive.finish_run(run, 'failed', error_code='model_unavailable')
                return
            archive.finish_run(run, 'succeeded', classification=result)
            if self.on_history is not None:
                self.on_history(revision, run)
            from .storage.collection import CollectionStore
            CollectionStore(self.posts.db).record(post, topics, result)
            if any(item['match'] for item in result['topic_results']):
                self.session.record_counter('matched')
            mode = self.session.snapshot.get('config', {}).get('mode')
            if mode == 'dry_run':
                return
            proposals = self.router.plan(post, decision, session_mode=mode)
            # The Router independently rechecks its current auto gate and every
            # account, destination, route, sender and content binding.
            if mode == 'auto':
                for proposal in proposals:
                    if not self._live():
                        break
                    if not proposal['approval_required'] and self.router.auto_send_authorized():
                        _, created = self.router.stage(proposal, report_created=True)
                        if created:
                            self.session.record_counter('queued')
            if mode == 'auto' and getattr(self.router, 'selected_route_ids', None) is not None:
                return proposals
            if self._live() and self.session.can_transmit(self.session._clock.now()) and mode in {'review', 'auto'}:
                self.sender.start_next('session')
            return proposals


class PreparedSession:
    """Runs/cleans native objects on the single synchronous browser owner thread."""
    def __init__(self, browser, runtime, pipeline, session, *, poll=None, browser_launcher=None, retain_runtime=False):
        from threading import get_ident
        self.browser, self.runtime, self.pipeline, self.session = browser, runtime, pipeline, session
        self.poll, self.browser_launcher = poll, browser_launcher
        self._owner = get_ident()
        self._browser_closed = self._runtime_closed = False
        self._retain_runtime=retain_runtime

    def _same_thread(self):
        from threading import get_ident
        if get_ident() != self._owner:
            raise RuntimeError('Dependency owner thread mismatch')

    def run(self, cancelled):
        self._same_thread()
        last_read = None
        last_issue = None
        last_recovery = None
        activity = getattr(self.pipeline, 'on_activity', None)
        recovery_delay = getattr(self.browser, 'recovery_delay', lambda: 0)
        recovery_status = getattr(self.browser, 'recovery_status', lambda: None)

        def report_recovery():
            nonlocal last_recovery
            if activity is None:
                return
            status = recovery_status()
            if status is not None:
                marker = (status['reason'],status['attempt'])
                if marker != last_recovery:
                    activity('collection_recovering',detail=f"{status['reason']}:{status['attempt']}:{status['retry_in_seconds']}")
                    last_recovery = marker
            elif last_recovery is not None and not cancelled.is_set() and self.session.can_intake():
                # The collector clears recovery only after a validated card.
                # A hard stop also clears its status, but closes intake and
                # must never be described as recovery or reopen authority.
                activity('collection_recovered',detail='validated_post')
                last_recovery = None

        while not cancelled.is_set() and self.session.can_intake():
            delay = recovery_delay()
            report_recovery()
            if delay > 0:
                # Stay on this dependency owner and capture lease. Sleep via
                # cancellation, not a restart or a busy observation loop; the
                # one-second cap also refreshes the existing session deadline.
                cancelled.wait(min(delay,1))
                continue
            if cancelled.is_set() or not self.session.can_intake():
                break
            stamp = self.session._clock.monotonic() if activity is not None else 0
            reporting = last_read is None or stamp - last_read >= 5
            if reporting and activity is not None:
                activity('reading', detail='visible_feed')
                last_read = stamp
            posts = self.browser.observe()
            if not isinstance(posts, list) or len(posts) > 100:
                raise ValueError('Invalid bounded observation batch')
            report_recovery()
            issues = getattr(self.browser, 'issues', [])
            codes = tuple(sorted({item['code'] for item in issues}))
            if codes and activity is not None and (reporting or codes != last_issue):
                activity('skipped', detail=codes[0])
            last_issue = codes
            for post in posts:
                if cancelled.is_set() or not self.session.can_intake():
                    break
                self.pipeline.process(post)
            if cancelled.is_set() or not self.session.can_intake():
                break
            self.browser.scroll_next()
            report_recovery()
            cancelled.wait(min(1,max(.25,recovery_delay())))

    def close(self):
        self._same_thread()
        if not self._browser_closed:
            try:
                self.browser.close()
                self._browser_closed = True
            except Exception:
                pass
        if self.browser_launcher is not None:
            try:
                self._browser_closed = self._browser_closed and self.browser_launcher.close_pending() == {'stopped': True, 'remaining': []}
            except Exception:
                self._browser_closed = False
        if self._retain_runtime:self._runtime_closed=True
        if not self._runtime_closed:
            try:
                self._runtime_closed = self.runtime.stop() == {'stopped': True, 'remaining': []}
            except Exception:
                pass
        remaining = ([] if self._browser_closed else ['browser']) + ([] if self._runtime_closed else ['inference'])
        return {'stopped': not remaining, 'remaining': remaining}


class PreparationFailure(RuntimeError):
    def __init__(self, *, cleanup_confirmed, lease=None, stage=None):
        super().__init__('Owned session preparation unavailable')
        if stage not in (None, 'browser', 'model'):
            raise ValueError('Unsupported preparation stage')
        self.cleanup_confirmed = cleanup_confirmed
        self.lease = lease
        self.attention_reason = {'browser':'browser_preparation_failed', 'model':'model_preparation_failed'}.get(stage, 'preparation_failed')


class SessionPreparation:
    """Trusted owner-thread assembly; factories themselves must be inert."""
    def __init__(self, *, browser_factory, runtime, manifest, profile, pipeline_factory, session,
                 browser_launcher=None, poll=None, prepare_runtime=None, retain_runtime=False):
        self.browser_factory, self.runtime, self.manifest = browser_factory, runtime, manifest
        self.profile, self.pipeline_factory, self.session = profile, pipeline_factory, session
        self.browser_launcher, self.poll = browser_launcher, poll
        self.prepare_runtime,self.retain_runtime=prepare_runtime,retain_runtime

    def __call__(self, config, cancelled):
        from copy import deepcopy
        browser = self.browser_factory(config)
        owned = PreparedSession(browser, self.runtime, self.pipeline_factory(cancelled), self.session,
                                browser_launcher=self.browser_launcher, poll=self.poll,retain_runtime=self.retain_runtime)
        stage = 'browser'
        try:
            if not cancelled.is_set():
                browser.open(self.profile)
            stage = 'model'
            if not cancelled.is_set():
                if self.prepare_runtime is not None:self.prepare_runtime(cancelled)
                else:self.runtime.start(deepcopy(self.manifest), cancellation=cancelled)
            return owned
        except Exception:
            try:
                confirmed = owned.close() == {'stopped': True, 'remaining': []}
            except Exception:
                confirmed = False
            raise PreparationFailure(cleanup_confirmed=confirmed, lease=None if confirmed else owned, stage=stage) from None
