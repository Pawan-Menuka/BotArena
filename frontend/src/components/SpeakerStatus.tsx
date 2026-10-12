import type { Snapshot } from '../api/types'

export function SpeakerStatus({ debate }: { debate: Snapshot }) {
  const { status, progress } = debate
  const label = status === 'judging' ? 'The judge is evaluating both sides'
    : status === 'running' ? `${progress.active_speaker === 'nova' ? 'Nova' : 'Atlas'} ${progress.activity === 'retrying' ? 'is retrying this speech' : 'has the floor'}`
    : status === 'created' ? 'Ready when you are' : status === 'failed' ? 'Debate paused' : 'Debate complete'
  return <section className="panel status-panel" aria-label="Debate progress">
    <div className="section-heading"><span className="eyebrow">{status === 'running' ? `Round ${progress.current_round} of 3` : 'Battle status'}</span>
      <span className="muted">{progress.completed_turns} of 6 speeches saved</span></div>
    <h2 aria-live="polite" aria-atomic="true">{label}</h2>
    <div className="round-track" aria-hidden="true">{Array.from({ length: 6 }, (_, index) =>
      <span key={index} className={index < progress.completed_turns ? (index % 2 ? 'saved nova-fill' : 'saved atlas-fill') : ''} />)}</div>
    <div className="round-labels"><span>Opening</span><span>Rebuttal</span><span>Closing</span></div>
  </section>
}
