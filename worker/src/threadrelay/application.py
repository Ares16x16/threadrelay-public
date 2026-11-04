"""Durable offline composition. Native activation requires a separate host capability.

The root is trusted bootstrap data, not a renderer argument. A capable host warms
the installed local model in the background; browser and account actions remain explicit.
"""
from pathlib import Path

from .collector.embedded import EmbeddedBrowserSession as BrowserSession
from .coordinator import Coordinator
from .delivery.auth import AuthController
from .delivery.destinations import DestinationVerifier
from .delivery.owner import DeliveryOwner
from .delivery.sender import Sender
from .delivery.telegram import NativeUnavailable, TelegramClient
from .features import AccountFeatures, DestinationFeatures, LocalFeatures, ThreadsFeatures
from .inference.decision import Classifier
from .inference.runtime import DecisionRuntime
from .inference.native_launcher import NativeInferenceLauncher
from .inference.provision import ModelProvisioner, load_catalog
from .routing.policy import Router
from .pipeline import Pipeline, SessionPreparation
from .readiness import assess
from .session import SessionController, SystemClock
from .secure_channel import SecureChannel
from .storage.database import Database
from .storage.outbox import AccountStore, Outbox
from .storage.posts import PostStore
from .storage.routing import ConfigStore


class _UnavailableSecrets:
    def resolve(self, ref):
        raise NativeUnavailable('Secure vault unavailable')


def _unavailable_binding():
    raise NativeUnavailable('Native TDLib unavailable')


def _unavailable_reference():
    raise NativeUnavailable('Secure vault unavailable')


def _prepare_unavailable(config, cancelled):
    raise NativeUnavailable('Owned native preparation unavailable')


class LazyTelegramClient:
    """A stable shared proxy; only explicit offline initialization creates TDLib's wrapper."""
    def __init__(self, factory):
        self._factory = factory
        self._client = None
        self._destination_verifier = None
        self._destination_listing = None
        self._owner_test = None
        self._owner_history = None
        self._owner_automatic = None
        self.profile_allowed=None

    @property
    def _binding(self):
        return None if self._client is None else self._client._binding

    def has_existing_session(self,account_ref):
        candidate=self._client if self._client is not None else self._factory()
        return candidate.has_existing_session(account_ref)

    def initialize_offline(self, account_ref, *, require_existing=False):
        if self._client is not None and self._client._closed:
            self._client = None
        if self._client is None:
            self._client = self._factory()
            self._client._destination_verifier = self._destination_verifier
            self._client._destination_listing = self._destination_listing
            self._client._owner_test = self._owner_test
            self._client._owner_history = self._owner_history
            self._client._owner_automatic = self._owner_automatic
            self._client.profile_allowed=self.profile_allowed
        return self._client.initialize_offline(account_ref,require_existing=require_existing)

    def disable_network(self):
        if self._client is not None:
            self._client.disable_network()

    def begin_shutdown(self):
        if self._client is not None:
            self._client.begin_shutdown()

    def close(self):
        if self._client is not None:
            self._client.close()

    def __getattr__(self, name):
        client = self.__dict__.get('_client')
        if client is None:
            raise NativeUnavailable('Telegram client unavailable')
        return getattr(client, name)


