import { useCallback, useEffect, useRef, useState } from 'react'
import { api, APIError, errorMessage } from '../api/client'
import type { Snapshot } from '../api/types'
import { readToken } from '../storage/pendingCreation'

export function normalize(snapshot: Snapshot): Snapshot {
  return { ...snapshot, messages: [...new Map(snapshot.messages.map(message => [message.id, message])).values()]
    .sort((left, right) => left.turn_order - right.turn_order) }
}

export function usePolling(id: string) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)
  const [epoch, setEpoch] = useState(0)
  const latest = useRef<Snapshot | null>(null)
  const refresh = useCallback(() => setEpoch(value => value + 1), [])

  useEffect(() => {
    let disposed = false
    let inFlight = false
    let immediate = false
    let failures = 0
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    setMissing(false)

    async function fetchSnapshot() {
      if (disposed || stopped || document.hidden) return
      if (inFlight) { immediate = true; return }
      inFlight = true
      let delay = 1500
      try {
        const incoming = normalize(await api.snapshot(id, readToken(id), controller.signal))
        if (disposed) return
        if (!latest.current || incoming.revision >= latest.current.revision) {
          latest.current = incoming
          setSnapshot(incoming)
        }
        failures = 0
        setError(null)
        stopped = latest.current?.status === 'completed' || latest.current?.status === 'failed'
      } catch (failure) {
        if (disposed) return
        if (failure instanceof APIError && failure.status === 404) {
          stopped = true
          setMissing(true)
          setSnapshot(null)
        } else {
          setError(errorMessage(failure))
          failures++
          delay = Math.max(Math.min(1500 * 2 ** failures, 15000), failure instanceof APIError ? failure.retryAfterMs : 0)
        }
      } finally {
        inFlight = false
        if (!disposed && !stopped && !document.hidden) {
          timer = setTimeout(() => { void fetchSnapshot() }, immediate ? 0 : delay)
          immediate = false
        }
      }
    }

    function visible() {
      clearTimeout(timer)
      if (!document.hidden && !stopped) void fetchSnapshot()
    }
    document.addEventListener('visibilitychange', visible)
    void fetchSnapshot()
    return () => { disposed = true; controller.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visible) }
  }, [id, epoch])
  return { snapshot, error, missing, refresh }
}
