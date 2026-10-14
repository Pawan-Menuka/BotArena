import { Component, type ReactNode } from 'react'

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <main className="shell"><section className="panel">
      <h1>Something went wrong.</h1><p>Your saved debates are still on the server. Reload to reconnect.</p>
      <button className="button primary" onClick={() => window.location.reload()}>Reload BotArena</button>
    </section></main> : this.props.children
  }
}
