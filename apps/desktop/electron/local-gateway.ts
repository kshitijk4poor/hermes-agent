import { spawn } from 'node:child_process'
import crypto from 'node:crypto'
import { hiddenWindowsChildOptions } from './windows-child-options'

export function runGatewayEnsure(backend, cwd: string, home: string): Promise<{ code: number; stdout: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(backend.command, backend.args, hiddenWindowsChildOptions({ cwd, env: { ...process.env, HERMES_HOME: home, ...backend.env }, shell: backend.shell, stdio: ['ignore', 'pipe', 'pipe'] }))
    let stdout = ''
    // Only the bounded ensure client is ours. Never retain/kill its detached owner.
    const timer = setTimeout(() => child.kill(), 40_000)
    child.stdout.on('data', data => { stdout += data.toString(); if (stdout.length > 65536) child.kill() })
    child.stderr.resume()
    child.on('error', () => { clearTimeout(timer); reject(new Error('Could not run hermes gateway ensure')) })
    child.on('close', code => { clearTimeout(timer); resolve({ code: code ?? 7, stdout }) })
  })
}
import fs from 'node:fs/promises'
import net from 'node:net'
import path from 'node:path'

export interface GatewayEndpoint {
  profile_id: string
  instance_id: string
  authority_epoch: number
  runtime_protocol: number
  api_origin: string
  capabilities: string[]
  supervisor: string
}

export async function ensureLocalGateway(run: () => Promise<{ code: number; stdout: string }>) {
  const result = await run()
  const payload = JSON.parse(result.stdout)
  if (result.code !== 0 || payload.state !== 'ready') {
    throw new Error(`Gateway ${payload.state || 'inaccessible'} (${payload.reason_code || 'ensure_failed'}). Use hermes gateway status for recovery.`)
  }
  const endpoint: GatewayEndpoint = payload.endpoint
  const origin = new URL(endpoint.api_origin)
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', '[::1]'].includes(origin.hostname)
      || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash
      || !origin.port || endpoint.runtime_protocol !== 1 || !endpoint.instance_id || !endpoint.profile_id
      || !endpoint.capabilities.includes('session-authority-v1')) {
    throw new Error('Invalid local gateway endpoint')
  }
  return { baseUrl: endpoint.api_origin, wsUrl: `${endpoint.api_origin.replace(/^http/, 'ws')}/api/ws?native_dial=unminted`, mode: 'local', source: 'local', authMode: 'native', token: '', gatewayEndpoint: endpoint }
}

// Credentials never enter public descriptors, logs, renderer URLs or persisted state.
export function createLocalGatewayDials() {
  const pending = new Map<string, { ticket: string; webContentsId: number; expires: number }>()
  return {
    prepare(origin: string, ticket: string, webContentsId: number) {
      for (const [key, value] of pending) if (value.expires <= Date.now()) pending.delete(key)
      if (pending.size >= 100) throw new Error('Too many pending gateway dials')
      const url = `${origin.replace(/^http/, 'ws')}/api/ws?native_dial=${crypto.randomUUID()}`
      pending.set(url, { ticket, webContentsId, expires: Date.now() + 25_000 })
      return url
    },
    headers(details: { url: string; webContentsId?: number; resourceType: string; requestHeaders?: Record<string, string> }) {
      const grant = pending.get(details.url)
      if (!grant || grant.webContentsId !== details.webContentsId || details.resourceType !== 'webSocket') return null
      pending.delete(details.url)
      if (grant.expires <= Date.now()) return null
      const headers = { ...details.requestHeaders }
      for (const key of Object.keys(headers)) if (['origin', 'sec-websocket-protocol'].includes(key.toLowerCase())) delete headers[key]
      headers['Sec-WebSocket-Protocol'] = `hermes-gateway-v1, hermes-gateway-ticket.${grant.ticket}`
      return headers
    }
  }
}

async function privateNode(file: string, kind: 'directory' | 'socket' | 'file') {
  const node = await fs.lstat(file)
  const valid = { directory: node.isDirectory(), socket: node.isSocket(), file: node.isFile() }[kind]
  if (!valid || node.uid !== process.getuid?.() || (node.mode & 0o077)) throw new Error('Unsafe gateway control path')
}

export async function mintLocalGatewayTicket(endpoint: GatewayEndpoint): Promise<string> {
  // Named-pipe bootstrap needs the same server-identity validation as Python's
  // native bootstrap. Refuse rather than silently use an unauthenticated pipe.
  if (process.platform === 'win32') throw new Error('Native gateway bootstrap on Windows requires the validated named-pipe client')
  const home = endpoint.profile_id
  if (await fs.realpath(home) !== home) throw new Error('Noncanonical gateway profile')
  await privateNode(home, 'directory')
  let socketPath = path.join(home, 'gateway.sock')
  try { await privateNode(socketPath, 'socket') } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    const pointer = path.join(home, 'gateway.sock.path')
    await privateNode(pointer, 'file')
    const target = (await fs.readFile(pointer, 'utf8')).trim()
    const hash = crypto.createHash('sha256').update(home).digest('hex').slice(0, 16)
    if (!path.isAbsolute(target) || path.basename(path.dirname(target)) !== `hermes-gw-${hash}` || path.basename(target) !== 'control.sock') throw new Error('Invalid gateway control pointer')
    await privateNode(path.dirname(target), 'directory')
    await privateNode(target, 'socket')
    socketPath = target
  }
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath)
    let buffer = ''
    const deadline = setTimeout(() => socket.destroy(new Error('Gateway ticket deadline')), 5000)
    socket.on('error', () => reject(new Error('Gateway ticket bootstrap failed')))
    socket.on('close', () => { clearTimeout(deadline); reject(new Error('Gateway ticket connection closed')) })
    socket.on('connect', () => socket.write(JSON.stringify({ protocol: 1, id: 1, verb: 'session-ticket', params: { profile_id: home, instance_id: endpoint.instance_id, purpose: 'interactive' } }) + '\n'))
    socket.on('data', chunk => {
      buffer += chunk.toString()
      if (buffer.length > 65536) return socket.destroy(new Error('Oversized gateway ticket response'))
      if (!buffer.includes('\n')) return
      try {
        const reply = JSON.parse(buffer.split('\n')[0])
        const grant = reply.result
        if (!reply.ok || reply.protocol !== 1 || reply.id !== 1 || grant?.instance_id !== endpoint.instance_id || grant?.profile_id !== home || typeof grant?.ticket !== 'string') throw new Error('Invalid gateway ticket')
        resolve(grant.ticket)
      } catch { reject(new Error('Invalid gateway ticket response')) }
      socket.destroy()
    })
  })
}
