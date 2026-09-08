import { expect, test } from 'vitest'

import { createLocalGatewayDials, ensureLocalGateway } from './local-gateway'

test('native HTTP mints fresh purpose-bound grants without browser credentials', async () => {
  const fs = await import('node:fs/promises')
  const os = await import('node:os')
  const path = await import('node:path')
  const net = await import('node:net')
  const { nativeGatewayHttpHeaders } = await import('./local-gateway')
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'desktop-http-'))
  const endpoint = { profile_id: home, instance_id: 'owner', authority_epoch: 1, runtime_protocol: 1, api_origin: 'http://127.0.0.1:1234', capabilities: ['session-authority-v1'], supervisor: 'none' }
  const requests: any[] = []

  const server = net.createServer(socket => socket.once('data', chunk => {
    const request = JSON.parse(chunk.toString())
    requests.push(request)
    socket.end(JSON.stringify({ protocol: 1, id: 1, ok: true, result: { profile_id: home, instance_id: 'owner', ticket: `grant-${requests.length}` } }) + '\n')
  }))

  await new Promise<void>(resolve => server.listen(path.join(home, 'gateway.sock'), resolve))
  await fs.chmod(path.join(home, 'gateway.sock'), 0o600)

  try {
    const descriptor = { gatewayEndpoint: endpoint, baseUrl: endpoint.api_origin }
    const a = await nativeGatewayHttpHeaders(descriptor, endpoint.api_origin + '/api/config')
    const b = await nativeGatewayHttpHeaders(descriptor, endpoint.api_origin + '/api/config')
    expect(a).toEqual({ 'X-Hermes-Gateway-Ticket': 'grant-1' })
    expect(b).toEqual({ 'X-Hermes-Gateway-Ticket': 'grant-2' })
    expect(requests.map(r => r.params)).toEqual([1, 2].map(() => ({ profile_id: home, instance_id: 'owner', purpose: 'native-http' })))
    await expect(nativeGatewayHttpHeaders(descriptor, 'http://127.0.0.1:4567/api/config')).rejects.toThrow('origin')
    expect(requests).toHaveLength(2)
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()))
    await fs.rm(home, { recursive: true, force: true })
  }
})

test('ensure consumes structured readiness without acquiring a child owner', async () => {
  const endpoint = { profile_id: '/private/profile', instance_id: 'owner', authority_epoch: 1, runtime_protocol: 1, api_origin: 'http://127.0.0.1:1234', capabilities: ['session-authority-v1'], supervisor: 'none' }
  const connection = await ensureLocalGateway(async () => ({ code: 0, stdout: JSON.stringify({ state: 'ready', endpoint }) }))
  expect(connection.baseUrl).toBe(endpoint.api_origin)
  expect(connection.gatewayEndpoint).toEqual(endpoint)
  expect(connection).not.toHaveProperty('process')
  expect(connection.token).toBe('')
  await expect(ensureLocalGateway(async () => ({ code: 5, stdout: JSON.stringify({ state: 'starting', reason_code: 'deadline' }) }))).rejects.toThrow('starting')
})

test('private dial credential is one-use and bound to the requesting native window', () => {
  const dials = createLocalGatewayDials()
  const dial = new URL(dials.prepare('http://127.0.0.1:1234', 'private-ticket', 7))
  expect(dial.searchParams.get('ticket')).toBe('private-ticket')
  dial.searchParams.delete('ticket')
  const url = dial.toString()
  expect(url).not.toContain('private-ticket')
  const details = { url, webContentsId: 8, resourceType: 'webSocket', requestHeaders: { Origin: 'http://renderer', 'Sec-WebSocket-Protocol': 'hermes-gateway-v1, hermes-gateway-ticket.private-ticket' } }
  expect(dials.headers(details)).toBeNull()
  const headers = dials.headers({ ...details, webContentsId: 7 })!
  expect(headers).not.toHaveProperty('Origin')
  expect(headers['Sec-WebSocket-Protocol']).toBe('hermes-gateway-v1, hermes-gateway-ticket.private-ticket')
  expect(dials.headers({ ...details, webContentsId: 7 })).toBeNull()
})
