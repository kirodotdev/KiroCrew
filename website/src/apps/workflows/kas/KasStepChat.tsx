/**
 * One workflow step's own chat session, as the Kiro IDE shows it in the run
 * view: the step's prompt, the assistant's replies, and — folded PER REPLY —
 * the reasoning and tool rows that led to each. Every plain reply is preceded
 * by a right-aligned "Thought (N) + Tools (M)" chip holding that work, carries
 * a timestamp beneath it, and a `send_message` note to the parent chat draws
 * its own card. A composer styled like the main chat sits pinned at the bottom,
 * enabled while the step waits for a reply and otherwise disabled with a reason.
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
import { loadChatConfig } from '../../../pages/chat/ChatSettings'
import { fmtCredits, fmtElapsed, fmtTimeNumeric } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { foldReplies, parseParentMessage, spanMs, type StepChip } from './kasModel'
import { SessionMessageCard } from '../../../pages/chat/SessionMessageCard'
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

/** One reasoning or tool entry, inside an expanded chip. */
function WorkEntry({ entry }: { entry: KasStepEntry }) {
  if (entry.kind === 'tool') return <ToolRow entry={entry} />
  if (entry.kind === 'assistant' && entry.reasoning) return <Reasoning text={entry.text} />
  // A user prompt never reaches a chip; an unexpected plain reply renders plainly.
  if (entry.kind === 'assistant') return <div className="text-[13px] text-text min-w-0"><MarkdownRenderer content={entry.text} /></div>
  return null
}

/** The chip's label: "Thought (N) + Tools (M)", omitting a zero half. */
function chipLabel(chip: StepChip): string {
  if (chip.thoughts > 0 && chip.tools > 0) {
    return i18nT('apps.workflows.kas.chip_thought_tools', { thoughts: chip.thoughts, tools: chip.tools })
  }
  if (chip.tools > 0) return i18nT('apps.workflows.kas.chip_tools', { n: chip.tools })
  return i18nT('apps.workflows.kas.chip_thought', { n: chip.thoughts })
}

/** The right-aligned "Thought (N) + Tools (M)" chip of the work since the last
 *  reply. Starts open only when the chat setting "Collapse all steps" is off; a
 *  running tool shows a spinner on the chip without expanding anything. */
