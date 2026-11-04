"""Narrow inference launch adapter for a trusted same-manager host broker.

No process, executable, URL, argv or model path is accepted from renderer IPC.
The broker must itself verify artifacts, register its direct owned root before
activation, and prove endpoint ownership and resource limits. This adapter does
not manufacture those platform capabilities or open a native safety gate.
"""
from pathlib import Path
import re

from .provision import load_catalog
from .runtime import RuntimeLaunchSpec, RuntimeUnavailable


class NativeInferenceLauncher:
    def __init__(self, app_data_root: Path, broker):
        root = Path(app_data_root)
        if not root.is_absolute():
            raise ValueError('Trusted app root required')
        self._root, self._broker = root.resolve(), broker
        self._catalog = load_catalog()

    def launch(self, spec: RuntimeLaunchSpec):
        if not isinstance(spec, RuntimeLaunchSpec):
            raise RuntimeUnavailable('invalid_runtime_launch')
        record = next((r for r in self._catalog['models'] if r['manifest']['id'] == spec.model_id), None)
        if (record is None or spec.model_sha256 != record['manifest']['sha256']
                or spec.runtime_commit != self._catalog['runtime']['commit']
                or spec.model_file != self._root / 'models' / record['filename']
                or spec.host != '127.0.0.1' or spec.offline is not True
                or type(spec.context_tokens) is not int or not 2048 <= spec.context_tokens <= 65536
                or type(spec.cpu_threads) is not int or not 1 <= spec.cpu_threads <= 16
                or not isinstance(spec.auth_token, str) or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', spec.auth_token) is None
                or spec.cancelled.is_set()):
            raise RuntimeUnavailable('invalid_runtime_launch')
        # The private broker maps the pinned ID to its own verified installation;
        # it never trusts a worker-supplied executable, path, argv or endpoint.
        # If cancellation races the return, DecisionRuntime retains and closes
        # this lease rather than losing ownership in this adapter.
        return self._broker.launch_inference(model_id=spec.model_id, context_tokens=spec.context_tokens,
            cpu_threads=spec.cpu_threads, auth_token=spec.auth_token, cancelled=spec.cancelled)
