import { expect, test } from 'vitest'

import { CanonicalDesktopProtocol } from './canonical-protocol'

test('canonical create retries retain identity and reject unsupported explicit intent', () => {
  const protocol = new CanonicalDesktopProtocol()
  const params = { source: 'desktop', profile: 'default', cols: 96, fast: false, cwd: '/tmp' }
  const first = protocol.prepare('session.create', params)
  expect(first).toMatchObject({ source: 'gui', cwd: '/tmp', request_id: expect.any(String) })
  expect(protocol.prepare('session.create', { ...params })).toEqual(first)
  expect(first).not.toHaveProperty('profile')
  expect(() => protocol.prepare('session.create', { ...params, fast: true })).toThrow('fast')
  expect(() => protocol.prepare('session.create', { ...params, provider: 'custom' })).toThrow('provider')
  protocol.result('session.create', first, { session_id: 'canonical', stored_session_id: 'canonical' })
  expect(protocol.prepare('session.create', params).request_id).not.toBe(first.request_id)
})

test('canonical receipts preserve admission identity and replay restores fenced controls', () => {
  const protocol = new CanonicalDesktopProtocol()
  const receipt = protocol.result('prompt.submit', { session_id: 's', submission_id: 'input' }, { admission_id: 'admission', ref: { session_id: 's', profile_id: '/tmp/profile' }, status: 'queued' })
  expect(receipt).toMatchObject({ admission_id: 'admission', submission_id: 'input', session_id: 's' })
  expect(() => protocol.result('prompt.submit', { session_id: 'other', submission_id: 'input' }, receipt)).toThrow('destination')
  const resumed = protocol.result('session.resume', { session_id: 's' }, { session_id: 's', stored_session_id: 's', execution_generation: 3, prompts: [{ kind: 'approval', prompt_id: 'p', execution_generation: 3, command: 'test' }] })
  expect(resumed.pending_approval).toMatchObject({ request_id: 'p', execution_generation: 3 })
  expect(protocol.prepare('approval.respond', { session_id: 's', request_id: 'p', choice: 'once' })).toEqual({ session_id: 's', execution_generation: 3, prompt_id: 'p', choice: 'once' })
  expect(protocol.prepare('session.interrupt', { session_id: 's' })).toEqual({ session_id: 's', execution_generation: 3 })
  protocol.event({ type: 'message.start', session_id: 's', payload: { execution_generation: 4 } })
  expect(() => protocol.prepare('approval.respond', { session_id: 's', request_id: 'p', choice: 'once' })).toThrow('stale')
})
