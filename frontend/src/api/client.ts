import type { HistoryPage, Snapshot, Topic } from './types'

export class APIError extends Error {
  constructor(public status: number, public code: string, message: string, public retryAfterMs = 0) {
    super(message)
  }
}

export function retryAfter(value: string | null): number {
  if (!value) return 0
  const seconds = Number(value)
  const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(value) - Date.now()
  return Number.isFinite(delay) ? Math.max(0, delay) : 0
}

async function request<T>(path: string, options: { token?: string; body?: unknown; method?: string; signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = {}
  if (options.token) headers['X-Debate-Token'] = options.token
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(`/api${path}`, { method: options.method ?? 'GET', headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal, cache: 'no-store' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new APIError(0, 'connection_error', 'Could not reach BotArena. Check your connection and try again.')
  }
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new APIError(response.status, payload?.error?.code ?? 'request_failed',
      payload?.error?.message ?? 'The request could not be completed.', retryAfter(response.headers.get('Retry-After')))
  }
  if (payload === null) throw new APIError(0, 'invalid_response', 'The server returned an unreadable response.')
  return payload as T
}

export const api = {
  topics: (signal?: AbortSignal) => request<{ topics: Topic[] }>('/topics', { signal }),
  create: (body: import('./types').TopicChoice & { client_request_id: string }, token: string) => request<Snapshot>('/debates', { method: 'POST', body, token }),
  snapshot: (id: string, token?: string, signal?: AbortSignal) => request<Snapshot>(`/debates/${encodeURIComponent(id)}`, { token, signal }),
  run: (id: string, token: string) => request<{ id: string; status: 'running' | 'judging' }>(`/debates/${encodeURIComponent(id)}/run`, { method: 'POST', token }),
  history: (cursor?: string, signal?: AbortSignal) => request<HistoryPage>(`/debates?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal }),
}

export function errorMessage(error: unknown): string {
  return error instanceof APIError ? error.message : 'Something went wrong. Please try again.'
}