class Application:
    def __init__(self, root: Path, *, generation: int = 1, broker=None):
        root = Path(root)
        if not root.is_absolute():
            raise ValueError('Trusted absolute data root required')
        self.root = root.resolve()
        self._broker = broker
        from threading import Event,Lock
        self._sample_cancelled = Event()
        self._sample_runtime = None
        self._sample_active = False
        self._sample_cleanup_pending = False
        self._model_shutdown=Event()
        self._model_lock=Lock()
        self._model_start_done=Event()
        self._model_thread=None
        self._model_start_error=None
        self._collection_browser = None
        self.clock = SystemClock()
        self.db = Database(self.root / 'threadrelay.sqlite3', clock=self.clock.now)
        try:
            from .storage.activity import ActivityStore
            self.activity = ActivityStore(self.db)
            self._activity_recording_ok = True
            self.session = SessionController(clock=self.clock, recovered=bool(self.db.recovery['reasons']),
                setup_check=lambda config: self._setup_check(config), on_transition=self._session_activity)
            self.coordinator = Coordinator(self.session, prepare=_prepare_unavailable)
            self.config = ConfigStore(self.db)
            from .storage.saved_destinations import SavedDestinations
            SavedDestinations(self.db).require_saved_selection()
            self.posts = PostStore(self.db)
            self.outbox = Outbox(self.db)
            provisioner = ModelProvisioner()
            catalog = load_catalog()
            model = next(record for record in catalog['models'] if record['manifest'] == provisioner.primary_manifest)
            self.browser_launcher = None  # Embedded host attachment owns its own bounded release.
            inference_launcher = NativeInferenceLauncher(self.root, broker) if broker is not None else None
            self._model_size = model['size_bytes']
            self._model_manifest=provisioner.primary_manifest
            self.runtime = DecisionRuntime(model_file=self.root / 'models' / model['filename'], provisioner=provisioner,
                launcher=inference_launcher,
                setup_authorized=lambda:not self._model_shutdown.is_set(),
                can_infer=self._can_infer_model,persistent=True)
            self.session.add_gate_close_hook(self.runtime.cancel_request)
            self.session.add_gate_close_hook(self._cancel_sample)
            self.classifier = Classifier(self.runtime)
            from .routing.automatic import AutomaticPolicy
            self.automatic = AutomaticPolicy(self.db, self.classifier, route_driven=True, evidence=(lambda: broker.automatic_evidence()) if broker is not None else lambda: None)
            self.router = Router(self.db, classifier=self.classifier, clock=self.clock,
                auto_send_authorized=self.automatic.authorized, auto_route_authorized=self.automatic.route_authorized,
                auto_source_authorized=self.automatic.source_authorized,
                selected_route_ids=lambda: self.session.snapshot.get('config', {}).get('route_ids', []))
            self.telegram = LazyTelegramClient(lambda: TelegramClient(broker.load_tdlib if broker is not None and hasattr(broker, "load_tdlib") else _unavailable_binding, broker if broker is not None else _UnavailableSecrets(), self.root / 'telegram'))
            self.secure_channel = SecureChannel(generation=generation)
            self.auth = AuthController(self.telegram, AccountStore(self.db), self.secure_channel,
                clock=self.clock.monotonic, lifecycle_lock=self.session.authority_lock)
            self.destination = DestinationVerifier(self.telegram, self.auth, self.config, self.session)
            from .delivery.destination_listing import DestinationListing
            self.destination_listing = DestinationListing(self.telegram, self.auth, self.config, self.session,
                native_gate=self._telegram_auth_capable, clock=self.clock.monotonic)
            from .delivery.owner_test import OwnerTestController
            self.owner_test = OwnerTestController(self.telegram, self.auth, self.session,
                native_gate=self._telegram_auth_capable, monotonic=self.clock.monotonic,
                on_state=self._owner_test_activity)
            self.session.add_gate_close_hook(self.owner_test.revoke)
            self.sender = Sender(self.router, self.session, self.auth, self.telegram,
                destination_authority=self.destination, clock=self.clock.now, monotonic=self.clock.monotonic)
            from .delivery.workflow import DeliveryWorkflow
            from .storage.archive import ArchiveStore
            self.archive = ArchiveStore(self.db)
            self.workflow = DeliveryWorkflow(self.db, self.archive, auth=self.auth, destinations=self.destination,
                sender=self.sender, native_gate=self._telegram_capable, automatic=self.automatic, clock=self.clock)
            from .delivery.owner_history import OwnerHistoryController
            self.owner_history = OwnerHistoryController(self.telegram,self.auth,self.session,self.workflow,
                native_gate=self._telegram_auth_capable,monotonic=self.clock.monotonic)
            self.session.add_gate_close_hook(self.owner_history.revoke)
            from .delivery.automatic_queue import AutomaticQueue
            from .delivery.owner_automatic import OwnerAutomaticController
            self.automatic_queue = AutomaticQueue(self.router,self.automatic,self.sender,self.clock)
            self.owner_automatic = OwnerAutomaticController(self.telegram,self.auth,self.session,self.automatic_queue,
                native_gate=self._telegram_auth_capable,monotonic=self.clock.monotonic)
            self.session.add_gate_close_hook(self.owner_automatic.revoke)
            self.delivery_owner = DeliveryOwner(self.sender, self.telegram, self.session,
                native_gate=self._telegram_capable, auth_gate=self._telegram_auth_capable, vault_reference=(lambda: broker.begin_account()) if broker is not None else _unavailable_reference,
                maintenance=self.maintenance, remove_local=(lambda: broker.remove_account()) if broker is not None else None, workflow=self.workflow, listing=self.destination_listing, owner_test=self.owner_test, owner_history=self.owner_history, owner_automatic=self.owner_automatic)
            from .delivery.recovery import TelegramRecovery
            self.telegram_recovery=TelegramRecovery(self.root/'telegram',self.auth,self.telegram,self.session,
                quarantine=(lambda profile_id,digest:broker.quarantine_telegram_profile(profile_id,digest)) if broker is not None else None,
                verify=(lambda recovery_id:broker.verify_telegram_quarantine(recovery_id)) if broker is not None else None,
                stop_collection=self.coordinator.stop,reset_owners=(self.owner_history,self.owner_automatic),clock=self.clock.monotonic)
            self.auth.verify_isolations=self.telegram_recovery.verify_for_login
            self.telegram.profile_allowed=self.telegram_recovery.profile_allowed
            self.delivery_owner.recovery=self.telegram_recovery
            self.coordinator.on_running = self.delivery_owner.running
            self.browser = BrowserSession(app_data_dir=self.root, broker=broker, can_intake=self.session.can_intake,
                on_attention=self.session.require_attention, now=self.clock.now, launcher=self.browser_launcher)
            account = AccountFeatures(self.auth, vault_reference=(lambda: broker.begin_account()) if broker is not None else _unavailable_reference,
                native_gate=self._telegram_auth_capable, lifecycle=self.delivery_owner, discard_secret_ref=self.secure_channel.discard, wall_clock=self.clock.now)
            destination = DestinationFeatures(self.destination, native_gate=self._telegram_capable, self_gate=self._telegram_auth_capable,
                metadata_gate=self._telegram_auth_capable, listing=self.destination_listing)
            def login_factory():
                browser = BrowserSession(app_data_dir=self.root, broker=broker, launcher=self.browser_launcher,
                    can_intake=self.session.can_intake, on_attention=self.session.require_attention, now=self.clock.now)
                launcher, profile = self.browser_launcher, self.root / 'browser-profiles' / 'threads'
                class LoginLease:
                    def open(self): browser.open(profile)
                    def close(self):
                        try:
                            browser.close()
                            if launcher is not None and launcher.close_pending() != {'stopped': True, 'remaining': []}:
                                return {'stopped': False, 'remaining': ['browser']}
                            return {'stopped': True, 'remaining': []}
                        except Exception:
                            return {'stopped': False, 'remaining': ['browser']}
                return LoginLease()
            self.features = LocalFeatures(self.db, router=self.router, account=account, session=self.session, wake_delivery=self.delivery_owner.wake, automatic=self.automatic, sample=self.test_sample, workflow=self.workflow,
                listing=self.destination_listing, app_status=self.app_status, activity=self.query_activity, owner_test=self.owner_test, owner_history=self.owner_history, owner_automatic=self.owner_automatic,
                destination=destination, threads=ThreadsFeatures(self.browser, app_data_dir=self.root,
                    open_owned=lambda: self.coordinator.open_login(login_factory)))
            self.features.telegram_recovery=self.telegram_recovery
            if broker is not None:
                def browser_factory(config):
                    browser = BrowserSession(app_data_dir=self.root, broker=broker, launcher=self.browser_launcher,
                        feed_url=config['feed_url'], can_intake=self.session.can_intake,
                        on_attention=self.session.require_attention, now=self.clock.now)
                    self._collection_browser = browser
                    return browser
                self.coordinator.prepare = SessionPreparation(browser_factory=browser_factory, runtime=self.runtime,
                    manifest=provisioner.primary_manifest, profile=self.root / 'browser-profiles' / 'threads',
                    pipeline_factory=self.pipeline, session=self.session, browser_launcher=self.browser_launcher,
                    prepare_runtime=self._ensure_model,retain_runtime=True,
)
                if broker.capabilities().get('inference') is True:self._start_model()
        except BaseException:
            if hasattr(self, 'workflow'):
                self.workflow.close()
            if hasattr(self, 'delivery_owner'):
                self.delivery_owner.close()
            self.db.close()
            raise

    def _cancel_sample(self):
        self._sample_cancelled.set()
        if self._sample_runtime is not None:
            self._sample_runtime.cancel_request()

    def _can_infer_model(self):
        return not self._model_shutdown.is_set() and (
            self._sample_active and not self._sample_cancelled.is_set() and self.session.snapshot['state']=='stopped'
            or self.session.can_process_observation())

    def _start_model(self):
        """One background activation of the installed, pinned local model."""
        from threading import Thread
        with self._model_lock:
            if self._model_shutdown.is_set():raise RuntimeError('Application closing')
            if self.runtime.status()['state'] in {'ready','classifying'}:return
            if self._model_thread is not None and self._model_thread.is_alive():return
            self._model_start_error=None
            self._model_start_done.clear()
            def start():
                try:self.runtime.start(self._model_manifest,cancellation=self._model_shutdown)
                except Exception as error:self._model_start_error=error
                finally:self._model_start_done.set()
            self._model_thread=Thread(target=start,name='application-model-owner',daemon=True)
            self._model_thread.start()

    def _ensure_model(self,cancelled=None):
        self._start_model()
        while not self._model_start_done.wait(.05):
            if self._model_shutdown.is_set() or cancelled is not None and cancelled.is_set():
                raise RuntimeError('Model request cancelled')
        if self._model_shutdown.is_set() or cancelled is not None and cancelled.is_set():
            raise RuntimeError('Model request cancelled')
        if self._model_start_error is not None:raise RuntimeError('Local model unavailable') from None

    def _record_activity(self, kind, **values):
        try:
            self.activity.append(kind, nonblocking=True, **values)
        except Exception:
            # Keep revocation independent, but expose missing observations.
            self._activity_recording_ok = False

    def _owner_test_activity(self, snapshot):
        state = snapshot['state']
        kind = {'verifying':'telegram_test_verifying','sending':'telegram_test_sending',
                'sent':'telegram_test_sent','failed':'telegram_test_failed','unknown':'telegram_test_unknown'}.get(state)
        if kind:
            self._record_activity(kind, detail='Test')

    def _session_activity(self, snapshot):
        kind = {'starting':'session_starting','running':'session_running','paused':'session_paused',
                'needs_attention':'session_attention','failed':'session_attention','stopped':'session_stopped'}.get(snapshot['state'])
        if kind:
            self._record_activity(kind, detail=snapshot['attention_reason'])

    def query_activity(self, **query):
        status = self.app_status()
        return {**self.activity.query(**query), 'session':status['session'], 'model':status['model'], 'recovery':status['recovery'],
                'recording_ok':self._activity_recording_ok}

    def app_status(self):
        """Local observation only; no activation, vault read, reconnect or probe."""
        session = self.session.snapshot
        # A failed close retains this runtime even after sample intake ends.
        # Keep reporting its cleanup phase until ownership is released.
        sample = self._sample_active or self._sample_runtime is not None
        runtime = self._sample_runtime if sample else self.runtime
        model = runtime.status() if runtime is not None else {'state':'starting' if sample else 'unavailable','model_id':None}
        if not sample and self._model_thread is not None and self._model_thread.is_alive() and model['state']=='not_loaded':
            model={**model,'state':'starting'}
        activity = 'sample' if sample else 'collection' if session['state'] in {'starting','running','pausing','stopping'} and model['state'] in {'starting','ready','classifying','stopping'} else None
        recovery = None
        if session['state'] == 'running' and self._collection_browser is not None:
            recovery = self._collection_browser.recovery_status()
        return {'service':'ready','model':model,'activity':activity,'session':session,'recovery':recovery}

    def test_sample(self, request):
        """Explicit isolated run; optional stored source never enrolls or sends."""
        import hashlib
        from uuid import uuid4
        from .contracts import validate_contract
        validate_contract('SampleRequest', request)
        stored_post=None
        with self.session.authority_lock:
            if self._sample_active or self._sample_cleanup_pending or self.coordinator._owner is not None or self.session.snapshot['state'] != 'stopped':
                raise ValueError('Stop the session before sample testing')
            if 'post_id' in request:
                stored_post=self.posts.get(request['post_id'])
                if stored_post is None or stored_post['text']!=request['text']:
                    raise ValueError('Stored source changed or unavailable')
            runtime=self.runtime
            self._sample_cancelled.clear()
            self._sample_active = True
            self._sample_runtime = runtime
        from .storage.archive import ArchiveStore
        archive = ArchiveStore(self.db)
        classifier = Classifier(runtime)
        topics = self.config.topics()
        run = None
        result = None
        try:
            revision=archive.observe(stored_post) if stored_post is not None else None
            run = archive.start_run('collection' if stored_post is not None else 'sample', revision,
                {'post':stored_post} if stored_post is not None else {'text':request['text']},topics,classifier.metadata)
            self._ensure_model(self._sample_cancelled)
            # Synthetic envelope never enters the observed-post archive.
            identifier = 'sample-' + uuid4().hex
            post = {'post_id':identifier,'permalink':'https://www.threads.com/@sandbox/post/'+identifier,
                'author':'Isolated sample','text':request['text'],'content_hash':hashlib.sha256(request['text'].encode()).hexdigest(),
                'visibility':'unknown','quality_flags':[],'observed_at':self.clock.now().isoformat(),'source_session_id':'sandbox','quote':None}
            result = classifier.classify(stored_post if stored_post is not None else post, topics)
        finally:
            with self.session.authority_lock:
                cancelled = self._sample_cancelled.is_set()
                self._sample_active = False
                self._sample_runtime = None
                if run is not None:
                    if cancelled:
                        archive.finish_run(run, 'cancelled', error_code='session_cancelled')
                    elif result is None or classifier.last_reason == 'inference_unavailable':
                        archive.finish_run(run, 'failed', error_code='model_unavailable')
                    else:
                        archive.finish_run(run, 'succeeded', classification=result)
                        if stored_post is not None:
                            from .storage.collection import CollectionStore
                            if not CollectionStore(self.db).record(stored_post,topics,result):
                                raise ValueError('Stored source or topics changed during classification')
            if cancelled:
                raise RuntimeError('Sample cancelled')
        return {'classification':result,'sending_possible':False}

    def _telegram_capable(self):
        return bool(self._broker is not None and self._broker.capabilities().get('telegram') is True)

    def _telegram_auth_capable(self):
        if self._broker is None:return False
        capabilities=self._broker.capabilities()
        return capabilities.get('telegram_auth') is True or capabilities.get('telegram') is True

    def maintenance(self, *, force=False):
        # Confirmed policy only. Bounded work at most once per minute on the owner;
        # opening/querying never silently adopts proposed deletion durations.
        if not hasattr(self, 'features'):
            return
        now = self.clock.monotonic()
        if not force and now < getattr(self, '_next_maintenance', 0):
            return
        self._next_maintenance = now + 60
        policy = self.features.retention()
        if policy['confirmed']:
            from .storage.retention import Retention
            Retention(self.db, **{k:v for k,v in policy.items() if k != 'confirmed'}).prune(self.clock.now())

    def pipeline(self, cancelled):
        return Pipeline(self.session, cancelled, self.posts, self.config, self.classifier, self.router, self.delivery_owner.bind(cancelled), on_history=self.workflow.on_history, on_activity=self._record_activity)

    def readiness(self, config):
        # Independent, current host proofs; absent installed acceptance remains
        # closed without affecting local SQLite operations.
        capabilities = {} if self._broker is None else self._broker.capabilities()
        try:
            embedded_available = self._broker is not None and self._broker.embedded_threads_available() is True
        except Exception:
            embedded_available = False
        capabilities['browser'] = bool(embedded_available and config.get('feed_url', 'https://www.threads.com/') == 'https://www.threads.com/')
        capabilities['automatic'] = capabilities.get('automatic', False) and self.automatic.authorized()
        # The narrowly scoped connected-account score lane has its own live
        # checks and private permit; it never changes native delivery proof.
        if self._score_auto_ready():
            capabilities['telegram'] = True
            capabilities['automatic'] = self.automatic.authorized()
        if capabilities.get('inference', False):
            try:
                model = self.runtime._file
                valid = not model.is_symlink() and model.is_file() and model.stat().st_size == self._model_size
            except OSError:
                valid = False
            if not valid:
                capabilities = {**capabilities, 'inference':False}
        result = assess(config, capabilities)
        if config.get('mode') == 'auto':
            selected = config.get('route_ids', [])
            routes = [r for r in self.config.routes() if r['id'] in selected and r['enabled'] and r['mode'] == 'auto']
            if len(selected) != 1 or len(routes) != 1:
                return {**result, 'can_start': False, 'reason': 'select_one_auto_route'}
            from .storage.outbox import _has_native_uncertainty
            with self.db.connection() as con:
                if _has_native_uncertainty(con):
                    return {**result, 'can_start': False, 'reason': 'automatic_outcome_unknown'}
            account=self.auth.snapshot()
            if (self.auth.status!='ready' or account.auth_state!='ready' or account.network_state!='online'
                    or account.revocation_pending or not self.telegram.transport_ready):
                return {**result, 'can_start': False, 'reason': 'telegram_connection_required'}
            try:
                with self.db.connection() as con:
                    self.features._validate_route(routes[0], con)
            except Exception:
                return {**result, 'can_start': False, 'reason': 'selected_auto_route_unavailable'}
        return result

    def _score_auto_ready(self):
        try:
            account=self.auth.snapshot()
            from .delivery.telegram import NativeJsonBinding
            return bool(self._telegram_auth_capable() and self.auth.status=='ready'
                and account.auth_state=='ready' and account.network_state=='online' and not account.revocation_pending
                and type(self.telegram._binding) is NativeJsonBinding and self.telegram._binding.connection_only
                and self.telegram.transport_ready and self.telegram._profile_guard is not None
                and self.automatic_queue.profile_ready(self.telegram._profile_guard.profile_id))
        except Exception:return False

    def _setup_check(self, config):
        if self._sample_active or self._sample_cleanup_pending:
            return {'allowed':False,'reason':'sample_runtime_in_use'}
        value = self.readiness(config)
        return {'allowed': value['can_start'], 'reason': value['reason']}

    def close(self):
        self._model_shutdown.set()
        self.workflow.close()
        self.secure_channel.close()
        self._cancel_sample()
        self.runtime.revoke()
        if self._sample_runtime is not None:
            if self._sample_runtime.stop() != {"stopped":True,"remaining":[]}:
                raise RuntimeError("Sample runtime cleanup unconfirmed")
        self.coordinator.stop()
        if not self.coordinator.wait_cleanup(5):
            raise RuntimeError('Dependency cleanup unconfirmed')
        if self._model_thread is not None:
            self._model_thread.join(5)
            if self._model_thread.is_alive():raise RuntimeError('Model startup cleanup unconfirmed')
        if not self.delivery_owner.close():
            raise RuntimeError('Delivery owner cleanup unconfirmed')
        self.browser.close()
        report = self.runtime.stop()
        if not report['stopped']:
            raise RuntimeError('Inference cleanup unconfirmed')
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
