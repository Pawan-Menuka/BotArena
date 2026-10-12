import type { Snapshot } from '../src/api/types'

export const topic = { id: 'four-day-work-week', title: 'Should a four-day work week become the standard?', pro_framing: 'More time to recharge.', con_framing: 'What happens to coverage?' }
export function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return { id: 'debate-1', topic, status: 'created', revision: 0, created_at: '2026-10-11T00:00:00Z',
    updated_at: '2026-10-11T00:00:00Z', completed_at: null,
    progress: { completed_turns: 0, total_turns: 6, current_round: 0, active_speaker: null, active_turn: null, activity: null },
    messages: [], judgment: null, error: null,
    controls: { can_start: true, can_resume: false, remaining_attempts: 3 }, ...overrides }
}
export const message = { id: 'm1', agent: 'atlas' as const, side: 'pro' as const, round_number: 1, turn_order: 1,
  content: '<script>window.hacked=true</script><img src=x onerror=alert(1)>', created_at: '2026-10-11T00:00:01Z' }
export const judgment = { pro_scores: { logical_reasoning: 28, relevance: 20, rebuttal_quality: 20, clarity: 12 },
  con_scores: { logical_reasoning: 26, relevance: 19, rebuttal_quality: 18, clarity: 12 }, pro_total: 80, con_total: 75, winner: 'draw' as const,
  pro_strengths: 'Clear reasoning.', pro_weaknesses: 'Some gaps.', con_strengths: 'Useful questions.', con_weaknesses: 'Little detail.',
  overall_explanation: '<b>This is plain text</b>', referenced_message_ids: ['m1'], model_id: 'fake', prompt_version: '1', rubric_version: '1' }
