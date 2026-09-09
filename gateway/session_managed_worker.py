"""Admission-owned production exec; observers never own the worker lifetime."""
import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys

from agent.managed_worker import encode_frame, read_frame
from gateway.session_worker_reservation import reserve_admission_worker
from hermes_state_runtime import RuntimeStoreError


def managed_policy(authority, ref):
    """Initial opt-in slice: explicit local custom-provider sessions only.

    The frozen creation snapshot, not current profile config, chooses execution.
    Safe-mode policy can reuse execute_managed after its early-import gates land.
    """
    from gateway.session_policy import policy_for_source
    policy = policy_for_source(authority.runner, authority.sessions[ref.session_id].source)
    if policy is None or policy.config().get('gateway', {}).get('managed_workers') is not True:
        return None
    request = json.loads(policy.request_json)
    if policy.source != 'cli' or request.get('provider') != 'custom' or not request.get('base_url'):
        raise RuntimeStoreError('unsupported_managed_policy')
    return policy


def _bootstrap(authority, ref, row, policy, scope):
    from gateway.session_policy import launch_key
    from gateway.session_policy_credentials import recover_config_secrets
    terminal = json.loads(policy.terminal_json)
    if policy.config_secret_ref:
        for path, value in recover_config_secrets(authority, policy).items():
            if path[0] is None:
                terminal[path[1]] = value
    hydrated = replace(policy, config_json=json.dumps(policy.config(authority)),
                       terminal_json=json.dumps(terminal), credential_ref=None, config_secret_ref=None)
    live = authority.sessions[ref.session_id]
    return {'version': 1, 'home': authority.profile_id, 'scope': scope,
            'policy': asdict(hydrated), 'api_key': launch_key(authority, policy),
            'text': row['payload']['text'], 'route': live.route,
            'user_id': live.source.user_id, 'chat_id': live.source.chat_id}


class ManagedWorker:
    def __init__(self, process):
        self.process = process

    def send(self, frame):
        self.process.stdin.write(encode_frame(frame))
        self.process.stdin.flush()

    def close(self):
        self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()


async def execute_managed(authority, ref, row, policy):
    from gateway.session_results import retain_result
    process = await asyncio.to_thread(subprocess.Popen, [sys.executable, '-m', 'agent.managed_worker'],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    worker = ManagedWorker(process)
    workers = getattr(authority, '_managed_workers', None)
    if workers is None:
        workers = authority._managed_workers = {}
    workers[ref.session_id] = worker
    accepted = None
    try:
        scope = reserve_admission_worker(authority, admission_id=row['admission_id'],
                    process=process, principal_id=row['principal_id'])
        # The child reads nothing else until the exact reservation has committed.
        await asyncio.to_thread(worker.send, _bootstrap(authority, ref, row, policy, scope))
        while True:
            frame = await asyncio.to_thread(read_frame, process.stdout)
            authority.check_approval_generation(ref.session_id, row['generation'])
            kind = frame.get('type')
            if kind == 'ready' and set(frame) == {'type', 'pid'} and frame['pid'] == process.pid:
                continue
            if kind == 'delta' and set(frame) == {'type', 'text'} and isinstance(frame['text'], str):
                authority.publish_execution(ref.session_id, row['generation'], 'message.delta', {'text': frame['text']})
                continue
            if kind == 'result' and set(frame) == {'type', 'result'} and accepted is None:
                result = frame['result']
                if (not isinstance(result, dict) or set(result) - {'final_response', 'failed', 'interrupted'}
                        or not isinstance(result.get('final_response'), str)):
                    raise RuntimeStoreError('invalid_worker_result')
                retain_result(authority.db, epoch=authority.epoch, row=row, result={'result': result, 'usage': {}})
                accepted = result
                await asyncio.to_thread(worker.send, {'type': 'finish'})
                continue
            if frame == {'type': 'finished'} and accepted is not None:
                code = await asyncio.to_thread(process.wait, 10)
                if code != 0:
                    raise RuntimeStoreError('managed_worker_lost')
                return accepted['final_response']
            raise RuntimeStoreError('invalid_worker_frame')
    finally:
        workers.pop(ref.session_id, None)
        await asyncio.to_thread(worker.close)
