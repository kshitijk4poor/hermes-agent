"""Owner-only durable native input codec; never a client-supplied wire format.

Ordinary context and local media are retained without serializing delegated trust.
Multiplex/role/relay authorization still requires a current server-owned grant.
"""
from copy import deepcopy
from datetime import datetime

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.session_ingress_media import capture_native_media, restore_native_media
from hermes_state_runtime import RuntimeStoreError

_EVENT_FIELDS = (
    'user_id', 'user_name', 'message_id', 'platform_update_id',
    'reply_to_message_id', 'reply_to_text', 'reply_to_author_id',
    'reply_to_author_name', 'reply_to_is_own_message', 'allow_gateway_control',
)
_CONTEXT_FIELDS = ('auto_skill', 'channel_prompt', 'channel_context')


def _validate_native(runner, event):
    source = event.source
    if (source is None or not isinstance(event.text, str)
            or not isinstance(event.message_type, MessageType)
            or event.message_type == MessageType.COMMAND or event.is_command()
            or event.internal or event.metadata or event.prompt_response
            or source.role_authorized or source.delivered_via_upstream_relay
            or source.profile_route_rejected
            or getattr(source, '_authorization_profile_home', None) is not None
            or getattr(runner.config, 'multiplex_profiles', False)):
        raise RuntimeStoreError('invalid_params')
    if not runner._is_user_authorized_for_source(source, allow_adapter_delegation=False):
        raise RuntimeStoreError('permission_denied')
    if any(value is not None and not isinstance(value, str)
           for value in (event.channel_prompt, event.channel_context)):
        raise RuntimeStoreError('invalid_params')
    skill = event.auto_skill
    if not (skill is None or isinstance(skill, str)
            or isinstance(skill, list) and all(isinstance(item, str) for item in skill)):
        raise RuntimeStoreError('invalid_params')
    if (not isinstance(event.media_urls, list) or not all(isinstance(item, str) for item in event.media_urls)
            or not isinstance(event.media_types, list) or not all(isinstance(item, str) for item in event.media_types)
            or not isinstance(event.media_text_inlined, list)
            or not all(item is None or type(item) is bool for item in event.media_text_inlined)
            or len(event.media_types) not in (0, len(event.media_urls))
            or len(event.media_text_inlined) not in (0, len(event.media_urls))):
        raise RuntimeStoreError('invalid_params')
    encoded_source = source.to_dict()
    encoded_source['is_bot'] = source.is_bot
    restored_source = SessionSource.from_dict(encoded_source)
    restored_source.is_bot = source.is_bot
    adapter = runner._adapter_for_source(source)
    if adapter is None or runner._adapter_for_source(restored_source) is not adapter:
        raise RuntimeStoreError('not_found')
    return encoded_source


def snapshot_native(runner, event):
    encoded_source = _validate_native(runner, event)
    envelope = {'source': encoded_source,
                'route': runner.session_store._generate_session_key(event.source),
                'event': deepcopy({name: getattr(event, name) for name in _EVENT_FIELDS}),
                'timestamp': event.timestamp.isoformat()}
    # Omit new defaults so an identical retry of an older text admission retains
    # its fingerprint. Explicit context (including an empty skill list) is exact.
    envelope['event'].update({name: deepcopy(getattr(event, name)) for name in _CONTEXT_FIELDS
                              if getattr(event, name) is not None})
    if event.message_type != MessageType.TEXT:
        envelope['message_type'] = event.message_type.value
    if event.media_urls:
        envelope['media'] = capture_native_media(event.media_urls)
        envelope['event'].update(media_types=list(event.media_types),
                                 media_text_inlined=list(event.media_text_inlined))
    # Keep the private dispatch key so previously committed text-only rows recover
    # under the same authority; optional media/context fields extend that envelope.
    return {'text': event.text, 'native_text_v1': envelope}


def restore_native(payload):
    envelope = payload['native_text_v1']
    source = SessionSource.from_dict(envelope['source'])
    source.is_bot = envelope['source']['is_bot']
    return MessageEvent(text=payload['text'], source=source,
                        timestamp=datetime.fromisoformat(envelope['timestamp']),
                        message_type=MessageType(envelope.get('message_type', 'text')),
                        media_urls=restore_native_media(envelope.get('media', [])),
                        **deepcopy(envelope['event']))


def check_native_route(runner, payload, session_id, available_source, adapter):
    """Read-only preflight: route/auth rejection must never consume a queued row."""
    event = restore_native(payload)
    # Validate the stored sender without recapturing files or trusting the binding caller.
    _validate_native(runner, event)
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
