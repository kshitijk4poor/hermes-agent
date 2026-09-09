/**
 * Wiring coverage for the main.ts gateway download transports. These functions
 * pull in main-process singletons (electronNet, the OAuth session, the save
 * dialog), so we assert on their source shape — the same approach as
 * oauth-session-request.test.ts — while gateway-file-download.test.ts unit-tests
 * the extracted streaming/decoding logic behaviorally. The token transport
 * lives in gateway-download-transport.ts and is covered behaviorally by
 * gateway-download-transport.test.ts.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'

import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { downloadAgentFor } from './api-transport'
import { pathForRegistryBackendRequest } from './connection-config'
import type {
  GatewayFileSaveContext,
  GatewayFileSaveDeps,
  GatewayFileSaveResult,
  GatewaySaveDialogResult
} from './gateway-file-download'
import {
  finalizeGatewayDownload,
  fsPumpDeps,
  gatewayFileRequestPaths,
  saveGatewayDownload
} from './gateway-file-download'
import type {
  GatewayDownloadOptions,
  GatewayOauthDownloadDeps,
  GatewayOauthDownloadRequest,
  GatewayOauthRequestOptions
} from './gateway-file-download-transport'
import { downloadViaOauthSessionToFile, downloadViaTokenToFile } from './gateway-file-download-transport'

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = (): void => {
    throw new Error('Promise not initialized')
  }

  const promise: Promise<T> = new Promise((done): void => {
    resolve = done
  })

  return { promise, resolve }
}

const context: GatewayFileSaveContext = { suggested: 'suggested.bin', fallbackName: 'fallback.bin' }
let directory: string
const servers: http.Server[] = []

beforeEach(async (): Promise<void> => {
  directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'gateway-transport-'))
})
afterEach(async (): Promise<void> => {
  vi.useRealTimers()

  for (const server of servers.splice(0)) {
    server.closeAllConnections()
    await new Promise<void>((resolve, reject): void => {
      server.close((error?: Error): void => {
        if (error) {
          reject(error)
        } else {
          resolve()
        }
      })
    })
  }

  await fs.promises.rm(directory, { recursive: true, force: true })
})

async function serve(handler: (request: http.IncomingMessage, response: http.ServerResponse) => void): Promise<string> {
  const server: http.Server = http.createServer(handler)
  servers.push(server)
  await new Promise<void>((resolve): void => {
    server.listen(0, '127.0.0.1', resolve)
  })
  // SAFETY: listen(0, '127.0.0.1') completed above, so this is a bound TCP address, not a pipe or closed server.
  const address: AddressInfo = server.address() as AddressInfo

  return `http://127.0.0.1:${address.port}`
}

function saveDialog(filePath: string): GatewayFileSaveDeps {
  return { showSaveDialog: async (): Promise<GatewaySaveDialogResult> => ({ canceled: false, filePath }) }
}

interface AuthCase {
  name: string
  options: GatewayDownloadOptions
  header: string
  value: string
}

const authCases: AuthCase[] = [
  { name: 'token', options: {}, header: 'x-hermes-session-token', value: 'session-token' },
  { name: 'bearer', options: { bearer: 'native-token' }, header: 'authorization', value: 'Bearer native-token' }
]

test.each(authCases)(
  '$name transport streams before EOF, only after the dialog, and drops the connection timeout',
  async ({ options, header, value }: AuthCase): Promise<void> => {
    const responseReady: Deferred<http.ServerResponse> = deferred<http.ServerResponse>()

    const baseUrl: string = await serve((request: http.IncomingMessage, response: http.ServerResponse): void => {
      expect(request.headers[header]).toBe(value)
      expect(request.headers[header === 'authorization' ? 'x-hermes-session-token' : 'authorization']).toBeUndefined()
      response.writeHead(200, { 'Content-Disposition': 'attachment; filename="server.bin"' })
      response.flushHeaders()
      responseReady.resolve(response)
    })

    const dialogEntered: Deferred<void> = deferred<void>()
    const decision: Deferred<GatewaySaveDialogResult> = deferred<GatewaySaveDialogResult>()
    const destination: string = path.join(directory, 'existing.bin')
    await fs.promises.writeFile(destination, 'original')

    const pending: Promise<GatewayFileSaveResult> = downloadViaTokenToFile(
      `${baseUrl}/download`,
      'session-token',
      context,
      {
        showSaveDialog: async (settings: { defaultPath: string; filters?: unknown; title: string }): Promise<GatewaySaveDialogResult> => {
          // #92480: the dialog must carry the download's file type so Windows has
          // a default extension to append; the resolved name reaches it intact.
          expect(settings.defaultPath).toBe('server.bin')
          expect(settings.title).toBe('Save File')
          expect(settings.filters).toEqual([{ name: 'BIN File', extensions: ['bin'] }, { name: 'All Files', extensions: ['*'] }])
          dialogEntered.resolve()

          return decision.promise
        }
      },
      { ...options, timeoutMs: 2000 }
    )

    const response: http.ServerResponse = await responseReady.promise
    await dialogEntered.promise
    expect(
      Object.values(downloadAgentFor('http:').sockets)
        .flat()
        .some((socket): boolean => socket?.timeout === 0)
    ).toBe(true)
    response.write('first chunk')
    expect(await fs.promises.readdir(directory)).toEqual(['existing.bin'])
    decision.resolve({ canceled: false, filePath: destination })
    await vi.waitFor(async (): Promise<void> => {
      const part: string | undefined = (await fs.promises.readdir(directory)).find((name: string): boolean =>
        name.endsWith('.part')
      )

      expect(part).toBeDefined()
      expect(await fs.promises.readFile(path.join(directory, part!), 'utf8')).toBe('first chunk')
    })
    expect(await fs.promises.readFile(destination, 'utf8')).toBe('original')
    response.end(' last chunk')
    expect(await pending).toEqual({ saved: true, path: destination })
    expect(await fs.promises.readFile(destination, 'utf8')).toBe('first chunk last chunk')
    expect(await fs.promises.readdir(directory)).toEqual(['existing.bin'])
  }
)

interface FixtureSession {
  partition: string
}

class CookieRequest extends EventEmitter implements GatewayOauthDownloadRequest {
  aborted: boolean = false
  ended: boolean = false
  abort(): void {
    this.aborted = true
  }
  end(): void {
    this.ended = true
  }
}

test('oauth transport streams to disk instead of buffering the whole body', () => {
  const fn = extract('function downloadViaOauthSessionToFile', '\nasync function finalizeGatewayDownload')

test('dialog-time and mid-stream failures abort without clobbering an existing destination', async (): Promise<void> => {
  const destination: string = path.join(directory, 'existing.bin')
  await fs.promises.writeFile(destination, 'original')

  for (const duringDialog of [true, false]) {
    const response = Object.assign(new PassThrough(), { statusCode: 200, headers: {} })
    const decision: Deferred<GatewaySaveDialogResult> = deferred<GatewaySaveDialogResult>()
    let aborted: boolean = false

    const pending: Promise<GatewayFileSaveResult> = finalizeGatewayDownload(
      response,
      context,
      (): void => {
        aborted = true
      },
      { showSaveDialog: (): Promise<GatewaySaveDialogResult> => decision.promise }
    )

    const rejected: Promise<void> = expect(pending).rejects.toThrow('socket failed')

    if (!duringDialog) {
      decision.resolve({ canceled: false, filePath: destination })
      await vi.waitFor((): void => {
        expect(response.listenerCount('data')).toBe(1)
      })
      response.write('partial')
    }

    response.destroy(new Error('socket failed'))
    await new Promise<void>((resolve): void => {
      response.once('close', resolve)
    })
    decision.resolve({ canceled: false, filePath: destination })
    await rejected
    expect(aborted).toBe(true)
    expect(await fs.promises.readFile(destination, 'utf8')).toBe('original')
    expect(await fs.promises.readdir(directory)).toEqual(['existing.bin'])
  }
})

test('data-URL fallback keeps a pre-existing temp collision and destination intact', async (): Promise<void> => {
  const destination: string = path.join(directory, 'existing.bin')
  const temp: string = path.join(directory, 'collision.part')
  await fs.promises.writeFile(destination, 'original')
  await fs.promises.writeFile(temp, 'other download')
  await expect(
    saveGatewayDownload({ download: '/download', dataUrl: '/data-url' }, context, {
      ...saveDialog(destination),
      pump: { ...fsPumpDeps(), tempPathFor: (): string => temp },
      download: async (): Promise<GatewayFileSaveResult> => {
        throw Object.assign(new Error('not found'), { statusCode: 404 })
      },
      readDataUrl: async (): Promise<string> => 'data:,replacement'
    })
  ).rejects.toMatchObject({ code: 'EEXIST' })
  expect(await fs.promises.readFile(destination, 'utf8')).toBe('original')
  expect(await fs.promises.readFile(temp, 'utf8')).toBe('other download')
})

// #92480: both gateway save dialogs opened with no `filters`, so the Windows
// dialog offered only "All Files" and had no default extension to append. The
// streaming path is asserted above inside the token-download test; this covers
// the data-url fallback, which any gateway old enough to 404 the streaming
// route falls back into. The helper's own behavior (whitelist, All Files last)
// is covered in gateway-file-download.test.ts.
test('the data-url save dialog carries a file type too', async (): Promise<void> => {
  const suggested = 'suggested.bin'
  const seen: { filters?: unknown }[] = []
  const destination: string = path.join(directory, 'saved.bin')

  const result: GatewayFileSaveResult = await saveGatewayDownload(
    { dataUrl: '/api/fs/read-data-url?path=/x', download: '/download' },
    { fallbackName: 'fallback.bin', suggested },
    {
      download: async (): Promise<GatewayFileSaveResult> => {
        throw Object.assign(new Error('not found'), { statusCode: 404 })
      },
      readDataUrl: async (): Promise<string> => 'data:application/octet-stream,hello',
      showSaveDialog: async (settings: { defaultPath: string; filters?: unknown }): Promise<GatewaySaveDialogResult> => {
        seen.push(settings)

        return { canceled: false, filePath: destination }
      }
    }
  )

  expect(result.saved).toBe(true)
  expect(seen).toEqual([
    {
      defaultPath: suggested,
      filters: [{ name: 'BIN File', extensions: ['bin'] }, { name: 'All Files', extensions: ['*'] }],
      title: 'Save File'
    }
  ])
  await expect(fs.promises.readFile(destination, 'utf8')).resolves.toBe('hello')
})
