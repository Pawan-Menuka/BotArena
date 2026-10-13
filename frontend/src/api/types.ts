export interface Topic { id: string; title: string; pro_framing: string; con_framing: string }
export type TopicChoice = { topic_id: string; custom_topic?: never } | { custom_topic: string; topic_id?: never }
export type Status = 'created' | 'running' | 'judging' | 'completed' | 'failed'
export interface Message {
  id: string; agent: 'atlas' | 'nova'; side: 'pro' | 'con'; round_number: number
  turn_order: number; content: string; created_at: string
}
export interface Scores { logical_reasoning: number; relevance: number; rebuttal_quality: number; clarity: number }
export interface Judgment {
  pro_scores: Scores; con_scores: Scores; pro_total: number; con_total: number; winner: 'pro' | 'con' | 'draw'
  pro_strengths: string; pro_weaknesses: string; con_strengths: string; con_weaknesses: string
  overall_explanation: string; referenced_message_ids: string[]
  model_id: string; prompt_version: string; rubric_version: string
}
export interface Snapshot {
  id: string; topic: Topic; status: Status; revision: number
  created_at: string; updated_at: string; completed_at: string | null
  progress: { completed_turns: number; total_turns: number; current_round: number
    active_speaker: 'atlas' | 'nova' | null; active_turn: number | null
    activity: 'generating' | 'retrying' | 'evaluating' | null }
  messages: Message[]; judgment: Judgment | null
  error: { code: string; message: string; failed_stage: string; failed_turn: number | null } | null
  controls?: { can_start: boolean; can_resume: boolean; remaining_attempts: number }
}
export interface HistoryItem { id: string; topic: Topic; status: Status; created_at: string; completed_at: string | null }
export interface HistoryPage { items: HistoryItem[]; next_cursor: string | null }
