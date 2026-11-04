"""Synthetic cross-language fixture produced by the real account projection."""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_telegram_auth import FixtureBinding, FixtureVault, MemorySecretHandoff, state
from threadrelay.delivery.auth import AuthController
from threadrelay.delivery.telegram import TelegramClient
from threadrelay.features import AccountFeatures
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import AccountStore

with tempfile.TemporaryDirectory() as directory:
    # Canonicalize this freshly created test-owned temp directory: macOS may
    # spell the system temp root through /var, which the production no-follow
    # session directory boundary deliberately rejects.
    root=Path(directory).resolve(); binding=FixtureBinding(); handoff=MemorySecretHandoff()
    with Database(root/'fixture.db') as db:
        auth=AuthController(TelegramClient(lambda:binding,FixtureVault(),root/'sessions'),AccountStore(db),handoff,clock=lambda:0.0,challenge_ttl=5)
        adapter=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:True,discard_secret_ref=lambda ref:handoff.values.pop(ref,None),wall_clock=lambda:datetime(2030,1,1,tzinfo=timezone.utc))
        adapter.begin(); binding.events.append(state('WaitCode')); auth.poll()
        print(json.dumps(adapter.query()))
