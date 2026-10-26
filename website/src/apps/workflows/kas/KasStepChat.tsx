/**
 * One workflow step's own chat session, as the Kiro IDE shows it beside the
 * STEPS tree: `From {chat} › {agent}`, the step's prompt (collapsed when
 * long), the assistant's replies, its reasoning (collapsed), its tool calls as
 * compact rows with expandable output, and a `credits · elapsed · state`
 * footer.
 *
 * Read from `GET /api/workflows/kas/runs/{id}/steps/{node}/transcript`; polled
 * every ~2s while the step runs, read once otherwise.
 */
import { useState, type ReactNode } from 'react'
import {
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  FilePen,
  FileText,
  Globe,
  Loader2,
  Search,
  Send,
  ShieldQuestion,
  Terminal,
  Trash2,
  Wrench,
  XCircle,
} from 'lucide-react'

import type { KasStepEntry, KasStepTranscript } from '../../../api/client'
import { apiErrorCode } from '../../../api/apiError'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtCredits, fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { spanMs } from './kasModel'
import { useNow } from './KasRunBits'
import { useKasReplyToStep, useKasStepTranscript } from './useKasWorkflows'

/** A prompt longer than this many lines (or characters) starts collapsed. */
const PROMPT_LINES = 6
const PROMPT_CHARS = 600

function ToolKindIcon({ kind }: { kind: string | null }) {
  const props = { size: 12, className: 'text-muted shrink-0', 'aria-hidden': true as const }
  switch (kind) {
    case 'read':
      return <FileText {...props} />
    case 'edit':
    case 'move':
      return <FilePen {...props} />
    case 'delete':
      return <Trash2 {...props} />
    case 'search':
      return <Search {...props} />
    case 'execute':
      return <Terminal {...props} />
    case 'fetch':
      return <Globe {...props} />
    case 'think':
      return <Brain {...props} />
    default:
      return <Wrench {...props} />
  }
}

function ToolStatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed':
      return <CheckCircle2 size={12} className="text-ok shrink-0" aria-label={i18nT('apps.workflows.kas.state_completed')} />
    case 'failed':
      return <XCircle size={12} className="text-danger shrink-0" aria-label={i18nT('apps.workflows.kas.state_failed')} />
    case 'waiting_approval':
      return (
        <ShieldQuestion size={12} className="text-warn shrink-0" aria-label={i18nT('apps.workflows.kas.tool_waiting_approval')} />
      )
    // A live call: the gateway reports `running`, KAS itself `in_progress`.
    case 'running':
    case 'in_progress':
      return (
        <Loader2 size={12} className="text-accent shrink-0 motion-safe:animate-spin" aria-label={i18nT('apps.workflows.kas.state_running')} />
      )
    default:
      return <Circle size={12} className="text-muted shrink-0" aria-label={i18nT('apps.workflows.kas.step_pending')} />
  }
}

function UserPrompt({ text }: { text: string }) {
  const long = text.split('\n').length > PROMPT_LINES || text.length > PROMPT_CHARS
  const [open, setOpen] = useState(false)
  return (
    <div className="self-end max-w-[92%] rounded-lg bg-bg-hover text-text px-3 py-2 text-[12.5px]" data-testid="kas-step-user">
      {/* The step prompt is the recipe's Markdown (headings, lists, code), so it
          renders like any chat message; collapsed it keeps ~6 lines' height. */}
      <div className={`break-words ${long && !open ? 'max-h-[9.5em] overflow-hidden' : ''}`}>
        <MarkdownRenderer content={text} />
      </div>
      {long ? (
        <button
          type="button"
          className="mt-1 bg-transparent border-none p-0 text-[11.5px] text-accent cursor-pointer hover:underline"
          onClick={() => setOpen(v => !v)}
          aria-expanded={open}
        >
          {open ? i18nT('apps.workflows.kas.show_less') : i18nT('apps.workflows.kas.show_more')}
        </button>
      ) : null}
    </div>
  )
}

/** Short thinking reads as the step's narration; only a long block folds away. */
const REASONING_INLINE_CHARS = 400

function Reasoning({ text }: { text: string }) {
  if (text.length <= REASONING_INLINE_CHARS) {
    return (
      <div className="text-[12px] text-muted italic break-words" data-testid="kas-step-reasoning">
        <MarkdownRenderer content={text} />
      </div>
    )
  }
  return (
    <details className="text-[12px] text-muted" data-testid="kas-step-reasoning">
      <summary className="cursor-pointer select-none">{i18nT('apps.workflows.kas.thinking')}</summary>
      <div className="mt-1 pl-3 border-l border-border break-words">
        <MarkdownRenderer content={text} />
      </div>
    </details>
  )
}

