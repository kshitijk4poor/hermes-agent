import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { $busyInputConfig } from '@/store/busy-input-mode'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'

import { useBusyInputMode } from './use-busy-input-mode'

vi.mock('@/store/session-request-router', () => ({
  requestForSessionProfile: (
    _owner: unknown,
    request: (method: string, params: unknown) => Promise<unknown>,
    method: string,
    params: unknown
  ) => request(method, params)
}))

afterEach(() => {
  cleanup()
  $busyInputConfig.set(null)
  $activeGatewayProfile.set('default')
  $gatewayState.set('closed')
})

it('uses only the current owner config and ignores a late response from the previous profile', async () => {
  $gatewayState.set('open')
  let resolveOld!: (value: { value: string }) => void

  const request = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveOld = resolve
        })
    )
    .mockResolvedValue({ value: 'steer' })

  const hook = renderHook(() =>
    useBusyInputMode({ sessionId: 'runtime', storedSessionId: null, requestGateway: request })
  )

  expect(hook.result.current).toBeNull()
  act(() => $activeGatewayProfile.set('other'))
  await waitFor(() => expect(hook.result.current).toBe('steer'))
  await act(async () => resolveOld({ value: 'queue' }))
  expect(hook.result.current).toBe('steer')
  act(() =>
    $busyInputConfig.set({
      owner: JSON.stringify([$connection.get()?.connectionId ?? '', 'other']),
      connection: $connection.get(),
      mode: 'queue'
    })
  )
  expect(hook.result.current).toBe('queue')
  act(() => $busyInputConfig.set({ owner: 'foreign-owner', connection: $connection.get(), mode: 'interrupt' }))
  expect(hook.result.current).toBe('steer')
})
