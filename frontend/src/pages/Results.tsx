import { Link } from 'react-router-dom'
import type { Judgment, Message, Scores } from '../api/types'

const criteria: { key: keyof Scores; label: string; cap: number }[] = [
  { key: 'logical_reasoning', label: 'Logical reasoning', cap: 35 }, { key: 'relevance', label: 'Relevance', cap: 25 },
  { key: 'rebuttal_quality', label: 'Rebuttal quality', cap: 25 }, { key: 'clarity', label: 'Clarity', cap: 15 },
]

export function Results({ judgment, messages = [] }: { judgment: Judgment; messages?: Message[] }) {
  return <section className="results" aria-labelledby="verdict-title">
    <div className="panel verdict"><span className="eyebrow">The verdict</span><h2 id="verdict-title">{judgment.winner === 'draw' ? 'Too close to call.' : `${judgment.winner === 'pro' ? 'Atlas' : 'Nova'} takes the debate.`}</h2>
      <p className="muted">{judgment.winner === 'draw' ? 'A difference of five points or fewer is a draw.' : 'The higher total wins with a lead of at least six points.'}</p>
      <div className="scoreboard"><div className="atlas-text"><span>Atlas · Pro</span><strong>{judgment.pro_total}<small>/100</small></strong></div><span className="versus">{judgment.winner === 'draw' ? 'DRAW' : 'VS'}</span><div className="nova-text"><span>Nova · Con</span><strong>{judgment.con_total}<small>/100</small></strong></div></div>
      <p className="ai-text">{judgment.overall_explanation}</p><p className="small muted">An AI assessment of the arguments presented, not verified fact.</p>
    </div>
    <div className="panel rubric"><h3>Where the points came from</h3><table><caption className="sr-only">Criterion scores awarded to Atlas and Nova</caption><thead><tr><th scope="col">Criterion</th><th scope="col">Atlas</th><th scope="col">Nova</th></tr></thead><tbody>
      {criteria.map(criterion => <tr key={criterion.key}><th scope="row">{criterion.label}<span className="small muted"> / {criterion.cap}</span></th><td>{judgment.pro_scores[criterion.key]}</td><td>{judgment.con_scores[criterion.key]}</td></tr>)}</tbody></table></div>
    <div className="analysis-grid">{(['pro', 'con'] as const).map(side => <section className={`panel analysis ${side === 'pro' ? 'atlas' : 'nova'}`} key={side}>
      <h3>{side === 'pro' ? 'Atlas' : 'Nova'}’s case</h3><h4>Strengths</h4><p className="ai-text">{judgment[`${side}_strengths`]}</p><h4>Weaknesses</h4><p className="ai-text">{judgment[`${side}_weaknesses`]}</p></section>)}</div>
    {judgment.referenced_message_ids.length > 0 && <p className="references">Transcript references: {judgment.referenced_message_ids.map((id, index) => {
      const speech = messages.find(message => message.id === id)
      return <a key={id} href={`#message-${id}`}>{speech ? `Speech ${speech.turn_order}` : `Reference ${index + 1}`}</a>
    })}</p>}
    <Link className="button primary" to="/">Choose a new battle →</Link>
  </section>
}
