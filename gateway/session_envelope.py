"""Owner-only durable native text codec; never a client-supplied wire format.

Media and delegated/multiplex trust need their own durable authorization contract.
Until then they are refused before admission, not silently downgraded on restart.
"""
from datetime import datetime

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_state_runtime import RuntimeStoreError

_EVENT_FIELDS = (
    'user_id', 'user_name', 'message_id', 'platform_update_id',
    'reply_to_message_id', 'reply_to_text', 'reply_to_author_id',
    'reply_to_author_name', 'reply_to_is_own_message', 'allow_gateway_control',
)


def snapshot_native(runner, event):
    source = event.source
    if (source is None or not isinstance(event.text, str)
            or event.message_type != MessageType.TEXT or event.is_command()
            or event.media_urls or event.media_types or event.media_text_inlined
            or event.internal or event.metadata or event.prompt_response
            or event.auto_skill or event.channel_prompt or event.channel_context
            or source.role_authorized or source.delivered_via_upstream_relay
            or source.profile_route_rejected
            or getattr(source, '_authorization_profile_home', None) is not None
            or getattr(runner.config, 'multiplex_profiles', False)):
        raise RuntimeStoreError('invalid_params')
    if not runner._is_user_authorized_for_source(source, allow_adapter_delegation=False):
        raise RuntimeStoreError('permission_denied')
    encoded_source = source.to_dict()
    encoded_source['is_bot'] = source.is_bot
    envelope = {'source': encoded_source,
                'route': runner.session_store._generate_session_key(source),
                'event': {name: getattr(event, name) for name in _EVENT_FIELDS},
                'timestamp': event.timestamp.isoformat()}
    payload = {'text': event.text, 'native_text_v1': envelope}
    restored = restore_native(payload)
    adapter = runner._adapter_for_source(source)
    if adapter is None or runner._adapter_for_source(restored.source) is not adapter:
        raise RuntimeStoreError('not_found')
    return payload


def restore_native(payload):
    envelope = payload['native_text_v1']
    source = SessionSource.from_dict(envelope['source'])
    source.is_bot = envelope['source']['is_bot']
    return MessageEvent(text=payload['text'], source=source,
                        timestamp=datetime.fromisoformat(envelope['timestamp']),
                        **envelope['event'])


def check_native_route(runner, payload, session_id, available_source, adapter):
    """Read-only preflight: route/auth rejection must never consume a queued row."""
    event = restore_native(payload)
    # Validate the stored sender, never an identity supplied by the binding caller.
    snapshot_native(runner, event)
    route = payload['native_text_v1']['route']
    store = runner.session_store
    if (store._generate_session_key(event.source) != route
            or store._generate_session_key(available_source) != route):
        raise RuntimeStoreError('admission_conflict')
    entry = store.lookup_by_session_key(route)
    if entry is None or entry.session_id != session_id:
        raise RuntimeStoreError('admission_conflict')
    if adapter is None or runner._adapter_for_source(event.source) is not adapter:
        raise RuntimeStoreError('not_found')
    return event.source, route
