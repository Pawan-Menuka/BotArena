import { useCallback, useEffect, useState } from 'react'
import { errorMessage } from '../api/client'
import type { Snapshot } from '../api/types'
import { discardCreation, pendingCreations, recoverCreation, RECOVERY_WINDOW, type PendingCreation } from '../storage/pendingCreation'

export function usePendingCreation() {
  const [pending, setPending] = useState<PendingCreation[]>([])
  const [recovered, setRecovered] = useState<Snapshot[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string[]>([])

  const recover = useCallback(async (entry: PendingCreation) => {
    setBusy(ids => [...ids, entry.client_request_id])
    try {
      const snapshot = await recoverCreation(entry)
      setRecovered(items => [...items.filter(item => item.id !== snapshot.id), snapshot])
      setPending(pendingCreations())
      setError(null)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusy(ids => ids.filter(id => id !== entry.client_request_id)) }
  }, [])

  useEffect(() => {
    function reconcile() {
      try {
        const entries = pendingCreations()
        setPending(entries)
        for (const entry of entries) {
          if (Date.now() - entry.created_at >= 0 && Date.now() - entry.created_at < RECOVERY_WINDOW) void recover(entry)
        }
      } catch { setError('Browser storage is unavailable. You can read debates, but starting one requires local storage.') }
    }
    reconcile()
    window.addEventListener('storage', reconcile)
    return () => window.removeEventListener('storage', reconcile)
  }, [recover])

  function discard(id: string) {
    try { discardCreation(id); setPending(pendingCreations()) }
    catch { setError('Could not update browser storage. Try again.') }
  }
  function reloadPending() {
    try { setPending(pendingCreations()) } catch { setError('Could not read browser storage.') }
  }
  return { pending, recovered, error, busy, recover, discard, reloadPending }
}
