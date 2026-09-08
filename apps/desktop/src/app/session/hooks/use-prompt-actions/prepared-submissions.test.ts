import { afterEach, expect, test, vi } from 'vitest'

import { type PreparedSubmission, readPreparedSubmission, removePreparedSubmission, writePreparedSubmission } from './prepared-submissions'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

test('native preparation waits for acknowledgement and never downgrades a write failure to browser storage', async () => {
  let ack!: () => void
  const gate = new Promise<void>(resolve => { ack = resolve })
  const entry = { id: 'a', text: 'Ω\n  exact', attachments: [], params: { session_id: 'live' } } as unknown as PreparedSubmission
  const native = { read: vi.fn(async () => JSON.stringify({ key: entry })), update: vi.fn(() => gate) }
  vi.stubGlobal('hermesDesktop', { preparedSubmissions: native })
  let finished = false
  const writing = writePreparedSubmission('key', entry).then(() => { finished = true })
  await Promise.resolve()
  expect(finished).toBe(false)
  expect(native.update).toHaveBeenCalledWith('key', JSON.stringify(entry))
  ack(); await writing
  expect(await readPreparedSubmission('key')).toEqual(entry)
  native.update.mockRejectedValueOnce(new Error('disk full'))
  await expect(writePreparedSubmission('key', entry)).rejects.toThrow('disk full')
  expect(localStorage.length).toBe(0)
  await removePreparedSubmission('key')
  expect(native.update).toHaveBeenLastCalledWith('key', null)
  vi.stubGlobal('hermesDesktop', undefined)
  await writePreparedSubmission('key', entry)
  expect(await readPreparedSubmission('key')).toEqual(entry)
  await removePreparedSubmission('key')
  expect(await readPreparedSubmission('key')).toBeUndefined()
})
