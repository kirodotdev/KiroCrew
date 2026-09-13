"""Owner/repository-bound job receipts over the existing app spawn service.

This is result/cancellation plumbing, NOT a native-tool isolation guarantee.
Only trusted authenticated app handlers should choose an owner when binding.
No credential, config, transcript or result-file reads are performed here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import time


class ScopedSpawnError(RuntimeError):
    pass


def _label(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ScopedSpawnError(f"invalid {name}")
    if any(ord(c) < 32 for c in value):
        raise ScopedSpawnError(f"invalid {name}")
    return value


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class JobScope:
    app: str
    owner: str
    provider: str
    repository: str

    def __post_init__(self):
        for name, value in vars(self).items():
            _label(value, name)
        if self.provider not in ('amazon-internal', 'public-github'):
            raise ScopedSpawnError('unsupported repository provider')


@dataclass(frozen=True)
class _Record:
    scope: JobScope
    key: str
    request_hash: str
    info: object
    limit: int
    native: bool = False


class ScopedSpawnBackend:
    """One bounded registry per host spawn implementation, shared across contexts.

    Restart loses this process-local registry and yields unavailable, never a
    fabricated completion. The caller's durable attempt journal must not replay
    an unknown dispatched job. No auto-pruning/reuse of attempt identities.
    """
    def __init__(self, dispatch, lookup, cancel, *, capacity=256):
        self._dispatch, self._lookup, self._cancel = dispatch, lookup, cancel
        self._capacity = capacity
        self._records = {}
        self._attempts = {}
        self._lock = asyncio.Lock()

    def bind(self, app, owner, provider, repository):
        return ScopedSpawnJobs(self, JobScope(app, owner, provider, repository))

    async def start(self, scope, task, agent, attempt, purpose, limit, *, native=False):
        _label(agent, 'agent')
        _label(attempt, 'attempt')
        if purpose not in ('plan', 'patch', 'review'):
            raise ScopedSpawnError('unsupported purpose')
        if not isinstance(task, str) or not task.strip() or len(task.encode()) > 120000:
            raise ScopedSpawnError('invalid or oversized prompt')
        if type(limit) is not int or not 1 <= limit <= 131072:
            raise ScopedSpawnError('invalid output limit')
        key = _digest([vars(scope), attempt, purpose])
        request_hash = _digest([task, agent, limit])
        async with self._lock:
            if key in self._attempts:
                job = self._attempts[key]
                record = self._records[job]
                if record.request_hash != request_hash or record.native != native:
                    raise ScopedSpawnError('attempt already bound to different request')
                # Return the original receipt even when the manager lost it;
                # status will say unavailable. Never silently spawn it again.
                return job
            if len(self._records) >= self._capacity:
                raise ScopedSpawnError('job receipt capacity reached')
            native_kw = {'_native_text': True} if native else {}
            job = await self._dispatch(task, agent, True, '', scope.app,
                                       _scope_key='appjob:' + key, _capture_bytes=limit, **native_kw)
            info = self._lookup(job)
            if info is None or getattr(info, 'app', None) != scope.app:
                raise ScopedSpawnError('host returned an unowned job')
            if job in self._records:
                # Never associate an old id with another scope or a new record.
                raise ScopedSpawnError('host reused a job identity')
            self._records[job] = _Record(scope, key, request_hash, info, limit, native)
            self._attempts[key] = job
            return job

    def _owned(self, scope, job):
        record = self._records.get(job) if isinstance(job, str) else None
        if record is None or record.scope != scope:
            raise ScopedSpawnError('job unavailable')
        info = self._lookup(job)
        if info is not record.info or getattr(info, 'app', None) != scope.app:
            return record, None
        return record, info

    def snapshot(self, scope, job):
        record, info = self._owned(scope, job)
        result = {'job_id': job, 'state': 'unavailable', 'text': None,
                  'reason': 'host-record-unavailable', 'observed_model': None,
                  'usage': None, 'model_requests': None, 'cleanup_confirmed': None,
                  'native_tool_isolation': None, 'request_sha256': record.request_hash}
        if info is None:
            return result
        model = getattr(info, 'resolved_model', '')
        if isinstance(model, str) and 0 < len(model) <= 256:
            result['observed_model'] = model
        if not getattr(info, 'done', False):
            result.update(state='queued' if getattr(info, 'queued', False) else 'running',
                          reason='awaiting-host-completion')
            return result
        cleanup = getattr(info, 'app_cleanup_confirmed', None)
        if cleanup is not True:
            if cleanup is False:
                result.update(state='failed', reason='host-cleanup-unconfirmed',
                              cleanup_confirmed=False)
            else:
                result.update(state='settling', reason='awaiting-host-cleanup')
            return result
        result['cleanup_confirmed'] = True
        result['cleanup_scope'] = 'observed-process-tree'
        if getattr(info, 'user_stopped', False):
            result.update(state='cancelled', reason='host-reported-stopped')
            return result
        if getattr(info, 'error', ''):
            # Host exceptions can include paths/prompts. Leave details in the
            # host's existing governed diagnostics, not this text-model channel.
            result.update(state='failed', reason='host-job-failed')
            return result
        if record.native:
            profile = getattr(info, 'app_native_text_profile', None)
            receipt = profile.receipt() if profile is not None else None
            if receipt is None:
                result.update(state='failed', reason='native-isolation-unconfirmed')
                return result
            result['native_tool_isolation'] = True
            result['native_isolation_receipt'] = receipt
            result['prompt_requests'] = receipt['prompt_requests']
        text = getattr(info, 'app_result_text', None)
        reason = getattr(info, 'app_result_error', '')
        if reason or not isinstance(text, str):
            result.update(state='failed', reason=reason or 'complete-result-unavailable')
        elif len(text.encode()) > record.limit:
            result.update(state='failed', reason='output-limit-exceeded')
        else:
            result.update(state='completed', reason='host-result-captured', text=text)
        return result

    async def cancel(self, scope, job):
        _, info = self._owned(scope, job)
        if info is None:
            return {'requested': False, 'reason': 'host-record-unavailable',
                    'cleanup_confirmed': None}
        # No await separates this identity check from invoking the manager's
        # existing cancellation operation. Its True means accepted, not reaped.
        accepted = bool(await self._cancel(job))
        return {'requested': accepted, 'reason': 'host-cancel-requested' if accepted
                else 'host-did-not-accept-cancel', 'cleanup_confirmed': None}


class ScopedSpawnJobs:
    def __init__(self, backend, scope):
        self._backend, self.scope = backend, scope

    async def run(self, task, agent, *, attempt, purpose, max_output_bytes=131072):
        return await self._backend.start(self.scope, task, agent, attempt,
                                         purpose, max_output_bytes)

    async def run_isolated(self, task, agent, *, attempt, purpose, max_output_bytes=131072):
        return await self._backend.start(self.scope, task, agent, attempt,
                                         purpose, max_output_bytes, native=True)

    def result(self, job_id):
        return self._backend.snapshot(self.scope, job_id)

    async def cancel(self, job_id):
        return await self._backend.cancel(self.scope, job_id)

    async def wait(self, job_id, *, timeout_s=30):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 900:
            raise ScopedSpawnError('invalid wait timeout')
        end = time.monotonic() + timeout_s
        while True:
            receipt = self.result(job_id)
            if receipt['state'] not in ('queued', 'running', 'settling'):
                return receipt
            remaining = end - time.monotonic()
            if remaining <= 0:
                return {**receipt, 'wait_timed_out': True}
            await asyncio.sleep(min(0.5, remaining))


def capture_app_result(info, text):
    """Capture raw model text before dashboard redaction/trimming; app jobs only.

    Called in the real completion path. Never reads result_path or history.
    Legacy jobs (limit 0) keep their current behavior.
    """
    limit = getattr(info, 'app_result_limit', 0)
    if not limit:
        return
    info.app_result_text = None
    info.app_result_error = ''
    if not getattr(info, 'app', '') or type(limit) is not int or not 1 <= limit <= 131072:
        info.app_result_error = 'invalid-app-result-capture'
        return
    if not isinstance(text, str) or not text:
        info.app_result_error = 'empty-model-output'
    elif len(text.encode()) > limit:
        info.app_result_error = 'output-limit-exceeded'
    else:
        # Keep the host security layer. Structured output is not an alternate
        # channel around credential/egress redaction. Modified output cannot
        # serve as an exact patch, so refuse it instead of returning raw bytes.
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
        safe, _ = redact_exfiltration_urls(text)
        safe, _ = redact_credentials(safe)
        if safe != text:
            info.app_result_error = 'model-output-redacted'
        else:
            info.app_result_text = text
