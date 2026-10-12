import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../api/client'
import type { Topic } from '../api/types'
import { usePendingCreation } from '../hooks/usePendingCreation'
import { prepareCreation, recoverCreation } from '../storage/pendingCreation'

export function Lobby() {
  const [topics, setTopics] = useState<Topic[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const submitting = useRef(false)
  const recovery = usePendingCreation()
  const navigate = useNavigate()

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    void api.topics(controller.signal).then(result => { setTopics(result.topics); setError(null) })
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [reload])

  async function start(topic: Topic) {
    if (submitting.current) return
    submitting.current = true
    setBusy(topic.id)
    setError(null)
    try {
      const pending = prepareCreation(topic.id)
      const debate = await recoverCreation(pending)
      // Start only belongs to this explicit click; recovery and page mount never execute it.
      try { await api.run(debate.id, pending.token) }
      catch { /* The arena fetches server state and offers an explicit Start if admission failed. */ }
      navigate(`/debates/${debate.id}`)
    } catch (failure) { setError(errorMessage(failure)); recovery.reloadPending() }
    finally { submitting.current = false; setBusy(null) }
  }

  return <>
    <section className="hero"><span className="eyebrow">A meeting of opposing minds</span>
      <h1>One topic.<br />Two sides.<span className="accent"> You decide.</span></h1>
      <p>Watch Atlas and Nova make their case in a six-speech debate.<br className="desktop-break" /> Then explore the arguments behind an AI verdict.</p>
      <div className="debaters"><div className="debater atlas"><span className="avatar large">A</span><div><strong>Atlas</strong><span>Pro · Analytical & structured</span></div></div>
        <span className="versus">vs</span><div className="debater nova"><span className="avatar large">N</span><div><strong>Nova</strong><span>Con · Skeptical & probing</span></div></div></div>
    </section>
    {(recovery.pending.length > 0 || recovery.recovered.length > 0 || recovery.error) && <section className="panel recovery" aria-label="Pending battles">
      <h2>Pick up where you left off</h2><p>Recovering a battle saves its link. Execution starts only when you press Start.</p>
      {recovery.error && <p role="alert">{recovery.error}</p>}
      {recovery.pending.map(entry => <div className="recovery-row" key={entry.client_request_id}><span>Pending battle · {new Date(entry.created_at).toLocaleString()}</span>
        <button className="button" disabled={recovery.busy.includes(entry.client_request_id)} onClick={() => { void recovery.recover(entry) }}>Recover pending battle</button>
        <button className="button subtle" disabled={recovery.busy.includes(entry.client_request_id)} onClick={() => recovery.discard(entry.client_request_id)}>Discard</button></div>)}
      {recovery.recovered.map(debate => <Link className="recovered-link" key={debate.id} to={`/debates/${debate.id}`}>Open recovered battle: {debate.topic.title} →</Link>)}
    </section>}
    <section aria-labelledby="topics-title"><div className="section-heading"><div><span className="eyebrow">Choose a proposition</span><h2 id="topics-title">What’s up for debate?</h2></div><span className="muted">3 rounds · 6 speeches · 1 verdict</span></div>
      {error && <div role="alert" className="notice"><p>{error}</p><button className="button" onClick={() => setReload(value => value + 1)}>Try again</button></div>}
      {loading ? <p role="status">Loading topics…</p> : topics.length === 0 && !error ? <p className="panel empty">No topics available yet. Check back shortly.</p> :
        <div className="topic-grid">{topics.map((topic, index) => <article className="panel topic-card" key={topic.id}>
          <span className="topic-number">TOPIC {String(index + 1).padStart(2, '0')}</span><h3>{topic.title}</h3>
          <div className="framing"><p><strong className="atlas-text">Pro</strong> {topic.pro_framing}</p><p><strong className="nova-text">Con</strong> {topic.con_framing}</p></div>
          <button className="button primary" disabled={busy !== null} onClick={() => { void start(topic) }}>{busy === topic.id ? 'Preparing battle…' : 'Start battle →'}</button>
        </article>)}</div>}
    </section>
    <section className="rules"><div><h3>A fair format</h3><p>Opening, rebuttal, closing. Fixed sides and equal speaking turns.</p></div><div><h3>Arguments, assessed</h3><p>An AI judge scores reasoning, relevance, rebuttal, and clarity. This is an assessment, not verified fact.</p><p>Outputs pass through a basic profanity filter. It can miss harmful content and flag legitimate wording.</p></div><div><h3>Come back anytime</h3><p>Speeches are saved as the debate progresses. This browser keeps the token to start or resume it.</p></div></section>
  </>
}
