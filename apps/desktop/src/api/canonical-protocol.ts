// Canonical local authority wire adapter. Remote legacy transports keep their
// existing protocol; unsupported explicit semantics fail before network admission.
export class CanonicalDesktopProtocol {
  private creates = new Map<string, string>()
  private revisions = new Map<string, number>()
  private mutations = new Map<string, Record<string, unknown>>()
  private generations = new Map<string, number>()
  private prompts = new Map<string, Record<string, unknown>>()

  failure(params: Record<string, unknown>, error: unknown) {
    if ((error as { data?: { reason?: string } })?.data?.reason !== 'revision_conflict') { return }

    // A confirmed CAS refusal did not mutate. Ambiguous transport failures keep
    // the original revision/id so a retry cannot overwrite another user's edit.
    for (const [key, mutation] of this.mutations) { if (mutation.request_id === params.request_id) { this.mutations.delete(key) } }
  }

  prepare(method: string, params: Record<string, unknown>): Record<string, unknown> {
    const field = ({ 'session.title': 'title', 'session.archive': 'archived' } as Record<string, string>)[method]

    if (field) {
      const key = JSON.stringify([params.session_id, field, params[field]])
      const retained = this.mutations.get(key)

      if (retained) { return retained }
      const revision = this.revisions.get(String(params.session_id))

      if (revision === undefined) { throw new Error('Session revision unavailable; reopen the session before editing metadata') }

      const mutation = { session_id: params.session_id, request_id: crypto.randomUUID(), expected_revision: revision,
        operation: field === 'title' ? 'rename' : 'archive', payload: { [field]: params[field] } }

      this.mutations.set(key, mutation)

      return mutation
    }

    if (method === 'session.create') {
      const allowed = new Set(['request_id', 'source', 'cwd', 'model', 'toolsets', 'profile', 'cols'])
      const unsupported = Object.keys(params).filter(key => !allowed.has(key) && !(key === 'fast' && params[key] === false))

      if (unsupported.length) { throw new Error(`Canonical gateway does not support explicit session options: ${unsupported.join(', ')}`) }
      const result = Object.fromEntries(Object.entries(params).filter(([key]) => ['request_id', 'cwd', 'model', 'toolsets'].includes(key)))
      const key = JSON.stringify(result)
      const requestId = params.request_id ?? this.creates.get(key) ?? crypto.randomUUID()
      this.creates.set(key, String(requestId))

      return { ...result, request_id: requestId, source: 'gui' }
    }

    if (method === 'session.interrupt') {
      const generation = params.execution_generation ?? this.generations.get(String(params.session_id))

      if (typeof generation !== 'number') { throw new Error('Session execution identity unavailable; reconnect before Stop') }

      return { session_id: params.session_id, execution_generation: generation }
    }

    if (method === 'approval.respond' || method === 'clarify.respond') {
      const id = String(params.prompt_id ?? params.request_id ?? '')
      const prompt = this.prompts.get(id)
      const sessionId = params.session_id ?? prompt?.session_id

      if (!prompt || prompt.session_id !== sessionId || prompt.execution_generation !== this.generations.get(String(sessionId))) {
        throw new Error('Prompt is stale or unavailable; reconnect before responding')
      }

      const field = method === 'approval.respond' ? 'choice' : 'answer'

      return { session_id: sessionId, execution_generation: prompt.execution_generation, prompt_id: id, [field]: params[field] }
    }

    return params
  }

  event(event: { type: string; session_id?: string; payload?: unknown }) {
    const payload = event.payload as Record<string, unknown> | undefined

    if (!payload || !event.session_id) { return }
    const sid = event.session_id

    if (Array.isArray(payload.pending)) {
      payload.pending_submissions = payload.pending.map(row => ({ ...row, user: row.text }))
    }

    if (typeof payload.execution_generation === 'number') {
      const current = this.generations.get(sid) ?? -1

      if (payload.execution_generation < current) { return }
      this.generations.set(sid, payload.execution_generation)
    }

    if (typeof payload.prompt_id === 'string') {
      if (event.type.endsWith('.settled')) { this.prompts.delete(payload.prompt_id);

 return }

      if (event.type === 'approval.request' || event.type === 'clarify.request') {
        payload.request_id = payload.prompt_id
        this.prompts.set(payload.prompt_id, { ...payload, session_id: sid })
      }
    }
  }

  result(method: string, params: Record<string, unknown>, value: any): any {
    if (!value || typeof value !== 'object') { return value }

    if (typeof value.session_id === 'string' && typeof value.revision === 'number') {
      this.revisions.set(value.session_id, value.revision)
    }

    if (method === 'session.title' || method === 'session.archive') {
      if (value.session_id !== params.session_id) { throw new Error('Metadata receipt destination mismatch') }

      for (const [key, mutation] of this.mutations) { if (mutation.request_id === params.request_id) { this.mutations.delete(key) } }

      return { ...value, ok: true }
    }

    if (method === 'session.create') {
      for (const [key, id] of this.creates) { if (id === params.request_id) { this.creates.delete(key) } }
    }

    if (method === 'prompt.submit') {
      if (value.ref?.session_id !== params.session_id || typeof value.admission_id !== 'string') {
        throw new Error('Canonical admission receipt destination mismatch')
      }

      return { ...value, session_id: value.ref.session_id, submission_id: params.submission_id ?? params.input_id }
    }

    if (method === 'session.resume' || method === 'session.create') {
      const sid = value.session_id
      this.event({ type: 'session.info', session_id: sid, payload: value })

      const prompts = (value.prompts ?? []).map((prompt: Record<string, unknown>) => {
        const projected = { ...prompt, request_id: prompt.prompt_id }
        this.event({ type: `${prompt.kind}.request`, session_id: sid, payload: projected })

        return projected
      })

      return { ...value, pending_approval: prompts.find((p: any) => p.kind === 'approval'), pending_clarify: prompts.find((p: any) => p.kind === 'clarify'), info: { ...value.info, stored_session_id: value.stored_session_id, pending_submissions: value.pending_submissions, execution_generation: value.execution_generation, running: value.running } }
    }

    if (method === 'session.events.since') {
      for (const event of value.events ?? []) { this.event(event) }
    }

    return value
  }
}
