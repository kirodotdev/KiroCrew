/**
 * WorkflowRunCard — a persistent, clickable inline card rendered in the chat
 * message flow for a `workflow_run` tool call. Unlike the transient
 * WorkflowProgressBar (which lives above the composer and drops shortly after a
 * run ends), this card stays in scrollback anchored to the invocation that
 * launched the run.
 *
 * It subscribes to the same Redux slice the progress bar uses
 * (`chat.workflowRuns[run_id]`, folded from `workflow_run_event` WS frames) for
 * live status / phase / last-log, and the whole card is a button that opens the
 * Workflows side panel (`openActivityToTab('workflows')`) so the user can drill
 * into the phase tree, source, and events. For historical runs that have since
 * been dropped from the live slice, it renders a neutral clickable state — the
 * panel still has the full run history from the backend.
 */
import { memo } from 'react'
import { Workflow, Loader2, CheckCircle2, AlertCircle } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, switchSlot } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
/** The `workflow_run` tool result reads "Started workflow run `wf_NNNNNN`…"
 *  (see the workflow_run handler in mcp_core.py). Matching that phrase both
 *  identifies the call as a launch and captures its run id — and works for
 *  historical messages too, since the tool output is persisted on meta.output. */
const WF_RUN_ID_RE = /Started workflow run `(wf_[A-Za-z0-9_]+)`/

/** Extract the wf_ run id from a tool message's persisted output, or null when
 *  the message is not a completed workflow_run launch. Pure — no hooks — so it
 *  is safe to call from render dispatch and from TurnBlock's grouping logic. */
export function extractWorkflowRunId(message: ChatMessage): string | null {
  const output = (message.meta?.output as string | undefined) || ''
  const m = WF_RUN_ID_RE.exec(output)
  return m ? m[1] : null
}

/** True when a chat message is a workflow_run launch that should render as the
 *  inline card (and therefore must NOT be folded into TurnBlock's collapsible
 *  tool-call group). */
export function isWorkflowRunTool(message: ChatMessage): boolean {
  return message.role === 'tool' && extractWorkflowRunId(message) !== null
}

/** Best-effort friendly label from the tool input JSON (name, else intent). */
function parseLaunchLabel(message: ChatMessage): string {
  try {
    const input = (message.meta?.input as string | undefined) || ''
    if (!input) return ''
    const obj = JSON.parse(input) as { name?: unknown; intent?: unknown }
    return String(obj.name || obj.intent || '').trim()
  } catch {
    return ''
  }
}

const WorkflowRunCard = memo(function WorkflowRunCard({
  runId,
  message,
  slot,
}: {
  runId: string
  message: ChatMessage
  /** Session this card belongs to. Supplied by a surface that can render a
   *  NON-active session (a split-view pane); omitted by single chat, which only
   *  ever draws the active one. */
  slot?: string
}) {
  const dispatch = useAppDispatch()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const run = useAppSelector(s => s.chat.workflowRuns?.[runId])
  const status = run?.status

  const name = sanitizeLlmOutput((run?.name || parseLaunchLabel(message) || runId).slice(0, 80))
  const phase = sanitizeLlmOutput((run?.phase || '').slice(0, 40))
  const lastLog = sanitizeLlmOutput((run?.lastLog || '').slice(0, 120))
  const errMsg = sanitizeLlmOutput((run?.error || '').slice(0, 120))

  const open = () => {
    // The Workflows panel is mounted for `activeSlot`, which split view never
    // moves with pane focus — so opening from a background pane must make this
    // card's session active first, or the panel belongs to another session.
    if (slot && slot !== activeSlot) dispatch(switchSlot(slot))
    dispatch(openActivityToTab('workflows'))
  }

  // Row geometry -- the px-4 gutter and the --mc-content-width clamp -- belongs to
  // the HOST row wrapper, never to this card. ChatPage wraps every renderMessage
  // result, and the shared registries wrap this card through ctx.row. Re-applying
  // it here nested one clamp inside another and inset the card by a second full
  // gutter, so it sat 20px right of every sibling row and 40px narrower.
  return (
    <button
      type="button"
      onClick={open}
      title={i18nT('pages.chat.workflowRunCard.open_in_the_workflows_panel')}
      className="pi-morph group w-full text-left rounded-md bg-accent/10 border border-accent/20 hover:bg-accent/15 hover:border-accent/40 transition-colors px-3 py-2 flex items-start gap-2"
    >
      <span className="shrink-0 mt-0.5">
        {status === 'running' && <Loader2 size={15} className="text-accent animate-spin" />}
        {status === 'finished' && <CheckCircle2 size={15} className="text-green-500" />}
        {(status === 'failed' || status === 'cancelled') && <AlertCircle size={15} className="text-danger" />}
        {!status && <Workflow size={15} className="text-accent/70" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <Workflow size={12} className="text-accent/70 shrink-0" />
          <span className="truncate text-[13px] font-medium text-text-strong">{name}</span>
          {status && (
            <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-accent/10 border border-accent/20 text-accent">
              {status === 'running' && phase ? phase : status}
            </span>
          )}
        </div>
        {status === 'running' && lastLog && (
          <div className="text-[12px] text-muted italic truncate mt-0.5">{lastLog}</div>
        )}
        {(status === 'failed' || status === 'cancelled') && errMsg && (
          <div className="text-[12px] text-danger truncate mt-0.5">{errMsg}</div>
        )}
        <div className="text-[10px] text-muted font-mono truncate mt-0.5">
          {runId} {i18nT('pages.chat.workflowRunCard.open_workflows_panel')}
        </div>
      </div>
      <PanelRightSolid
        size={14}
        className="text-muted shrink-0 mt-0.5 opacity-60 group-hover:opacity-100 transition-opacity"
      />
    </button>
  )
})

export default WorkflowRunCard
