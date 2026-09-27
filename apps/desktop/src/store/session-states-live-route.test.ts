import { afterEach, describe, expect, it } from 'vitest'

import { liveRoutedSessionId } from './session-states'

// Branch → immediate Enter: navigate() had already rewritten location.hash to
// the branch, but the render-time route copy still named the parent, so the
// send's route/selection guard saw nothing to reconcile and U5 landed in the
// parent (lineage-sidebar.spec.ts "U5 landed in a NEW session", ~1 in 28 runs
// on a 2-core box). The send must honor the live hash over the stale copy.
describe('liveRoutedSessionId', () => {
  afterEach(() => {
    window.history.replaceState(null, '', window.location.pathname)
  })

  it('prefers the live hash route over a stale render-time copy', () => {
    window.location.hash = '#/local-branch'

    expect(liveRoutedSessionId('local-parent')).toBe('local-branch')
  })

  it('treats a new-chat hash as authoritative even when the copy still names a session', () => {
    window.location.hash = '#/'

    expect(liveRoutedSessionId('local-parent')).toBeNull()
  })

  it('falls back to the render-time copy when there is no hash at all', () => {
    expect(window.location.hash).toBe('')
    expect(liveRoutedSessionId('local-parent')).toBe('local-parent')
  })
})
