"""Real AIAgent in a separate interpreter, using only the worker transport."""
import json
import os
from pathlib import Path
import sys

import psutil

from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC


def writable_fds():
    result = []
    for path in Path('/proc/self/fd').iterdir():
        try:
            target = path.resolve()
            flags = next(line.split(':')[1].strip() for line in
                         (Path('/proc/self/fdinfo') / path.name).read_text().splitlines()
                         if line.startswith('flags:'))
            if target.name in ('state.db', 'state.db-wal', 'state.db-shm') and int(flags, 8) & 3:
                result.append(str(target))
        except FileNotFoundError:
            continue
    return result


def main():
    command = json.loads(sys.stdin.readline())
    home = Path(command['home'])
    opens = []
    def audit(event, args):
        if event == 'sqlite3.connect' and 'state.db' in str(args[0]):
            import traceback
            opens.append({'path': str(args[0]), 'stack': traceback.format_stack(limit=12)})
    sys.addaudithook(audit)
    rpc = WorkerRPC(home)
    scope = dict(profile_id=str(home), session_id=command['session_id'],
                 execution_id='agent-worker', generation=0, pid=os.getpid(),
                 birth=psutil.Process().create_time(), secret='private-agent-worker')
    registered = rpc('worker.register', **scope, kind='compute')
    scope['epoch'] = registered['owner_epoch']
    store = RuntimeSessionStore(rpc, scope, home / 'worker-outboxes' / 'agent-worker')
    from run_agent import AIAgent
    agent = AIAgent(model='worker-model', provider='custom', base_url=command['url'], api_key='loopback-only',
                    session_db=store, session_id=command['session_id'], enabled_toolsets=[],
                    skip_context_files=True, skip_memory=True, skip_background_review=True, quiet_mode=True)
    try:
        result = agent.run_conversation('WORKER_INFERENCE')
        context = store.get_session(command['session_id'])
        store.flush_token_counts()
        finished = store.finish()
        receipt = {'result': result['final_response'], 'failed': result.get('failed'),
                   'context': context, 'finished': finished, 'opens': opens, 'fds': writable_fds()}
        Path(command['receipt']).write_text(json.dumps(receipt, default=str))
    finally:
        agent._end_session_on_close = False  # session lifecycle belongs to the authority
        agent.close()
        store.close()


if __name__ == '__main__':
    main()
