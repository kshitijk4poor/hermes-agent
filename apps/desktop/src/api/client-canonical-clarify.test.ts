import type { ServerRequest } from '@hermes/shared'
import { describe, expect, it, vi } from 'vitest'

import { HermesGateway } from './client'

interface CanonicalInternals {
  canonical: boolean
  deliverCanonicalPrompt: (event: { payload: unknown; session_id: string; type: string }, replayed: boolean) => void
}

/** A canonical gateway with `clarify.respond` captured and one prompt delivered to the card registry. */
function canonicalClarify(questions: Array<{ qid: string; question: string }>) {
  const gateway = new HermesGateway()
  const internals = gateway as unknown as CanonicalInternals
  internals.canonical = true
  const original = gateway.request.bind(gateway)
  const responds: Record<string, unknown>[] = []

  vi.spyOn(gateway, 'request').mockImplementation(async (method, params) => {
    if (method === 'clarify.respond') {
      responds.push(params ?? {})

      return { status: 'resolved' } as never
    }

    return original(method, params)
  })

  const delivered: ServerRequest[] = []
  gateway.onRequest(request => void delivered.push(request))
  internals.deliverCanonicalPrompt(
    {
      payload: { execution_generation: 1, kind: 'clarify', prompt_id: 'prompt-1', questions },
      session_id: 'session-1',
      type: 'clarify.request'
    },
    false
  )

  return { delivered, gateway, responds }
}

describe('HermesGateway canonical clarify', () => {
  const questions = [
    { qid: 'q0', question: 'Color?' },
    { qid: 'q1', question: 'Name?' }
  ]

  it('delivers the questions and sends the locked set as one clarify.respond (null = skipped)', async () => {
    const { delivered, gateway, responds } = canonicalClarify(questions)

    expect(delivered[0]?.params.questions).toEqual(questions)
    expect(await gateway.request('clarify.lock', { answer: 'red', question_id: 'q0', request_id: 'prompt-1' })).toEqual(
      { remaining: ['q1'], status: 'ok' }
    )
    expect(responds).toEqual([])
    expect(await gateway.request('clarify.lock', { answer: null, question_id: 'q1', request_id: 'prompt-1' })).toEqual(
      { remaining: [], status: 'ok' }
    )
    expect(responds).toEqual([{ answers: { q0: 'red', q1: null }, request_id: 'prompt-1', session_id: 'session-1' }])

    // The prompt is settled: a late cancel from the card sends nothing more.
    delivered[0]?.respond({})
    expect(responds).toHaveLength(1)
  })

  it('cancels with a clarify.respond that carries no answers', () => {
    const { delivered, responds } = canonicalClarify(questions)

    delivered[0]?.respond({})

    expect(responds).toEqual([{ request_id: 'prompt-1', session_id: 'session-1' }])
  })
})
