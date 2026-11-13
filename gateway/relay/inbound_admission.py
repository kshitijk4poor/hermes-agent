"""Native admission of a connector-delivered relay message (``RelayAdapter._on_inbound``)."""
import asyncio
import logging

from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger("gateway.relay.adapter")


async def admit_relay_delivery(adapter, runner, event):
    """Admit *event*; the transport ACKs the frame once this returns, or once the returned task does.

    Normally admission waits only for the SQLite receipt, on the socket reader. Code that may await
    ``adapter.send()``, whose ``outbound_result`` only that reader delivers, or wait for another inbound
    message runs in a task off the reader instead, after any earlier admission of the same chat, so one
    chat's deliveries still commit in arrival order. That code is a ``pre_gateway_dispatch`` or
    ``post_gateway_admission`` plugin, or answering a prompt a running turn blocks on (any pending
    prompt, since the reader cannot name the delivery's session before routing it). A chat whose lane
    still holds an admission stays in the lane."""
    from gateway.run_inbound_consumer import POST_ADMISSION_HOOK
    from hermes_cli.lifecycle import has_hook
    lanes = adapter._admission_lanes
    lane = (event.source.platform.value, event.source.chat_id)
    previous = lanes.get(lane)
    # A reply to a prompt a running turn blocks on (``_hm_answer_pending_prompt``) can send too: a
    # plain-text approval's confirmation, a slash-confirm's command. Before the delivery is routed
    # the reader cannot name its session, so any pending prompt moves admission off the reader; a
    # chat with an admission still in its lane stays there, or this one could commit before it.
    if not (has_hook('pre_gateway_dispatch') or has_hook(POST_ADMISSION_HOOK)
            or runner._hm_any_pending_reply_prompt() or previous is not None):
        # A prompt opening after that check is not answered from here: answering sends on this reader.
        event._admitted_on_reader = True
        await _admit(adapter, runner, event)
        return None
    task = asyncio.create_task(_admit_after(previous, adapter, runner, event))
    lanes[lane] = task
    task.add_done_callback(lambda done: lanes.pop(lane) if lanes.get(lane) is done else None)
    adapter._background_tasks.add(task)  # cancelled with the adapter's work on shutdown
    task.add_done_callback(adapter._background_tasks.discard)
    return task


async def _admit_after(previous, adapter, runner, event):
    if previous is not None:
        await asyncio.wait([previous])  # its outcome is its own frame's ACK, not this one's
    await _admit(adapter, runner, event)


async def _admit(adapter, runner, event):
    # A permanent refusal returns, so the transport ACKs it (a replay would be refused again: main's
    # runner dropped it too); any other failure withholds the ACK so the connector redelivers. A
    # parked target is refused to the sender and ACKed: it stays parked until a rescan or a restart,
    # and a withheld frame would stall the connector's ordered, ack-gated buffer drain for every
    # profile on this instance (a live frame is never redelivered anyway).
    from gateway.parked_route_refusal import ParkedProfileRefusal, refuse_parked_route
    from gateway.platforms.webhook_ingress import admit_producer
    from gateway.relay.adapter import _PERMANENT_REFUSALS
    try:
        receipt = await admit_producer(adapter, event)
    except ParkedProfileRefusal as exc:
        refuse_parked_route(adapter, event.source, exc.profile, reply_to=event.message_id)
        return
    except RuntimeStoreError as exc:
        if exc.reason not in _PERMANENT_REFUSALS:
            raise
        logger.warning("relay inbound refused (%s) for %s chat=%s; acknowledged, not replayed",
                       exc.reason, event.source.platform.value, event.source.chat_id)
        return
    if receipt is None and getattr(event, '_consumer_reply', None):
        # A post_gateway_admission plugin consumed it, or it answered a pending prompt (never
        # committed); its reply is sent off this reader, like an executed admission's.
        from gateway.session_ingress import deliver_response
        task = asyncio.create_task(deliver_response(
            adapter, event, runner._session_key_for_source(event.source), event._consumer_reply))
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)
