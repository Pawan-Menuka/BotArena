import { expect, it, vi } from 'vitest'
import { api, APIError, retryAfter } from '../src/api/client'

it('uses header-only execution tokens and disables caching', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'running', id: 'id' })))
  vi.stubGlobal('fetch', fetch)
  const token = 'a'.repeat(64)
  await api.run('id', token)
  const [url, options] = fetch.mock.calls[0]
  expect(url).toBe('/api/debates/id/run')
  expect(url).not.toContain(token)
  expect(options.headers['X-Debate-Token']).toBe(token)
  expect(options.body).toBeUndefined()
  expect(options.cache).toBe('no-store')
})

it('preserves the server retry delay on a failed response', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'capacity_unavailable', message: 'Try later' } }), { status: 503, headers: { 'Retry-After': '9' } })))
  await expect(api.run('id', 'a'.repeat(64))).rejects.toMatchObject({ status: 503, retryAfterMs: 9000 })
})

it('replaces raw network errors with a safe message', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('raw diagnostics')))
  await expect(api.topics()).rejects.toBeInstanceOf(APIError)
  await expect(api.topics()).rejects.toMatchObject({ code: 'connection_error' })
})

it('parses seconds and HTTP-date Retry-After values', () => {
  expect(retryAfter('3')).toBe(3000)
  expect(retryAfter('bad')).toBe(0)
  expect(retryAfter(new Date(Date.now() + 10000).toUTCString())).toBeGreaterThan(8500)
})
