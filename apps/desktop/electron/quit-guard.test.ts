import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type ActiveWork, backendOwnedByApp, mergeActiveWork, normalizeActiveWork, quitPromptFor, shouldGuardWindowClose } from './quit-guard'

test('normalizeActiveWork drops junk and keeps the count at least the title count', () => {
  assert.deepEqual(normalizeActiveWork(null), { count: 0, titles: [] })
  assert.deepEqual(normalizeActiveWork({ count: 'many', titles: 'nope' }), { count: 0, titles: [] })
  assert.deepEqual(normalizeActiveWork({ count: -3, titles: ['  Fix login  ', '', 7] }), {
    count: 1,
    titles: ['Fix login']
  })
})

test('normalizeActiveWork keeps untitled sessions in the count', () => {
  assert.deepEqual(normalizeActiveWork({ count: 3, titles: ['Fix login'] }), { count: 3, titles: ['Fix login'] })
})

test('mergeActiveWork de-dupes a session two windows both report', () => {
  const merged = mergeActiveWork([
    { count: 2, titles: ['Fix login', 'Ship docs'] },
    { count: 1, titles: ['Fix login'] }
  ])

  assert.deepEqual(merged, { count: 2, titles: ['Fix login', 'Ship docs'] })
})

test('quitPromptFor stays out of the way when nothing is running', () => {
  assert.equal(quitPromptFor({ count: 0, titles: [] }, false), null)
})

test('quitPromptFor stays out of the way during an update handoff', () => {
  const work = mergeActiveWork([normalizeActiveWork({ count: 2, titles: ['Fix login'] })])

  assert.ok(quitPromptFor(work, false))
  assert.equal(quitPromptFor(work, true), null)
})

test('quitPromptFor names the running chats', () => {
  const prompt = quitPromptFor({ count: 2, titles: ['Fix login', 'Ship docs'] }, false)

  assert.ok(prompt)
  assert.ok(prompt.detail.includes('• Fix login'))
  assert.ok(prompt.detail.includes('• Ship docs'))
})

test('quitPromptFor summarizes past the list cap and counts untitled work', () => {
  const prompt = quitPromptFor({ count: 9, titles: ['a', 'b', 'c', 'd', 'e', 'f'] }, false)

  assert.ok(prompt)
  assert.ok(prompt.detail.includes('• d'))
  assert.ok(!prompt.detail.includes('• e'))
  assert.ok(prompt.detail.includes('• 5 more'))
})

// #79579: only a backend the app owns (spawned locally, or started over SSH)
// dies with it. A remote URL or Hermes Cloud backend keeps the turn running
// after the app quits, so the prompt must not claim the work is lost.
test('backendOwnedByApp: a local primary is owned even before its child attaches', () => {
  assert.equal(backendOwnedByApp({ ownedBackendCount: 0, primaryRouteKind: null }), true)
})

test('backendOwnedByApp: an SSH primary is owned (the app starts and stops that server)', () => {
  assert.equal(backendOwnedByApp({ ownedBackendCount: 0, primaryRouteKind: 'ssh' }), true)
})

test('backendOwnedByApp: a remote URL or cloud primary with nothing spawned is not owned', () => {
  assert.equal(backendOwnedByApp({ ownedBackendCount: 0, primaryRouteKind: 'remote' }), false)
  assert.equal(backendOwnedByApp({ ownedBackendCount: 0, primaryRouteKind: 'cloud' }), false)
})

test('backendOwnedByApp: a remote primary alongside a spawned backend stays owned', () => {
  // Another window/profile may be running its turn on that local child.
  assert.equal(backendOwnedByApp({ ownedBackendCount: 1, primaryRouteKind: 'remote' }), true)
})

test('quitPromptFor warns about lost work when the app owns the backend (local)', () => {
  const owned = backendOwnedByApp({ ownedBackendCount: 1, primaryRouteKind: null })
  const prompt = quitPromptFor({ count: 1, titles: ['Fix login'] }, false, owned)

  assert.ok(prompt)
  assert.equal(prompt.message, 'Hermes is still working on 1 chat.')
})

test('active-work reports with unknown lifecycle keep a scoped confirmation, not a work-loss claim', () => {
  // The real IPC summary has no connection identity or Desktop-tool activity.
  // Neither named nor untitled work proves it is independent of the client.
  for (const titles of [[], ['Fix login']]) {
    const work = mergeActiveWork([normalizeActiveWork({ count: 1, titles })])
    const prompt = quitPromptFor(work, false)

    assert.ok(prompt)
    assert.match(prompt.detail, /[Rr]unning and queued work on a persistent gateway continues after Desktop quits/)
    assert.match(prompt.detail, /[Aa]ctivity that depends on this app or an older connection may be interrupted/)
    assert.doesNotMatch(prompt.detail, /work.*(?:lost|stops)|stops the agent|all work continues/i)
  }
})
