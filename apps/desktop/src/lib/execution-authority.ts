interface ExecutionAuthority {
  epoch: string
  generation: number
  terminal: boolean
  retiredEpochs: Set<string>
}

/** Each transport consumer owns its map; epochs are opaque, never ordered. */
export function acceptExecutionEvent(
  authorities: Map<string, ExecutionAuthority>,
  key: string,
  type: string,
  payload?: Record<string, unknown>
): boolean {
  if (!['session.info', 'message.start', 'message.complete', 'message.error'].includes(type)) {return true}
  const previous = authorities.get(key)
  const epoch = payload?.execution_epoch
  const generation = payload?.execution_generation
  const versioned = typeof epoch === 'string' && epoch.length > 0 && typeof generation === 'number' && Number.isSafeInteger(generation) && generation >= 0

  if (!versioned) {return !previous}
  const terminal = type === 'message.complete' || type === 'message.error' || payload?.running === false

  if (previous) {
    if (previous.retiredEpochs.has(epoch)) {return false}

    if (previous.epoch === epoch) {
      if (generation < previous.generation) {return false}

      if (generation === previous.generation && previous.terminal && (type === 'message.start' || payload?.running === true)) {return false}
    } else {
      // Only an owner snapshot/start can establish a new epoch, not a late finalizer.
      if (type !== 'session.info' && type !== 'message.start') {return false}
      previous.retiredEpochs.add(previous.epoch)
    }
  }

  authorities.set(key, { epoch, generation, terminal: terminal || Boolean(previous?.epoch === epoch && previous.generation === generation && previous.terminal), retiredEpochs: previous?.retiredEpochs ?? new Set() })

  return true
}
