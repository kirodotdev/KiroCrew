/**
 * A compact transcript line for KAS's workflow tools OTHER than `run_workflow`
 * (gap 26): validate / inspect / update / save / send-message. The Kiro IDE
 * gives each a bespoke row with a verdict badge and an "Open run" link; Crew
 * otherwise draws the generic tool pill. The launch tool (`run_workflow`) is
 * handled by `KasInlineRunCard` and must win over this.
 *
 * Detection is best-effort from the tool title in the message content plus the
 * tool's persisted output — the gateway stamps no workflow-tool discriminator
 * on the row. A row this cannot
 * classify falls through to the generic line, so a miss never breaks a row.
 */
import type { ReactNode } from 'react'
import { CheckCircle2, FileCog, FileSearch, Workflow, XCircle } from 'lucide-react'

import type { ChatMessage } from '../../../types'
import { useAppDispatch } from '../../../store'
import { openActivityToTab } from '../../../store/chatSlice'
import { useSlotId } from '../../../providers/SlotContext'
import { i18nT } from '../../../i18n/t'
import { setKasSelection } from './kasSelection'
import { servedByMcp } from './kasModel'

export type KasWorkflowToolKind = 'validate' | 'inspect' | 'update' | 'save' | 'report'

/** Title/name tokens KAS uses for each workflow tool, matched case-insensitively
 *  with either spaces or underscores. Ordered: the first hit wins. */
const KIND_TOKENS: { kind: KasWorkflowToolKind; tokens: RegExp }[] = [
  { kind: 'validate', tokens: /validate[ _]workflow/i },
  { kind: 'save', tokens: /save[ _]workflow(_definition| definition)?/i },
  { kind: 'update', tokens: /(update|revise)[ _]workflow( plan)?/i },
  { kind: 'inspect', tokens: /(inspect|check)[ _]workflow/i },
  { kind: 'report', tokens: /report[ _]to[ _]workflow|workflow[ _]send[ _-]?message/i },
]

const WF_ID_RE = /"workflowId"\s*:\s*"(wf_[A-Za-z0-9_]+)"/

/** Which KAS workflow tool this tool row is, or null (not one, or run_workflow).
 *
 *  A row the gateway stamped with a trusted `meta.mcp_server` was served by an
 *  MCP server (e.g. a GitHub connection's `run_workflow` / "Check workflow
 *  status"), never by Kiro Agent's own workflow engine, so its title is not
 *  read at all: that is what keeps a lookalike name from drawing this line. */
export function kasWorkflowToolKind(message: ChatMessage): KasWorkflowToolKind | null {
  if (message.role !== 'tool') return null
  if (servedByMcp(message)) return null
  const title = typeof message.content === 'string' ? message.content : ''
  for (const { kind, tokens } of KIND_TOKENS) {
    if (tokens.test(title)) return kind
  }
  return null
}

/** The run a workflow tool acted on, from its persisted input/output JSON. */
function workflowIdOf(message: ChatMessage): string | null {
  for (const key of ['output', 'input'] as const) {
    const raw = message.meta?.[key]
    if (typeof raw === 'string') {
      const m = WF_ID_RE.exec(raw)
      if (m) return m[1]
    }
  }
  return null
}

/** For `validate`: whether the output says the workflow is valid, or null. */
function validity(message: ChatMessage): boolean | null {
  const raw = message.meta?.output
  if (typeof raw !== 'string' || !raw.trimStart().startsWith('{')) return null
  try {
    const body = JSON.parse(raw) as { valid?: unknown; errors?: unknown }
    if (typeof body.valid === 'boolean') return body.valid
    if (Array.isArray(body.errors)) return body.errors.length === 0
    return null
  } catch {
    return null
  }
}

function kindLabel(kind: KasWorkflowToolKind): string {
  switch (kind) {
    case 'validate':
      return i18nT('apps.workflows.kas.tool_validate')
    case 'inspect':
      return i18nT('apps.workflows.kas.tool_inspect')
    case 'update':
      return i18nT('apps.workflows.kas.tool_update')
    case 'save':
      return i18nT('apps.workflows.kas.tool_save')
    case 'report':
      return i18nT('apps.workflows.kas.tool_report')
  }
}

function KindIcon({ kind }: { kind: KasWorkflowToolKind }) {
  if (kind === 'inspect') return <FileSearch size={14} className="text-muted shrink-0" aria-hidden="true" />
  if (kind === 'update' || kind === 'save') return <FileCog size={14} className="text-muted shrink-0" aria-hidden="true" />
  return <Workflow size={14} className="text-muted shrink-0" aria-hidden="true" />
}

export function KasWorkflowToolLine({
  message,
  kind,
  fallback,
}: {
  message: ChatMessage
  kind: KasWorkflowToolKind
  /** The generic tool line, shown when there is nothing richer to say. */
  fallback: ReactNode
}) {
  const dispatch = useAppDispatch()
  const slotId = useSlotId()
  const workflowId = workflowIdOf(message)
  const valid = kind === 'validate' ? validity(message) : null
  // Nothing to add over the generic pill: no run to open and no verdict.
  if (!workflowId && valid === null) return <>{fallback}</>

  const openRun = () => {
    if (!workflowId) return
    setKasSelection(slotId, { workflowId })
    dispatch(openActivityToTab('workflows'))
  }

  return (
    <div
      className="max-w-[560px] rounded-lg border border-border bg-bg-elevated text-text px-3 py-2 flex items-center gap-2 text-[13px]"
      data-testid="kas-workflow-tool-line"
      data-kind={kind}
    >
      <KindIcon kind={kind} />
      <span className="truncate min-w-0">{kindLabel(kind)}</span>
      {valid !== null ? (
        valid ? (
          <span className="inline-flex items-center gap-1 text-[11.5px] text-ok" data-testid="kas-tool-valid">
            <CheckCircle2 size={12} aria-hidden="true" /> {i18nT('apps.workflows.kas.tool_valid')}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[11.5px] text-danger" data-testid="kas-tool-invalid">
            <XCircle size={12} aria-hidden="true" /> {i18nT('apps.workflows.kas.tool_invalid')}
          </span>
        )
      ) : null}
      {workflowId ? (
        <button
          type="button"
          className="ml-auto shrink-0 text-accent bg-transparent border-none p-0 cursor-pointer text-[12px] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          onClick={openRun}
          data-testid="kas-tool-open-run"
        >
          {i18nT('apps.workflows.kas.tool_open_run')}
        </button>
      ) : null}
    </div>
  )
}