function ToolRow({ entry }: { entry: Extract<KasStepEntry, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false)
  // What the call DID (the command, the path), like the chat's own tool rows;
  // the generic title ("Run Command") only when the call carried no input.
  const label = entry.input || entry.title || entry.name
  const detail = entry.output
  return (
    <div className="rounded border border-border bg-bg text-text" data-testid="kas-step-tool" data-status={entry.status}>
      <button
        type="button"
        className="w-full flex items-center gap-1.5 px-2 py-1 bg-transparent border-none text-left text-[12px] text-text cursor-pointer disabled:cursor-default"
        onClick={() => setOpen(v => !v)}
        aria-expanded={detail ? open : undefined}
        disabled={!detail}
        title={entry.title || undefined}
      >
        {detail ? (
          open ? (
            <ChevronDown size={11} className="text-muted shrink-0" aria-hidden="true" />
          ) : (
            <ChevronRight size={11} className="text-muted shrink-0" aria-hidden="true" />
          )
        ) : (
          <span className="w-[11px] shrink-0" aria-hidden="true" />
        )}
        <ToolKindIcon kind={entry.toolKind} />
        <span className="truncate min-w-0 font-mono text-[11.5px]">{label}</span>
        <span className="ml-auto">
          <ToolStatusIcon status={entry.status} />
        </span>
      </button>
      {open && detail ? (
        <pre className="m-0 border-t border-border px-2 py-1.5 text-[11.5px] font-mono whitespace-pre-wrap break-all max-h-60 overflow-auto bg-bg text-text">
          {entry.input && entry.input.length > 120 ? <code className="block mb-1 text-muted">{entry.input}</code> : null}
          <code>{detail}</code>
        </pre>
      ) : null}
    </div>
  )
}

function Entry({ entry }: { entry: KasStepEntry }) {
  if (entry.kind === 'user') return <UserPrompt text={entry.text} />
  if (entry.kind === 'tool') return <ToolRow entry={entry} />
  if (entry.reasoning) return <Reasoning text={entry.text} />
  return (
    <div className="text-[13px] text-text min-w-0" data-testid="kas-step-assistant">
      <MarkdownRenderer content={entry.text} />
    </div>
  )
}

function Footer({ transcript, running, waiting }: { transcript: KasStepTranscript; running: boolean; waiting: boolean }) {
  const now = useNow(running)
  const parts: string[] = []
  if (transcript.credits != null) parts.push(i18nT('apps.workflows.kas.credits', { credits: fmtCredits(transcript.credits) }))
  const elapsed = spanMs(transcript.startedAt, running ? null : transcript.updatedAt, now)
  if (elapsed != null) parts.push(fmtElapsed(elapsed, { wholeSeconds: true }))
  // A step parked for the person's reply is neither in progress nor done.
  parts.push(
    waiting
      ? i18nT('apps.workflows.kas.waiting_reply')
      : running
        ? i18nT('apps.workflows.kas.in_progress')
        : i18nT('apps.workflows.kas.done'),
  )
  return (
    <p className="m-0 pt-2 border-t border-border text-[11px] text-muted tabular-nums" data-testid="kas-step-footer">
      {parts.join(' · ')}
    </p>
  )
}

/**
 * A composer under the transcript for a step parked on the person's next
 * message. Ctrl/Cmd+Enter sends; the Send button is disabled while empty or in
 * flight; a server refusal shows inline; a sent reply clears the box and the
 * transcript refetches (the hook invalidates its query).
 */
