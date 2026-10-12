import { api } from '../api/client'

const PENDING_PREFIX = 'botarena:pending:'
const TOKEN_PREFIX = 'botarena:token:'
export const RECOVERY_WINDOW = 5 * 60 * 1000
export interface PendingCreation { client_request_id: string; topic_id: string; token: string; created_at: number }

function storage(): Storage {
  try { return window.localStorage } catch { throw new Error('Browser storage is unavailable. Enable it before starting a battle.') }
}

export function readToken(debateId: string): string | undefined {
  try { return storage().getItem(TOKEN_PREFIX + debateId) ?? undefined } catch { return undefined }
}

export function pendingCreations(): PendingCreation[] {
  const entries: PendingCreation[] = []
  const store = storage()
  for (let index = 0; index < store.length; index++) {
    const key = store.key(index)
    if (!key?.startsWith(PENDING_PREFIX)) continue
    try {
      const value = JSON.parse(store.getItem(key) ?? 'null') as PendingCreation | null
      if (value && typeof value.client_request_id === 'string' && typeof value.topic_id === 'string'
          && typeof value.token === 'string' && /^[a-f0-9]{64}$/i.test(value.token)
          && typeof value.created_at === 'number' && key === PENDING_PREFIX + value.client_request_id) entries.push(value)
    } catch { /* Leave malformed entries untouched; they may belong to a different app version. */ }
  }
  return entries.sort((left, right) => left.created_at - right.created_at)
}

export function prepareCreation(topicId: string): PendingCreation {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  const pending = { client_request_id: crypto.randomUUID(), topic_id: topicId,
    token: Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join(''), created_at: Date.now() }
  try { storage().setItem(PENDING_PREFIX + pending.client_request_id, JSON.stringify(pending)) }
  catch { throw new Error('Could not save your execution token. Free browser storage before starting a battle.') }
  return pending
}

export function discardCreation(id: string): void { storage().removeItem(PENDING_PREFIX + id) }

const recoveries = new Map<string, Promise<Awaited<ReturnType<typeof api.create>>>>()

export function recoverCreation(pending: PendingCreation) {
  const existing = recoveries.get(pending.client_request_id)
  if (existing) return existing
  const recovery = (async () => {
    const snapshot = await api.create({ topic_id: pending.topic_id, client_request_id: pending.client_request_id }, pending.token)
    try {
      storage().setItem(TOKEN_PREFIX + snapshot.id, pending.token)
      discardCreation(pending.client_request_id)
    } catch { throw new Error('Battle saved, but browser storage could not save its token. Keep this pending recovery and try again.') }
    return snapshot
  })()
  recoveries.set(pending.client_request_id, recovery)
  void recovery.finally(() => recoveries.delete(pending.client_request_id)).catch(() => {})
  return recovery
}