function WorkChip({ chip, startOpen }: { chip: StepChip; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen)
  return (
    <div className="flex flex-col gap-1 items-end" data-testid="kas-step-chip" data-open={open ? 'true' : 'false'}>
      <button
        type="button"
        className="self-end inline-flex items-center gap-1 bg-transparent border-none p-0 text-[11.5px] text-muted cursor-pointer hover:text-text"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        {chipLabel(chip)}
        {chip.running ? (
          <Loader2 size={11} className="text-accent motion-safe:animate-spin" aria-hidden="true" data-testid="kas-step-chip-spinner" />
        ) : open ? (
          <ChevronDown size={12} aria-hidden="true" />
        ) : (
          <ChevronRight size={12} aria-hidden="true" />
        )}
      </button>
      {open ? (
        <div className="w-full flex flex-col gap-2 pl-3 border-l border-border">
          {chip.entries.map(e => (
            <WorkEntry key={e.id} entry={e} />
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** A step's `send_message` note to the run's parent chat, drawn as the OUTGOING
 *  session-message card (round 10): the SENDER is this step (its label + agent),
 *  and the body opens with the parent chat as its `@tag`. */
function ParentMessageCard({
  entry,
  nodeId,
  stepLabel,
  agent,
  chatTitle,
}: {
  entry: Extract<KasStepEntry, { kind: 'tool' }>
  nodeId: string
  stepLabel: string | null
  agent: string | null
  chatTitle: string | null
}) {
  const { text } = parseParentMessage(entry)
  const parent = chatTitle || i18nT('apps.workflows.kas.this_chat')
  return (
    <div data-testid="kas-step-parent">
      <SessionMessageCard
        direction="outgoing"
        sender={{ key: nodeId, title: stepLabel || agent || nodeId }}
        recipient={{ key: '', title: parent }}
        body={text}
        agent={agent}
      />
    </div>
  )
}

/** A reply, with its preceding chip and a timestamp beneath it. */
function Reply({ reply, chip, ts, startOpen }: { reply: Extract<KasStepEntry, { kind: 'assistant' }>; chip: StepChip | null; ts: string | null; startOpen: boolean }) {
  return (
    <>
      {chip ? <WorkChip chip={chip} startOpen={startOpen} /> : null}
      <div className="text-[13px] text-text min-w-0" data-testid="kas-step-assistant">
        <MarkdownRenderer content={reply.text} />
      </div>
      {ts ? (
        <p className="m-0 text-[10.5px] text-muted tabular-nums" data-testid="kas-step-ts">
          {fmtTimeNumeric(ts)}
        </p>
      ) : null}
    </>
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
 * The composer under the transcript, styled like the main chat composer and
 * pinned to the bottom of the scrolling pane. It is enabled only while the step
 * waits for the person's next message: Kiro Agent has no way to steer a step
 * that is not parked (a `session/prompt` on a running step would land as an
 * unrelated chat turn), so otherwise it is disabled with a one-line reason.
 * Mark done / Mark failed sit beside Send on the right.
 */
function StepComposer({
  workflowId,
  nodeId,
  path,
  canReply,
  stepRunning,
  agent,
  model,
  effort,
  actions,
}: {
  workflowId: string
  nodeId: string
  path: string | null
  canReply: boolean
  stepRunning: boolean
  agent: string | null
  model: string | null
  effort: string | null
  actions?: ReactNode
}) {
  const [draft, setDraft] = useState('')
  const reply = useKasReplyToStep(workflowId, nodeId, path)
  const trimmed = draft.trim()
  const disabled = !canReply || reply.isPending || trimmed.length === 0
  const modelLine = [model, effort].filter(Boolean).join(' · ')

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
      className="sticky bottom-0 z-[1] bg-card pt-2"
      data-testid="kas-step-reply"
      data-can-reply={canReply ? 'true' : 'false'}
      onSubmit={e => {
        e.preventDefault()
        send()
      }}
    >
      <div className="rounded-lg border border-border bg-bg focus-within:border-accent">
        <label htmlFor="kas-step-reply-input" className="sr-only">
          {i18nT('apps.workflows.kas.reply_label')}
        </label>
        <textarea
          id="kas-step-reply-input"
          className="w-full min-h-[2.75rem] resize-y rounded-t-lg bg-transparent text-text text-[12.5px] px-2.5 py-2 border-none focus-visible:outline-none disabled:opacity-60"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={!canReply || reply.isPending}
          // A reader who opened a waiting step via "Reply" lands in the box.
          autoFocus={canReply}
          placeholder={agent ? i18nT('apps.workflows.kas.composer_placeholder_agent', { agent }) : i18nT('apps.workflows.kas.composer_placeholder')}
          aria-label={i18nT('apps.workflows.kas.reply_label')}
        />
        <div className="flex items-center gap-2 px-2 py-1.5 border-t border-border" data-testid="kas-step-reply-actions">
          {agent ? (
            <span className="shrink-0 rounded px-1.5 py-px text-[10.5px] font-mono bg-bg-hover text-muted border border-border" data-testid="kas-composer-agent">
              {agent}
            </span>
          ) : null}
          {modelLine ? (
            <span className="shrink-0 rounded px-1.5 py-px text-[10.5px] text-muted tabular-nums bg-bg-hover border border-border truncate max-w-[12rem]" data-testid="kas-composer-model">
              {modelLine}
            </span>
          ) : null}
          <span className="ml-auto flex items-center gap-2 shrink-0">
            {actions}
            <button
              type="submit"
              className="inline-flex items-center gap-1.5 rounded bg-accent text-accent-fg text-[12px] px-2.5 py-1 border-none cursor-pointer hover:bg-accent-hover disabled:opacity-50 disabled:cursor-default"
              disabled={disabled}
              aria-busy={reply.isPending || undefined}
            >
              <Send size={12} aria-hidden="true" />
              {i18nT('apps.workflows.kas.reply_send')}
            </button>
          </span>
        </div>
      </div>
      {error ? (
        <p className="m-0 mt-1 text-[11.5px] text-danger" data-testid="kas-step-reply-error" role="alert">
          {error}
        </p>
      ) : !canReply ? (
        <p className="m-0 mt-1 text-[11px] text-muted" data-testid="kas-step-reply-hint">
          {stepRunning ? i18nT('apps.workflows.kas.composer_running_hint') : i18nT('apps.workflows.kas.composer_idle_hint')}
        </p>
      ) : null}
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
  stepLabel = null,
  model = null,
  effort = null,
  stepActions = null,
}: {
  workflowId: string
  nodeId: string | null
  /** The step instance's runtime path; passed to the transcript fetch and reply. */
  path?: string | null
  /** The step is running per the run's tree: keep polling. */
  stepRunning: boolean
  /** The step is parked for the person's next message: enable the composer. */
  canReply?: boolean
  /** The parent chat's title, for the breadcrumb; null when unknown. */
  chatTitle: string | null
  /** The step's agent from the run tree, until the transcript names one. */
  agent: string | null
  /** The step's own label from the run tree; the outgoing parent-note card's
   *  sender title. Falls back to the agent, then the node id. */
  stepLabel?: string | null
  /** The step's model / effort from the run tree, for the composer's read-only chip. */
  model?: string | null
  effort?: string | null
  /** Controls on this step (Mark done / Mark failed); drawn beside Send. */
  stepActions?: ReactNode
}) {
  const query = useKasStepTranscript(workflowId, nodeId, stepRunning, path)
  const transcript = query.data
  const running = stepRunning || transcript?.status === 'running'
  const agentName = transcript?.agent ?? agent
  // The parent chat's "Collapse all steps" preference: off = chips start open.
  const [collapseAll] = useState(() => loadChatConfig().collapseAllSteps)

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
            {foldReplies(transcript.entries).map((block, i) => {
              if (block.kind === 'prompt') return <UserPrompt key={block.entry.id} text={block.entry.text} />
              if (block.kind === 'parent')
                return (
                  <ParentMessageCard
                    key={block.entry.id}
                    entry={block.entry}
                    nodeId={nodeId ?? ''}
                    stepLabel={stepLabel}
                    agent={agentName}
                    chatTitle={chatTitle}
                  />
                )
              if (block.kind === 'reply')
                return <Reply key={block.reply.id} reply={block.reply} chip={block.chip} ts={block.ts} startOpen={!collapseAll} />
              return <WorkChip key={`work-${block.chip.entries[0]?.id ?? i}`} chip={block.chip} startOpen={!collapseAll} />
            })}
          </div>
        )}
      </>
    )
  }

  return (
    <section className="flex flex-col gap-2 min-w-0" aria-label={i18nT('apps.workflows.kas.step_chat')} data-testid="kas-step-chat">
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
      {nodeId && transcript ? (
        <>
          <StepComposer
            workflowId={workflowId}
            nodeId={nodeId}
            path={path}
            canReply={canReply}
            stepRunning={!!running}
            agent={agentName}
            model={transcript.model ?? model}
            effort={effort}
            actions={stepActions}
          />
          {/* The existing credits · elapsed · state line sits UNDER the composer. */}
          <Footer transcript={transcript} running={running && !canReply} waiting={canReply} />
        </>
      ) : stepActions ? (
        <div className="sticky bottom-0 z-[1] bg-card flex justify-end pt-2 pb-1 border-t border-border">{stepActions}</div>
      ) : null}
    </section>
  )
}
