import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { Lobby } from './pages/Lobby'
import { Arena } from './pages/Arena'
import { RecentBattles } from './pages/RecentBattles'

export function App() {
  const location = useLocation()
  useEffect(() => { window.scrollTo(0, 0); document.getElementById('main-content')?.focus() }, [location.pathname])
  return <><a className="skip-link" href="#main-content">Skip to content</a><header className="site-header"><div className="header-inner"><Link className="brand" to="/" aria-label="BotArena home"><span className="brand-symbol" aria-hidden="true">B</span>Bot<span>Arena</span></Link><nav aria-label="Main navigation"><NavLink to="/" end>Lobby</NavLink><NavLink to="/history">Recent battles</NavLink></nav></div></header>
    <main className="shell" id="main-content" tabIndex={-1}><Routes><Route path="/" element={<Lobby />} /><Route path="/debates/:id" element={<Arena />} /><Route path="/history" element={<RecentBattles />} /><Route path="*" element={<section className="panel empty"><h1>Page not found.</h1><Link to="/">Return to the lobby</Link></section>} /></Routes></main>
    <footer className="site-footer"><div className="shell"><span>BotArena</span><span>Two perspectives. Room for your own.</span><span>AI arguments · AI assessment</span></div></footer></>
}
