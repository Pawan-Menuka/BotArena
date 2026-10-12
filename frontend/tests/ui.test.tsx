import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { App } from '../src/App'
import { api } from '../src/api/client'
import { TranscriptView } from '../src/components/TranscriptView'
import { ErrorBoundary } from '../src/components/ErrorBoundary'
import { Results } from '../src/pages/Results'
import { prepareCreation, recoverCreation } from '../src/storage/pendingCreation'
import { judgment, message, snapshot, topic } from './fixtures'

it('renders speech and judge HTML literally', () => {
  const { container } = render(<MemoryRouter><TranscriptView messages={[message]} /><Results judgment={judgment} /></MemoryRouter>)
  expect(screen.getByText(message.content)).toBeInTheDocument()
  expect(screen.getByText(judgment.overall_explanation)).toBeInTheDocument()
  expect(container.querySelector('script, img, b')).toBeNull()
})

it('mounting a created arena never starts a debate', async () => {
  vi.spyOn(api, 'snapshot').mockResolvedValue(snapshot())
  const run = vi.spyOn(api, 'run')
  render(<MemoryRouter initialEntries={['/debates/debate-1']}><App /></MemoryRouter>)
  expect(await screen.findByRole('button', { name: 'Start debate →' })).toBeInTheDocument()
  expect(run).not.toHaveBeenCalled()
})

it('recovery replays only create and requires an explicit Start', async () => {
  prepareCreation(topic.id)
  vi.spyOn(api, 'topics').mockResolvedValue({ topics: [topic] })
  vi.spyOn(api, 'create').mockResolvedValue(snapshot())
  const run = vi.spyOn(api, 'run')
  render(<MemoryRouter><App /></MemoryRouter>)
  expect(await screen.findByRole('link', { name: /Open recovered battle/ })).toBeInTheDocument()
  expect(api.create).toHaveBeenCalledTimes(1)
  expect(run).not.toHaveBeenCalled()
})

it('old pending creation waits for a recovery click', async () => {
  const entry = prepareCreation(topic.id)
  localStorage.setItem(`botarena:pending:${entry.client_request_id}`, JSON.stringify({ ...entry, created_at: Date.now() - 6 * 60 * 1000 }))
  vi.spyOn(api, 'topics').mockResolvedValue({ topics: [] })
  const create = vi.spyOn(api, 'create').mockResolvedValue(snapshot())
  render(<MemoryRouter><App /></MemoryRouter>)
  const button = await screen.findByRole('button', { name: 'Recover pending battle' })
  expect(create).not.toHaveBeenCalled()
  fireEvent.click(button)
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
})

it('Start saves the pending entry before create and issues exactly one run', async () => {
  vi.spyOn(api, 'topics').mockResolvedValue({ topics: [topic] })
  vi.spyOn(api, 'create').mockImplementation(async () => {
    expect(Object.keys(localStorage).some(key => key.startsWith('botarena:pending:'))).toBe(true)
    return snapshot()
  })
  vi.spyOn(api, 'run').mockResolvedValue({ id: 'debate-1', status: 'running' })
  vi.spyOn(api, 'snapshot').mockResolvedValue(snapshot({ status: 'completed', judgment }))
  render(<MemoryRouter><App /></MemoryRouter>)
  const start = await screen.findByRole('button', { name: 'Start battle →' })
  fireEvent.click(start)
  fireEvent.click(start)
  expect(await screen.findByText('Too close to call.')).toBeInTheDocument()
  expect(api.run).toHaveBeenCalledTimes(1)
})

it('failed public debates offer no execution control', async () => {
  vi.spyOn(api, 'snapshot').mockResolvedValue(snapshot({ status: 'failed', controls: undefined }))
  render(<MemoryRouter initialEntries={['/debates/debate-1']}><App /></MemoryRouter>)
  expect(await screen.findByText('The debate hit a pause.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume debate →' })).toBeNull()
})

it('resumes an eligible failure only after the creator clicks', async () => {
  const pending = prepareCreation(topic.id)
  vi.spyOn(api, 'create').mockResolvedValue(snapshot())
  await recoverCreation(pending)
  vi.spyOn(api, 'snapshot').mockResolvedValueOnce(snapshot({ status: 'failed', controls: { can_start: false, can_resume: true, remaining_attempts: 2 } }))
    .mockResolvedValue(snapshot({ status: 'completed', revision: 2, judgment }))
  const run = vi.spyOn(api, 'run').mockResolvedValue({ id: 'debate-1', status: 'running' })
  render(<MemoryRouter initialEntries={['/debates/debate-1']}><App /></MemoryRouter>)
  const resume = await screen.findByRole('button', { name: 'Resume debate →' })
  expect(run).not.toHaveBeenCalled()
  fireEvent.click(resume)
  expect(await screen.findByText('Too close to call.')).toBeInTheDocument()
  expect(run).toHaveBeenCalledExactlyOnceWith('debate-1', pending.token)
})

it('history is labeled as shared and never executes work', async () => {
  vi.spyOn(api, 'history').mockResolvedValue({ items: [], next_cursor: null })
  const run = vi.spyOn(api, 'run')
  render(<MemoryRouter initialEntries={['/history']}><App /></MemoryRouter>)
  expect(await screen.findByText('The first word is yours.')).toBeInTheDocument()
  expect(screen.getByText(/public archive, not a personal history/)).toBeInTheDocument()
  expect(run).not.toHaveBeenCalled()
})

it('explains the limits of the profanity filter in the lobby', async () => {
  vi.spyOn(api, 'topics').mockResolvedValue({ topics: [] })
  render(<MemoryRouter><App /></MemoryRouter>)
  expect(await screen.findByText('Outputs pass through a basic profanity filter. It can miss harmful content and flag legitimate wording.')).toBeInTheDocument()
})

it('contains a render failure and offers Reload', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  function Broken(): never { throw new Error('broken') }
  await act(async () => { render(<ErrorBoundary><Broken /></ErrorBoundary>) })
  expect(screen.getByRole('button', { name: 'Reload BotArena' })).toBeInTheDocument()
})