function StepReplyComposer({
  workflowId,
  nodeId,
  path,
  actions,
}: {
  workflowId: string
  nodeId: string
  path: string | null
  /** Mark done / Mark failed, beside Send. */
  actions?: ReactNode
}) {
  const [draft, setDraft] = useState('')
  const reply = useKasReplyToStep(workflowId, nodeId, path)
  const trimmed = draft.trim()
  const disabled = reply.isPending || trimmed.length === 0

  const send = () => {
    if (disabled) return
    reply.mutate(trimmed, { onSuccess: () => setDraft('') })
  }
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault()
      send()
    }
  }
  const error = reply.isError ? (reply.error instanceof Error ? reply.error.message : String(reply.error)) : null

  return (
    <form
      className="flex flex-col gap-1.5 pt-2 border-t border-border"
      data-testid="kas-step-reply"
      onSubmit={e => {
        e.preventDefault()
        send()
      }}
    >
      <label htmlFor="kas-step-reply-input" className="text-[11.5px] font-medium text-text">
        {i18nT('apps.workflows.kas.reply_label')}
      </label>
      <textarea
        id="kas-step-reply-input"
        className="w-full min-h-[3.5rem] resize-y rounded border border-border bg-bg text-text text-[12.5px] px-2 py-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        disabled={reply.isPending}
        aria-label={i18nT('apps.workflows.kas.reply_label')}
      />
      {error ? (
        <p className="m-0 text-[11.5px] text-danger" data-testid="kas-step-reply-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">{actions}</div>
        <button
          type="submit"
          className="inline-flex items-center gap-1.5 rounded bg-accent text-accent-fg text-[12px] px-2.5 py-1 border-none cursor-pointer hover:bg-accent-hover disabled:opacity-50 disabled:cursor-default"
          disabled={disabled}
          aria-busy={reply.isPending || undefined}
        >
          <Send size={12} aria-hidden="true" />
          {i18nT('apps.workflows.kas.reply_send')}
        </button>
      </div>
    </form>
  )
}

export function KasStepChat({
  workflowId,
  nodeId,
  path = null,
  stepRunning,
  canReply = false,
  chatTitle,
  agent,
  stepActions = null,
}: {
  workflowId: string
  nodeId: string | null
  /** The step instance's runtime path; passed to the transcript fetch and reply. */
  path?: string | null
  /** The step is running per the run's tree: keep polling. */
  stepRunning: boolean
  /** The step is parked for the person's next message: show the reply composer. */
  canReply?: boolean
  /** The parent chat's title, for the breadcrumb; null when unknown. */
  chatTitle: string | null
  /** The step's agent from the run tree, until the transcript names one. */
  agent: string | null
  /** Controls on this step (Mark done / Mark failed); drawn beside the reply box. */
  stepActions?: ReactNode
}) {
  const query = useKasStepTranscript(workflowId, nodeId, stepRunning, path)
  const transcript = query.data
  const running = stepRunning || transcript?.status === 'running'
  const agentName = transcript?.agent ?? agent

  let body
  if (!nodeId) {
    body = <p className="m-0 text-[12px] text-muted">{i18nT('apps.workflows.kas.select_step')}</p>
  } else if (query.isError && query.error && (apiErrorCode(query.error) === 'step_unknown' || (query.error as { status?: number }).status === 404)) {
    body = (
      <p className="m-0 text-[12px] text-muted" data-testid="kas-step-not-started">
        {i18nT('apps.workflows.kas.step_not_started')}
      </p>
    )
  } else if (query.isError) {
    body = (
      <ErrorNotice
        message={i18nT('apps.workflows.kas.step_chat_failed', {
          error: query.error instanceof Error ? query.error.message : String(query.error),
        })}
        variant="inline"
      />
    )
  } else if (!transcript) {
    body = <p className="m-0 text-[12px] text-muted">{i18nT('apps.workflows.kas.loading_step')}</p>
  } else {
    body = (
      <>
        {transcript.truncated ? (
          <p className="m-0 text-[11px] text-muted italic">{i18nT('apps.workflows.kas.transcript_truncated')}</p>
        ) : null}
        {transcript.entries.length === 0 ? (
          <p className="m-0 text-[12px] text-muted">{i18nT('apps.workflows.kas.no_messages')}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {transcript.entries.map(entry => (
              <Entry key={entry.id} entry={entry} />
            ))}
          </div>
        )}
        <Footer transcript={transcript} running={running && !canReply} waiting={canReply} />
      </>
    )
  }

  return (
    <section
      className="flex flex-col gap-2 min-w-0"
      aria-label={i18nT('apps.workflows.kas.step_chat')}
      data-testid="kas-step-chat"
    >
      <p className="m-0 text-[11.5px] text-muted truncate" data-testid="kas-step-breadcrumb">
        {i18nT('apps.workflows.kas.from_chat', { chat: chatTitle || i18nT('apps.workflows.kas.this_chat') })}
        {agentName ? (
          <>
            {' › '}
            <span className="text-text font-medium">{agentName}</span>
          </>
        ) : null}
      </p>
      {body}
      {canReply && nodeId ? (
        <StepReplyComposer workflowId={workflowId} nodeId={nodeId} path={path} actions={stepActions} />
      ) : stepActions ? (
        <div className="pt-2 border-t border-border">{stepActions}</div>
      ) : null}
    </section>
  )
}
