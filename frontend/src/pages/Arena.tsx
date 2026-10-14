import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, errorMessage } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import { readToken } from '../storage/pendingCreation'
import { SpeakerStatus } from '../components/SpeakerStatus'
import { TranscriptView } from '../components/TranscriptView'
import { FailureView } from './FailureView'
import { Results } from './Results'

export function Arena() {
  const { id = '' } = useParams()
  return <ArenaBattle key={id} id={id} />
}

function ArenaBattle({ id }: { id: string }) {
  const { snapshot: debate, missing, error, refresh } = usePolling(id)
  const [runError, setRunError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submitting = useRef(false)

  async function run() {
    const token = readToken(id)
    if (!token || submitting.current) return
    submitting.current = true
    setBusy(true)
    setRunError(null)
    try { await api.run(id, token); refresh() }
    catch (failure) { setRunError(errorMessage(failure)); refresh() }
    finally { submitting.current = false; setBusy(false) }
  }

  if (missing) return <section className="panel empty"><h1>Battle not found.</h1><p>This link may be unavailable.</p><Link className="button primary" to="/">Back to the lobby</Link></section>
  if (!debate) return <section className="panel empty"><h1>Opening the arena…</h1>{error ? <div role="alert"><p>{error}</p><button className="button" onClick={refresh}>Try again</button></div> : <p role="status">Fetching the saved debate.</p>}</section>
  return <>
    <div className="breadcrumb"><Link to="/">← Lobby</Link><span>Battle / {debate.status === 'completed' ? 'Results' : 'Arena'}</span></div>
    <header className="battle-heading"><span className="eyebrow">The proposition</span><h1>{debate.topic.title}</h1><div className="side-labels"><span className="atlas-text">Atlas argues Pro</span><span className="nova-text">Nova argues Con</span></div></header>
    {error && <div className="notice" role="status"><p>Connection interrupted. Showing the last saved state. {error}</p><button className="button" onClick={refresh}>Reconnect</button></div>}
    {runError && <div className="notice" role="alert"><p>{runError}</p></div>}
    <SpeakerStatus debate={debate} />
    {debate.status === 'created' && <section className="panel start-panel"><h2>The speakers are ready.</h2><p>Opening this page never starts a debate.</p>
      {debate.controls?.can_start ? <button className="button primary" disabled={busy} onClick={() => { void run() }}>{busy ? 'Requesting execution…' : 'Start debate →'}</button>
        : <p className="muted">Only the creator’s browser can start this battle. If its token is lost, execution control cannot be recovered.</p>}</section>}
    {debate.status === 'failed' && <FailureView debate={debate} busy={busy} onResume={() => { void run() }} />}
    {debate.status === 'completed' && debate.judgment && <Results judgment={debate.judgment} messages={debate.messages} />}
    <TranscriptView messages={debate.messages} />
    <p className="small muted">This is a public debate. Share this page’s link to let someone read it; execution control stays in the creator’s browser.</p>
  </>
}
