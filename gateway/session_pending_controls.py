"""Generation-bound projection/control of the existing blocking approval queue.

Ordinary approvals and clarify prompts share the existing native waiters.
Secret/sudo prompts remain native; no response is published or journaled here.
"""
import json
from copy import deepcopy

from hermes_state_runtime import RuntimeStoreError
from tools.approval import list_gateway_approvals, resolve_gateway_approval


class PendingControls:
    def __init__(self, events):
        self.events = events
        self.pending = {}
        self.remote_responders = {}

    def register(self, session_id, route, generation, data):
        from gateway.run import _redact_approval_command
        with self.events.lock:
            prompt_id = data['request_id']
            choices = ['once', 'deny']
            if data.get('allow_session', True) and not data.get('smart_denied', False):
                choices.append('session')
            if data.get('allow_permanent', True) and not data.get('smart_denied', False):
                choices.append('always')
            prompt = {'kind': 'approval', 'prompt_id': prompt_id,
                      'execution_generation': generation,
                      'command': _redact_approval_command(data.get('command', '')),
                      'description': _redact_approval_command(data.get('description', '')),
                      'choices': choices}
            if 'edit' in data:
                prompt['edit'] = deepcopy(data['edit'])
                prompt['choices'] = ['once', 'deny']
            self.pending[prompt_id] = (route, prompt)
            self.events.publish(session_id, prompt, event_type='approval.request')

    def register_clarify(self, session_id, generation, entry, questions=None):
        """Publish one clarify prompt in the clarify tool's ``questions`` shape. ``questions``
        defaults to the single question the native ``entry`` carries (one messaging card)."""
        from gateway.run import _redact_approval_command
        if questions is None:
            questions = [{'qid': 'q0', 'question': entry.question, 'choices': entry.choices,
                          'multi_select': entry.multi_select}]
        wire = [{'qid': q['qid'], 'question': _redact_approval_command(q['question']),
                 'choices': [_redact_approval_command(c) for c in q['choices'] or []],
                 'multi_select': bool(q['multi_select'])} for q in questions]
        with self.events.lock:
            prompt = {"kind": "clarify", "prompt_id": entry.clarify_id,
                      "execution_generation": generation, "questions": wire}
            if len(wire) == 1:
                # Single-question readers (the /v1 run projection, the CLI attach view).
                prompt.update(question=wire[0]['question'], choices=wire[0]['choices'],
                              multi_select=wire[0]['multi_select'])
            self.pending[entry.clarify_id] = (entry, prompt)
            self.events.publish(session_id, prompt, event_type="clarify.request")

    def snapshot(self, session_id, generation):
        with self.events.lock:
            for prompt_id, (route, prompt) in list(self.pending.items()):
                active = (prompt_id in self.remote_responders or
                          (not route.event.is_set() if prompt['kind'] == 'clarify' else
                           any(p['request_id'] == prompt_id for p in list_gateway_approvals(route))))
                if prompt['execution_generation'] != generation or not active:
                    del self.pending[prompt_id]
                    self.remote_responders.pop(prompt_id, None)
                    self.events.publish(session_id, {'prompt_id': prompt_id,
                        'execution_generation': prompt['execution_generation']}, event_type=prompt['kind'] + '.settled')
            return tuple(deepcopy(prompt) for _, prompt in self.pending.values())

    def respond(self, session_id, generation, prompt_id, response, *, kind="approval"):
        with self.events.lock:
            self.snapshot(session_id, generation)
            entry = self.pending.get(prompt_id)
            if entry is None:
                return {'status': 'already_resolved', 'prompt_id': prompt_id}
            route, prompt = entry
            if prompt['kind'] != kind:
                raise RuntimeStoreError('invalid_params')
            if kind == 'clarify':
                reply = _clarify_reply(prompt, response)
            elif (not isinstance(response, dict) or set(response) != {'choice'}
                    or response['choice'] not in prompt['choices']):
                raise RuntimeStoreError('invalid_params')
            if prompt_id in self.remote_responders:
                value = json.dumps(reply, ensure_ascii=False) if kind == 'clarify' else response['choice']
                self.remote_responders[prompt_id](kind, prompt_id, value)
                del self.remote_responders[prompt_id]
                del self.pending[prompt_id]
                self.events.publish(session_id, {'prompt_id': prompt_id, 'execution_generation': generation},
                                    event_type=kind + '.settled')
                return {'status': 'resolved', 'prompt_id': prompt_id}
            if kind == 'clarify':
                from tools.clarify_gateway import CANCELLED, SKIPPED, resolve_gateway_clarify
                # A native card asks exactly one question.
                answer = reply['answers'].get(prompt['questions'][0]['qid'])
                value = CANCELLED if reply['outcome'] == 'cancelled' else answer or SKIPPED
                resolved = resolve_gateway_clarify(prompt_id, value)
                self.snapshot(session_id, generation)
                return {'status': 'resolved' if resolved else 'already_resolved', 'prompt_id': prompt_id}
            # The tools lock arbitrates native taps versus attached responders;
            # an exact request ID can never consume the next FIFO approval.
            resolved = resolve_gateway_approval(route, response['choice'], request_id=prompt_id)
            self.snapshot(session_id, generation)
            return {'status': 'resolved' if resolved else 'already_resolved', 'prompt_id': prompt_id}


def _clarify_reply(prompt, response):
    """The clarify tool's callback reply from a client response. ``{"answers": {qid: text | null}}``
    submits (null = skipped); no ``answers`` cancels; ``{"answer": text}`` answers a
    single-question prompt ('' = skipped)."""
    qids = [q['qid'] for q in prompt['questions']]
    if not isinstance(response, dict) or set(response) - {'answers', 'answer'} or len(response) > 1:
        raise RuntimeStoreError('invalid_params')
    if 'answer' in response:
        if len(qids) != 1 or not isinstance(response['answer'], str):
            raise RuntimeStoreError('invalid_params')
        response = {'answers': {qids[0]: response['answer'] or None}}
    if 'answers' not in response:
        return {'answers': {}, 'outcome': 'cancelled'}
    answers = response['answers']
    if (not isinstance(answers, dict) or set(answers) - set(qids)
            or any(v is not None and (not isinstance(v, str) or len(v) > 16384) for v in answers.values())):
        raise RuntimeStoreError('invalid_params')
    return {'answers': dict(answers), 'outcome': 'submitted'}
