"""Private exec entry. No agent/config imports before adopted process authority.

stdin is the owner's bounded bootstrap/control pipe; a duplicate of stdout is
reserved for framed events before runtime imports redirect ordinary output.
Neither assignment secrets nor launch credentials appear in argv or logs.
"""
import json
import os
from pathlib import Path
import sys
import threading

MAX_FRAME = 4 * 1024 * 1024
BOOTSTRAP_FIELDS = {'version', 'home', 'scope', 'policy', 'api_key', 'text', 'route', 'user_id', 'chat_id'}


def read_frame(stream):
    line = stream.readline(MAX_FRAME + 1)
    if not line:
        raise EOFError('managed_worker_pipe_closed')
    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
        raise ValueError('managed_worker_frame_too_large')
    frame = json.loads(line)
    if not isinstance(frame, dict):
        raise ValueError('invalid_managed_worker_frame')
    return frame


def encode_frame(frame):
    data = json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_FRAME:
        raise ValueError('managed_worker_frame_too_large')
    return data


def validate_bootstrap(frame):
    if set(frame) != BOOTSTRAP_FIELDS or frame['version'] != 1:
        raise ValueError('invalid_managed_worker_bootstrap')
    scope = frame['scope']
    fields = {'profile_id', 'session_id', 'execution_id', 'generation', 'pid', 'birth', 'secret', 'epoch'}
    if (not isinstance(scope, dict) or set(scope) != fields or scope['pid'] != os.getpid()
            or scope['profile_id'] != frame['home'] or not Path(frame['home']).is_absolute()
            or not isinstance(frame['policy'], dict)
            or any(not isinstance(frame[k], str) for k in ('text', 'route', 'user_id', 'chat_id'))
            or (frame['api_key'] is not None and not isinstance(frame['api_key'], str))):
        raise ValueError('invalid_managed_worker_bootstrap')
    return frame


class WorkerChannel:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, kind, **payload):
        data = encode_frame({'type': kind, **payload})
        with self.lock:
            self.stream.write(data)
            self.stream.flush()


def execute(frame, channel):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC
    scope = dict(frame['scope'])
    rpc = WorkerRPC(frame['home'])
    adopted = rpc('worker.adopt', **{k: v for k, v in scope.items() if k != 'epoch'})
    if adopted['owner_epoch'] != scope['epoch']:
        raise RuntimeError('stale_epoch')
    store = RuntimeSessionStore(rpc, scope, Path(frame['home']) / 'worker-outboxes' / scope['execution_id'])
    # Store construction binds the delegation ledger before tool discovery.
    from gateway.session_policy import restore_policy, policy_scope
    policy = restore_policy(frame['policy'])
    from run_agent import AIAgent
    agent = None
    try:
        with policy_scope(policy):
            agent = AIAgent(model=policy.model, provider=policy.provider, base_url=policy.base_url,
                api_key=frame['api_key'], session_db=store, session_id=scope['session_id'],
                enabled_toolsets=list(policy.toolsets), max_iterations=policy.max_turns,
                reasoning_config=policy.reasoning_config, platform=policy.source,
                gateway_session_key=frame['route'], user_id=frame['user_id'], chat_id=frame['chat_id'],
                skip_context_files=policy.ignore_rules, load_soul_identity=not policy.ignore_rules,
                skip_memory=policy.ignore_rules, skip_background_review=True, quiet_mode=True,
                stream_delta_callback=lambda text: channel.send('delta', text=text) if text else None)
            channel.send('ready', pid=os.getpid())
            history = store.get_messages_as_conversation(scope['session_id'])
            result = agent.run_conversation(frame['text'], conversation_history=history)
            agent._end_session_on_close = False
            agent.close()
            agent = None
            store.flush_token_counts()
            channel.send('result', result={k: result[k] for k in
                ('final_response', 'failed', 'interrupted') if k in result})
            if read_frame(sys.stdin.buffer) != {'type': 'finish'}:
                raise ValueError('invalid_managed_worker_control')
            store.finish()
            channel.send('finished')
    finally:
        if agent is not None:
            agent._end_session_on_close = False
            agent.close()
        store.close()


def main():
    channel = WorkerChannel(os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0))  # windows-footgun: ok — binary frames
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        frame = validate_bootstrap(read_frame(sys.stdin.buffer))
        execute(frame, channel)
    except Exception:
        # Runtime exceptions can contain credentials; the owner gets no raw traceback.
        channel.send('error', reason='managed_worker_failed')
        return 1
    finally:
        channel.stream.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
