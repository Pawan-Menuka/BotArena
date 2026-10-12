import { act, renderHook } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { api, APIError } from '../src/api/client'
import { usePolling } from '../src/hooks/usePolling'
import { message, snapshot } from './fixtures'

it('polls without executing work, rejects old revisions, sorts and deduplicates messages, and stops at completion', async () => {
  vi.useFakeTimers()
  vi.spyOn(api, 'snapshot').mockResolvedValueOnce(snapshot({ status: 'running', revision: 5, messages: [{ ...message, id: 'm2', turn_order: 2 }, message, message] }))
    .mockResolvedValueOnce(snapshot({ status: 'running', revision: 4 }))
    .mockResolvedValue(snapshot({ status: 'completed', revision: 6 }))
  const run = vi.spyOn(api, 'run')
  const { result } = renderHook(() => usePolling('debate-1'))
  await act(async () => {})
  expect(result.current.snapshot?.messages.map(item => item.id)).toEqual(['m1', 'm2'])
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(result.current.snapshot?.revision).toBe(5)
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(result.current.snapshot?.status).toBe('completed')
  await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
  expect(api.snapshot).toHaveBeenCalledTimes(3)
  expect(run).not.toHaveBeenCalled()
})

it('allows only one in-flight request and pauses while hidden', async () => {
  vi.useFakeTimers()
  let resolve!: (value: ReturnType<typeof snapshot>) => void
  vi.spyOn(api, 'snapshot').mockImplementationOnce(() => new Promise(done => { resolve = done })).mockResolvedValue(snapshot({ status: 'running' }))
  Object.defineProperty(document, 'hidden', { value: false, configurable: true })
  renderHook(() => usePolling('debate-1'))
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(api.snapshot).toHaveBeenCalledTimes(1)
  Object.defineProperty(document, 'hidden', { value: true, configurable: true })
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  await act(async () => { resolve(snapshot({ status: 'running' })) })
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(api.snapshot).toHaveBeenCalledTimes(1)
  Object.defineProperty(document, 'hidden', { value: false, configurable: true })
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
  expect(api.snapshot).toHaveBeenCalledTimes(2)
})

it('honors Retry-After and stops permanently on 404', async () => {
  vi.useFakeTimers()
  vi.spyOn(api, 'snapshot').mockRejectedValueOnce(new APIError(503, 'busy', 'Try later', 9000)).mockRejectedValue(new APIError(404, 'not_found', 'Missing'))
  const { result } = renderHook(() => usePolling('debate-1'))
  await act(async () => {})
  await act(async () => { await vi.advanceTimersByTimeAsync(8999) })
  expect(api.snapshot).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(result.current.missing).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(api.snapshot).toHaveBeenCalledTimes(2)
})

it('backs off failed reads to fifteen seconds without running work', async () => {
  vi.useFakeTimers()
  vi.spyOn(api, 'snapshot').mockRejectedValue(new APIError(0, 'connection_error', 'Offline'))
  const run = vi.spyOn(api, 'run')
  renderHook(() => usePolling('debate-1'))
  await act(async () => {})
  for (const delay of [3000, 6000, 12000, 15000, 15000]) {
    const calls = vi.mocked(api.snapshot).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(delay - 1) })
    expect(api.snapshot).toHaveBeenCalledTimes(calls)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(api.snapshot).toHaveBeenCalledTimes(calls + 1)
  }
  expect(run).not.toHaveBeenCalled()
})
