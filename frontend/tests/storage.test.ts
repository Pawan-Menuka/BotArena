import { describe, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { pendingCreations, prepareCreation, readToken, recoverCreation } from '../src/storage/pendingCreation'
import { snapshot } from './fixtures'

describe('pending creation', () => {
  it('writes separate 32-byte tokens before any request and promotes before clearing', async () => {
    const first = prepareCreation('first')
    const second = prepareCreation('second')
    expect(first.token).toMatch(/^[0-9a-f]{64}$/)
    expect(first.client_request_id).not.toBe(second.client_request_id)
    expect(pendingCreations()).toHaveLength(2)
    vi.spyOn(api, 'create').mockImplementation(async (body, token) => {
      expect(pendingCreations().some(entry => entry.client_request_id === body.client_request_id && entry.token === token)).toBe(true)
      return snapshot()
    })
    const run = vi.spyOn(api, 'run')
    await recoverCreation(first)
    expect(readToken('debate-1')).toBe(first.token)
    expect(pendingCreations()).toEqual([second])
    expect(run).not.toHaveBeenCalled()
  })

  it('keeps the pending entry when the create response is uncertain', async () => {
    const pending = prepareCreation('topic')
    vi.spyOn(api, 'create').mockRejectedValue(new Error('offline'))
    await expect(recoverCreation(pending)).rejects.toThrow()
    expect(pendingCreations()).toEqual([pending])
  })

  it('deduplicates simultaneous recovery requests', async () => {
    const pending = prepareCreation('topic')
    vi.spyOn(api, 'create').mockResolvedValue(snapshot())
    await Promise.all([recoverCreation(pending), recoverCreation(pending)])
    expect(api.create).toHaveBeenCalledTimes(1)
  })

  it('keeps the only token copy if promotion fails', async () => {
    const pending = prepareCreation('topic')
    vi.spyOn(api, 'create').mockResolvedValue(snapshot())
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    await expect(recoverCreation(pending)).rejects.toThrow('browser storage')
    expect(pendingCreations()).toEqual([pending])
  })

  it('replays exactly the saved custom topic after an uncertain response without starting work', async () => {
    const entry = prepareCreation({ custom_topic: 'Public transport should be free.' })
    const create = vi.spyOn(api, 'create').mockRejectedValueOnce(new Error('offline')).mockResolvedValue(snapshot())
    const run = vi.spyOn(api, 'run')
    await expect(recoverCreation(entry)).rejects.toThrow('offline')
    const saved = pendingCreations()[0]
    expect(saved.custom_topic).toBe('Public transport should be free.')
    await recoverCreation(saved)
    expect(create).toHaveBeenLastCalledWith({ custom_topic: saved.custom_topic, client_request_id: saved.client_request_id }, saved.token)
    expect(run).not.toHaveBeenCalled()
    expect(pendingCreations()).toEqual([])
    expect(readToken('debate-1')).toBe(saved.token)
  })
})
