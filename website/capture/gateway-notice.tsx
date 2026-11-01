/**
 * Isolated capture entry for the gateway status-row notices.
 *
 * WHY ISOLATED: each row is a recovery or empty-response outcome a live gateway
 * produces only after a real connection loss, a busy session, a stalled turn or
 * an empty model reply. The REAL transcript renderer registry draws them here
 * from fabricated rows carrying the gateway's English content and its notice
 * token, nothing stubbed but the data.
 *
 * Query: ?theme=dark|light&lang=en|de
 */
import { createRoot } from 'react-dom/client'
import type { ReactNode } from 'react'

import { initI18n } from '../src/i18n/all'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../src/app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const lang = params.get('lang') || 'en'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const row = (role: string, content: string, meta: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', meta }) as unknown as ChatMessage

const ROWS: ChatMessage[] = [
  row('error', '⟳ Connection lost (exit 1) — please retry.', { notice: 'connection_lost_give_up', notice_args: { exit_code: 1 } }),
  row('error', '⟳ Session busy — please retry.', { notice: 'session_busy_give_up' }),
  row('error', '⟳ Turn stalled — please retry.', { notice: 'turn_stalled_give_up' }),
  row('error', 'Session stuck — please start a new chat.', { notice: 'session_stuck' }),
  row('notice', 'ℹ The model returned nothing this turn. Just send your message again to continue.', {
    kind: 'empty_turn_notice',
    notice: 'empty_turn_give_up',
  }),
  row('notice', "⚠ Your account cannot use model 'claude-opus' — running on 'auto' instead.", {
    notice: 'model_access_fallback',
    notice_args: { rejected: 'claude-opus', model: 'auto' },
  }),
]

/** The remaining families, drawn in their own scene. */
const MORE_ROWS: ChatMessage[] = [
  row('error', '⟳ Connection lost — retrying…', { kind: 'transient_retry', notice: 'connection_lost_retrying' }),
  row('error', '⟳ Session busy — retrying…', { kind: 'transient_retry', notice: 'session_busy_retrying' }),
  row('error', '⟳ Recovering a stalled turn…', { kind: 'transient_retry', notice: 'turn_stalled_recovering' }),
  row('error', '⟳ Tool appeared stalled — please retry.', { notice: 'tool_stalled_give_up' }),
  row('error', 'The agent process this chat shares kept restarting — please retry.', { notice: 'shared_runtime_restarting' }),
  row('notice', 'ℹ The model returned nothing — auto-continuing (recovery 2 of 3).', {
    kind: 'empty_turn_notice',
    notice: 'empty_turn_continuing',
    notice_args: { n: 2, max: 3 },
  }),
  row('notice', 'ℹ The turn ended without a closing reply. Send a message to continue from where it stopped — completed steps will not re-run.', {
    kind: 'empty_turn_notice',
    notice: 'empty_turn_after_work',
  }),
  row('notice', 'ℹ️ The context was compacted mid-turn and the response stopped there — continuing automatically.', {
    kind: 'empty_turn_notice',
    notice: 'compaction_continuing',
  }),
  row(
    'notice',
    "claude-opus isn't offered right now — this session is running on auto instead. Pick another model from the composer, or leave it: your model choice is kept and will be used automatically once it's offered again.",
    { notice: 'model_unavailable', notice_args: { model: 'claude-opus' } },
  ),
]

/** Session reconnect, model-session rebuild and cancelled-replay rows. */
const RECOVERY_ROWS: ChatMessage[] = [
  row("notice", "⚠️ claude-opus is throttled — running on claude-sonnet until claude-opus recovers.", {"notice": "model_throttled_fallback", "notice_args": {"primary": "claude-opus", "fallback": "claude-sonnet"}}),
  row("error", "⟳ The agent lost this chat's session — reconnecting…", {"notice": "session_reconnecting", "kind": "transient_retry"}),
  row("error", "❌ Could not reconnect this chat's session. Send your message again, or start a new chat if this keeps happening.", {"notice": "session_reconnect_give_up"}),
  row("error", "⟳ The agent could not load its cloud credentials — restarting it to load them again…", {"notice": "credentials_reloading", "kind": "transient_retry"}),
  row("error", "⟳ Preserved model reasoning no longer matches this conversation — rebuilding the model session from this chat and retrying once…", {"notice": "reasoning_mismatch_rebuilding", "kind": "transient_retry"}),
  row("error", "⟳ The backend retained an image it can no longer process — restarting the model session without binary image history and continuing…", {"notice": "image_history_restarting", "kind": "transient_retry"}),
  row("error", "⟳ The backend keeps rejecting this conversation — restarting the model session and retrying (this chat's messages are kept; the model rebuilds its working context from them)…", {"notice": "conversation_rejected_restarting", "kind": "transient_retry"}),
  row("error", "⟳ Compaction failed — retrying…", {"notice": "compaction_retrying", "kind": "transient_retry"}),
  row("error", "⟳ Session reset — processing next message with conversation history", {"notice": "session_reset_continuing"}),
  row("notice", "ℹ️ Auto-continue cancelled — your message takes over.", {"notice": "auto_continue_cancelled_superseded"}),
  row("notice", "ℹ️ Auto-continue cancelled — this chat moved to another session, nothing was run.", {"notice": "auto_continue_cancelled_rebound"}),
  row("notice", "ℹ️ Auto-continue cancelled — the turn was stopped, nothing was run.", {"notice": "auto_continue_cancelled_stopped"}),
  row("notice", "ℹ️ Model-fallback retry cancelled — this chat moved to another session.", {"notice": "fallback_retry_cancelled_rebound"}),
  row("notice", "ℹ️ Model-fallback retry cancelled — your newer message runs instead.", {"notice": "fallback_retry_cancelled_superseded"}),
  row("notice", "ℹ️ Model-fallback retry cancelled — the turn was stopped.", {"notice": "fallback_retry_cancelled_stopped"}),
  row("notice", "ℹ️ Content-filter retry cancelled — this chat moved to another session.", {"notice": "filter_retry_cancelled_rebound"}),
  row("notice", "ℹ️ Content-filter retry cancelled — your newer message runs instead.", {"notice": "filter_retry_cancelled_superseded"}),
  row("notice", "ℹ️ Content-filter retry cancelled — the turn was stopped.", {"notice": "filter_retry_cancelled_stopped"}),
  row("notice", "ℹ️ Session reconnect cancelled — nothing was run.", {"notice": "session_reconnect_cancelled"}),
  row("notice", "ℹ️ Image-history recovery cancelled — nothing was run.", {"notice": "image_recovery_cancelled"}),
  row("notice", "ℹ️ Model-session recovery cancelled — nothing was run.", {"notice": "model_session_recovery_cancelled"}),
]

/** End-of-turn, queue, agent-switch and admission rows. */
const TURN_ROWS: ChatMessage[] = [
  row("notice", "ℹ️ The model wrote a tool call as plain text instead of making it, so nothing was run. If this happens again in this chat, start a new chat (or compact this one): the text stays in this chat's context, so a plain re-send often repeats it.", {"notice": "tool_call_as_text"}),
  row("notice", "ℹ️ The model wrote its last tool call as plain text instead of making it, so that call did not run — the 3 call(s) before it were attempted and may already have taken effect, so part of this turn may have landed and part did not. Check what landed before re-sending. If this happens again in this chat, start a new chat (or compact this one).", {"notice": "tool_call_as_text_partial", "notice_args": {"tool_calls": 3}}),
  row("notice", "ℹ️ A tool call leaked into the reply text instead of executing, just before the context was compacted — that call did not run. Any raw `<invoke>` text above is that leak, not a reply.", {"notice": "tool_call_leaked_at_compaction"}),
  row("notice", "⟳ The last tool call was refused for lack of gateway capacity — retrying it in 5s (attempt 2).", {"notice": "capacity_retrying", "notice_args": {"delay_secs": 5, "attempt": 2}}),
  row("notice", "ℹ The capacity retry was cancelled — your message takes over.", {"notice": "capacity_retry_cancelled_superseded"}),
  row("notice", "ℹ The capacity retry was cancelled — the turn was stopped, the call was not retried.", {"notice": "capacity_retry_cancelled_stopped"}),
  row("notice", "ℹ️ The last tool call was refused for lack of gateway capacity and the automatic retries are spent (3). Re-send the request once the gateway has capacity.", {"notice": "capacity_retries_spent", "notice_args": {"attempt": 3}}),
  row("notice", "ℹ️ The model ended after saying it would act but didn't. Auto-continue is skipped under auto-approve mode — re-send your request to carry it out.", {"notice": "intent_without_action_auto_approve"}),
  row("notice", "ℹ️ The model ended after saying it would act but didn't — auto-continuing once.", {"notice": "intent_without_action_continuing"}),
  row("notice", "ℹ️ The model again ended after saying it would act but didn't. The one automatic retry is already spent — send the request again to perform the action.", {"notice": "intent_without_action_spent"}),
  row("notice", "ℹ️ The model incorrectly stopped after read-only preparation — retrying your request once.", {"notice": "read_only_stop_retrying"}),
  row("notice", "ℹ️ This turn ended after the model said work was still in progress. No further main-agent steps run from this completed turn unless a Stop hook explicitly requests a bounded continuation. Separately shown subagents or monitor loops, if any, continue on their own; otherwise send a message to resume.", {"notice": "work_in_progress_ended"}),
  row("notice", "ℹ️ The reply says the agent will follow up later, but this turn set up no monitor, scheduled job or background run, and no monitor is running on this session — so nothing will wake it to send that update. Send a message when you want it, or ask the agent to set up a monitor.", {"notice": "deferred_followup"}),
  row("notice", "ℹ️ Memory is still being prepared after the gateway restart. Queued messages will run when it is ready.", {"notice": "memory_preparing"}),
  row("notice", "📌 Agent switch to researcher was blocked — this thread is pinned to nova. The next turn restarts on the pinned crew.", {"notice": "agent_switch_blocked_pinned", "notice_args": {"agent": "researcher", "member": "nova"}}),
  row("notice", "📌 Agent switch to researcher was blocked — this chat is in Crew Mode, which stays on its conductor. The next turn restarts on the conductor.", {"notice": "agent_switch_blocked_crew_mode", "notice_args": {"agent": "researcher"}}),
  row("error", "This thread's crew name cannot be dispatched. Rename or recreate the Crew Member.", {"notice": "crew_name_undispatchable"}),
  row("error", "This chat ran on a remote crew and is read-only now. Open the crew's own session to keep going.", {"notice": "remote_crew_read_only"}),
  row("error", "⚠️ Archiving this tab failed after its running turn was cancelled. The tab was kept, but that turn did not finish -- re-send to continue.", {"notice": "archive_failed_turn_cancelled"}),
]

const registry = mergeRenderers(createTranscriptRenderers({ slot: 's1', continuable: false, interrupted: false }))

function draw(m: ChatMessage, index: number): ReactNode {
  const entry = resolveRenderer(m, registry)
  const ctx = {
    index,
    messages: [m],
    running: false,
    key: `k${index}`,
    onFileOpen: () => {},
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    wrapper: (children: ReactNode) => children,
    row: (children: ReactNode) => children,
  } as unknown as MessageRenderContext
  return entry ? entry.render(m, ctx) : null
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(
  <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 16 }}>
    <div data-scene="rows" style={{ width: 640, padding: 8, display: 'grid', gap: 8 }}>
      <div className="font-mono text-[11px] text-muted">Gateway status rows ({lang})</div>
      {ROWS.map((m, i) => (
        <div key={i}>{draw(m, i)}</div>
      ))}
    </div>
    <div data-scene="more" style={{ width: 640, padding: 8, display: 'grid', gap: 8, marginTop: 24 }}>
      <div className="font-mono text-[11px] text-muted">More gateway status rows ({lang})</div>
      {MORE_ROWS.map((m, i) => (
        <div key={i}>{draw(m, i)}</div>
      ))}
    </div>
    <div data-scene="recovery" style={{ width: 640, padding: 8, display: 'grid', gap: 8, marginTop: 24 }}>
      <div className="font-mono text-[11px] text-muted">Reconnect, rebuild and cancelled-replay rows ({lang})</div>
      {RECOVERY_ROWS.map((m, i) => (
        <div key={i}>{draw(m, i)}</div>
      ))}
    </div>
    <div data-scene="turn" style={{ width: 640, padding: 8, display: 'grid', gap: 8, marginTop: 24 }}>
      <div className="font-mono text-[11px] text-muted">End-of-turn, queue, agent-switch and admission rows ({lang})</div>
      {TURN_ROWS.map((m, i) => (
        <div key={i}>{draw(m, i)}</div>
      ))}
    </div>
  </div>,
)
