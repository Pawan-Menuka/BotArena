import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, errorMessage } from '../api/client'
import type { HistoryPage } from '../api/types'

export function RecentBattles() {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined])
  const [page, setPage] = useState<HistoryPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const cursor = cursors.at(-1)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    void api.history(cursor, controller.signal).then(setPage)
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [cursor, reload])
  return <><header className="page-heading"><span className="eyebrow">The shared archive</span><h1>Recent battles</h1><p>Everyone’s debates, newest first. This is a public archive, not a personal history.</p></header>
    {error && <div className="notice" role="alert"><p>{error}</p><button className="button" onClick={() => setReload(value => value + 1)}>Try again</button></div>}
    {loading ? <p role="status">Loading recent battles…</p> : page?.items.length === 0 ? <section className="panel empty"><h2>The first word is yours.</h2><p>No battles have been created yet.</p><Link className="button primary" to="/">Choose a topic</Link></section> : !error &&
      <div className="history-list">{page?.items.map(item => <Link className="panel history-row" to={`/debates/${item.id}`} key={item.id}><div><span className="small muted">{new Date(item.created_at).toLocaleString()}</span><h2>{item.topic.title}</h2></div><span className="status-tag">{item.status}</span><span aria-hidden="true">↗</span></Link>)}</div>}
    <nav aria-label="History pagination" className="pagination"><button className="button" disabled={loading || cursors.length === 1} onClick={() => setCursors(values => values.slice(0, -1))}>← Newer</button><span className="muted">Page {cursors.length}</span><button className="button" disabled={loading || !page?.next_cursor} onClick={() => { if (page?.next_cursor) setCursors(values => [...values, page.next_cursor!]) }}>Older →</button></nav>
  </>
}
