import type { Snapshot } from '../api/types'

export function FailureView({ debate, busy, onResume }: { debate: Snapshot; busy: boolean; onResume: () => void }) {
  return <section className="panel failure" aria-labelledby="failure-title">
    <span className="eyebrow">Saved, not lost</span><h2 id="failure-title">The debate hit a pause.</h2>
    <p>{debate.error?.message ?? 'Execution stopped. Saved speeches are preserved.'}</p>
    {debate.error?.failed_stage && <p className="muted">Stopped during {debate.error.failed_turn ? `speech ${debate.error.failed_turn}` : debate.error.failed_stage.replaceAll('_', ' ')}.</p>}
    {debate.controls?.can_resume ? <><button className="button primary" disabled={busy} onClick={onResume}>{busy ? 'Requesting resume…' : 'Resume debate →'}</button>
      <p className="small muted">Continues from the saved checkpoint. {debate.controls.remaining_attempts} execution attempts remain.</p></>
      : <p className="muted">{debate.controls ? 'This battle cannot be resumed. You can still read its saved transcript.' : 'Only the creator’s browser can resume this battle. You can read all saved speeches here.'}</p>}
  </section>
}
