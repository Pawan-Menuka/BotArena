import { useRef } from 'react'
import type { Message } from '../api/types'

const rounds = ['Opening', 'Rebuttal', 'Closing']

export function TranscriptView({ messages }: { messages: Message[] }) {
  const end = useRef<HTMLDivElement>(null)
  return <section aria-labelledby="transcript-title" className="transcript-section">
    <div className="section-heading"><h2 id="transcript-title">The debate</h2>
      {messages.length > 0 && <button className="button subtle" onClick={() => {
        end.current?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'end' })
      }}>Jump to latest ↓</button>}</div>
    {messages.length === 0 ? <div className="panel empty"><h3>The floor is open.</h3><p>Committed speeches will appear here, one turn at a time.</p></div>
      : messages.map(message => <article className={`speech ${message.agent}`} key={message.id} id={`message-${message.id}`}>
        <div className="speech-meta"><span className="avatar" aria-hidden="true">{message.agent === 'atlas' ? 'A' : 'N'}</span>
          <strong>{message.agent === 'atlas' ? 'Atlas' : 'Nova'}</strong><span>{message.side === 'pro' ? 'Pro' : 'Con'} · {rounds[message.round_number - 1]}</span>
          <span className="turn-number">Turn {message.turn_order} / 6</span></div>
        <p className="ai-text">{message.content}</p>
      </article>)}
    <div ref={end} />
  </section>
}
