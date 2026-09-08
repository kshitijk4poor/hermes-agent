"""Trusted producer admission; not a client-selectable internal input flag.

Reuse the private native route envelope and FIFO, retaining nonhuman turn semantics.
A producer ACK means committed input, not successful inference or outbound delivery.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session_contract import SessionRef
from hermes_state_runtime import RuntimeStoreError, admit_session_input


def producer_identity(runner, event):
    identity = runner._completion_delivery_identity(event)
    if identity is None:
        raise RuntimeStoreError('invalid_params')
    return json.dumps(identity, separators=(',', ':'))


def completion_admission(runner, event):
    authority = getattr(runner, 'session_authority', None)
    if authority is None:
        return None
    entry = runner.session_store.lookup_by_session_key(str(event.get('session_key') or ''))
    if entry is None:
        return None
    from hermes_state_runtime import list_session_admissions
    identity = producer_identity(runner, event)
    for row in list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False):
        descriptor = row['payload'].get('native_text_v1', {}).get('automation', {})
        if identity in descriptor.get('identities', [descriptor.get('identity')]):
            return row
    return None


def _owner(runner, event):
    route = event.metadata.get('gateway_session_key') or runner.session_store._generate_session_key(event.source)
    entry = runner.session_store.lookup_by_session_key(route)
    if entry is None or entry.suspended:
        raise RuntimeStoreError('not_found')
    if runner.session_store._generate_session_key(event.source) != route:
        raise RuntimeStoreError('admission_conflict')
    expected = event.metadata.get('gateway_session_id')
    if expected and expected != entry.session_id:
        # A completed child may follow compression, but never /new or an unrelated resume.
        if runner.session_authority.db.get_compression_tip(expected) != entry.session_id:
            raise RuntimeStoreError('admission_conflict')
    return entry


def snapshot_automation(authority, adapter, event, identity):
    runner = authority.runner
    if (not event.internal or event.message_type != MessageType.TEXT or event.is_command()
            or not isinstance(event.text, str) or not identity
            or event.media_urls or event.prompt_response or event.source.platform == Platform.API_SERVER
            or set(event.metadata) - {'gateway_session_key', 'gateway_session_id', 'automation_identities'}):
        raise RuntimeStoreError('invalid_params')
    entry = _owner(runner, event)
    if event.source.platform == Platform.LOCAL:
        # Local frozen policy uses a different payload preflight; do not ACK work
        # until that consumer supports a private internal envelope too.
        raise RuntimeStoreError('invalid_params')
    from gateway.session_envelope import restore_native
    from hermes_state_runtime import list_session_admissions
    prior = list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)
    envelope = next((r['payload']['native_text_v1'] for r in reversed(prior)
                     if 'native_text_v1' in r['payload']), None)
    if envelope is None or 'provenance' not in envelope:
        raise RuntimeStoreError('not_found')
    # Persisted origins deliberately omit relay trust. Borrow only an exact
    # committed source's private proof, revalidated against the live connector.
    restored = restore_native({'text': event.text, 'native_text_v1': envelope}, runner)
    if (restored.source.to_dict() != event.source.to_dict()
            or runner._adapter_for_source(restored.source) is not adapter):
        raise RuntimeStoreError('admission_conflict')
    provenance = deepcopy(envelope['provenance'])
    source = deepcopy(envelope['source'])
    # Producer timestamps and platform reply IDs change on retry. Neither belongs
    # in the identity/fingerprint of the same immutable completion.
    envelope = {'source': source, 'route': entry.session_key,
        'timestamp': datetime.fromtimestamp(0, timezone.utc).isoformat(),
        'event': {'message_id': identity}, 'provenance': provenance,
        'automation': {'identity': identity, 'owner': entry.session_id}}
    identities = event.metadata.get('automation_identities')
    if identities:
        envelope['automation']['identities'] = sorted(set(identities))
    return {'text': event.text, 'native_text_v1': envelope}, entry


def check_automation_route(runner, payload, session_id, available_source, adapter):
    from gateway.session_envelope import restore_native
    event = restore_native(payload, runner)
    envelope = payload['native_text_v1']
    entry = _owner(runner, event)
    if (entry.session_id != session_id or envelope['automation']['owner'] != session_id
            or runner.session_store._generate_session_key(available_source) != entry.session_key
            or adapter is None or runner._adapter_for_source(event.source) is not adapter):
        raise RuntimeStoreError('admission_conflict')
    return event.source, entry.session_key


async def admit_automation(authority, adapter, event, identity):
    authority._require_admission_open()
    payload, entry = snapshot_automation(authority, adapter, event, identity)
    from gateway.session_authority import LiveSession
    ref = SessionRef(authority.profile_id, entry.session_id)
    authority.sessions.setdefault(ref.session_id, LiveSession(event.source, entry.session_key))
    row = admit_session_input(authority.db, epoch=authority.epoch, principal_id='automation:' + entry.session_key,
        session_id=ref.session_id, request_id=identity, payload=deepcopy(payload))
    event._gateway_accepted = True
    authority._publish_pending(ref)
    authority._schedule(ref)
    return authority._receipt(row)
