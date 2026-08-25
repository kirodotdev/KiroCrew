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
import { memo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Workflow,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Save,
} from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { api } from '../../api/client'
import Modal from '../../components/Modal'
import { Btn, Input } from '../../components/ui'
import { useRunSnapshot } from '../../apps/workflows/useRunSnapshot'
import WorkflowSourceCode from '../../apps/workflows/WorkflowSourceCode'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, switchSlot } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
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

function workflowSlug(name: string): string {
  return name
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
}

function sourceDescription(source: string): string {
  const doubleQuoted = /["']description["']\s*:\s*"((?:\\.|[^"\\])*)"/.exec(
    source,
  )
  if (doubleQuoted) {
    try {
      return String(JSON.parse(`"${doubleQuoted[1]}"`))
    } catch {
      return doubleQuoted[1]
    }
  }
  const singleQuoted = /["']description["']\s*:\s*'((?:\\.|[^'\\])*)'/.exec(
    source,
  )
  return singleQuoted?.[1]?.replace(/\\'/g, "'").replace(/\\\\/g, '\\') || ''
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const activeSlot = useAppSelector((s) => s.chat.activeSlot)
  const run = useAppSelector((s) => s.chat.workflowRuns?.[runId])
  const {
    snapshot,
    loading: sourceLoading,
    error: sourceError,
  } = useRunSnapshot(runId, { enabled: run?.status !== 'running' })
  const status = snapshot?.status || run?.status
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveName, setSaveName] = useState('')
  const [saveSlug, setSaveSlug] = useState('')
  const [saveDescription, setSaveDescription] = useState('')
  const [savedSlug, setSavedSlug] = useState('')

  const name = sanitizeLlmOutput(
    (snapshot?.name || run?.name || parseLaunchLabel(message) || runId).slice(
      0,
      80,
    ),
  )
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

  const saveDefinition = useMutation({
    mutationFn: () =>
      api.promoteWorkflowRun(runId, {
        name: saveName.trim(),
        description: saveDescription.trim(),
        slug: saveSlug.trim(),
      }),
    onSuccess: async (result) => {
      setSavedSlug(result.definition.slug)
      setSaveOpen(false)
      await queryClient.invalidateQueries({
        queryKey: ['workflow-definitions'],
      })
    },
  })

  const beginSave = () => {
    saveDefinition.reset()
    setSaveName(name)
    setSaveSlug(workflowSlug(name))
    setSaveDescription(
      sanitizeLlmOutput(
        sourceDescription(snapshot?.source || '').slice(0, 240),
      ),
    )
    setSaveOpen(true)
  }

  // Row geometry -- the px-4 gutter and the --mc-content-width clamp -- belongs to
  // the HOST row wrapper, never to this card. ChatPage wraps every renderMessage
  // result, and the shared registries wrap this card through ctx.row. Re-applying
  // it here nested one clamp inside another and inset the card by a second full
  // gutter, so it sat 20px right of every sibling row and 40px narrower.
  return (
    <>
      <div className="pi-morph w-full rounded-md bg-accent/10 ring-1 ring-inset forced-colors:border ring-accent/20 px-3 py-2">
        <Btn
          onClick={open}
          title={i18nT(
            'pages.chat.workflowRunCard.open_in_the_workflows_panel',
          )}
          className="group w-full min-w-0 items-start border-0 bg-transparent p-0 text-left hover:bg-transparent"
        >
          <span className="shrink-0 mt-0.5">
            {status === 'running' && (
              <Loader2 className="lucide-inline text-accent animate-spin" />
            )}
            {status === 'finished' && (
              <CheckCircle2 className="lucide-inline text-ok" />
            )}
            {(status === 'failed' || status === 'cancelled') && (
              <AlertCircle className="lucide-inline text-danger" />
            )}
            {!status && <Workflow className="lucide-inline text-accent/70" />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Workflow className="lucide-inline text-accent/70 shrink-0" />
              <span className="truncate text-[13px] leading-5 font-medium text-text-strong">
                {name}
              </span>
              {status && (
                <span className="shrink-0 text-[10px] leading-4 px-1.5 py-0.5 rounded bg-accent/10 border border-accent/20 text-accent">
                  {status === 'running' && phase ? phase : status}
                </span>
              )}
            </div>
            {status === 'running' && lastLog && (
              <div className="text-[12px] leading-5 text-muted italic truncate mt-1">
                {lastLog}
              </div>
            )}
            {(status === 'failed' || status === 'cancelled') && errMsg && (
              <div className="text-[12px] leading-5 text-danger truncate mt-1">
                {errMsg}
              </div>
            )}
            <div className="text-[10px] leading-4 text-muted font-mono truncate mt-1">
              {runId} {i18nT('pages.chat.workflowRunCard.open_workflows_panel')}
            </div>
          </div>
          <PanelRightSolid
            size={14}
            className="text-muted shrink-0 mt-0.5 opacity-60 group-hover:opacity-100 transition-opacity"
          />
        </Btn>
        {status === 'finished' && (
          <div className="mt-2 pt-2 border-t border-accent/15 flex justify-end">
            {savedSlug ? (
              <Btn
                onClick={() => navigate('/capabilities?tab=workflows')}
                className="font-mono"
              >
                {i18nT('pages.chat.workflowRunCard.saved_as', {
                  command: `/workflow ${savedSlug}`,
                })}
              </Btn>
            ) : (
              <Btn onClick={beginSave} disabled={sourceLoading}>
                {sourceLoading ? (
                  <Loader2 className="lucide-inline animate-spin" />
                ) : (
                  <Save className="lucide-inline" />
                )}
                {i18nT('pages.chat.workflowRunCard.save_workflow')}
              </Btn>
            )}
          </div>
        )}
      </div>
      <Modal
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        title={i18nT('pages.chat.workflowRunCard.save_title')}
        maxWidth={760}
        footer={
          <>
            <Btn onClick={() => setSaveOpen(false)}>
              {i18nT('pages.chat.workflowRunCard.cancel')}
            </Btn>
            <Btn
              primary
              onClick={() => saveDefinition.mutate()}
              disabled={
                saveDefinition.isPending ||
                !snapshot?.source ||
                !saveName.trim() ||
                !saveSlug.trim()
              }
            >
              {saveDefinition.isPending ? (
                <Loader2 className="lucide-inline animate-spin" />
              ) : (
                <Save className="lucide-inline" />
              )}
              {i18nT('pages.overview.workflowLibrary.save_to_library')}
            </Btn>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-[13px] text-muted">
            {i18nT('pages.chat.workflowRunCard.save_help')}
          </p>
          <label className="block text-[12px] text-muted">
            <span className="block mb-1">
              {i18nT('pages.overview.workflowLibrary.name')}
            </span>
            <Input
              aria-label={i18nT('pages.overview.workflowLibrary.name')}
              value={saveName}
              onChange={(event) => setSaveName(event.target.value)}
              disabled={saveDefinition.isPending}
              className="w-full"
            />
          </label>
          <label className="block text-[12px] text-muted">
            <span className="block mb-1">
              {i18nT('pages.overview.workflowLibrary.slug')}
            </span>
            <Input
              aria-label={i18nT('pages.overview.workflowLibrary.slug')}
              value={saveSlug}
              onChange={(event) => setSaveSlug(event.target.value)}
              disabled={saveDefinition.isPending}
              className="w-full font-mono"
            />
          </label>
          <label className="block text-[12px] text-muted">
            <span className="block mb-1">
              {i18nT('pages.overview.workflowLibrary.workflow_description')}
            </span>
            <Input
              aria-label={i18nT(
                'pages.overview.workflowLibrary.workflow_description',
              )}
              value={saveDescription}
              onChange={(event) => setSaveDescription(event.target.value)}
              disabled={saveDefinition.isPending}
              className="w-full"
            />
          </label>
          {snapshot?.source ? (
            <div className="text-[12px] text-muted">
              <span className="block mb-1">
                {i18nT('pages.overview.workflowLibrary.source')}
              </span>
              <WorkflowSourceCode
                source={snapshot.source}
                ariaLabel={i18nT('pages.overview.workflowLibrary.source')}
                compact
              />
            </div>
          ) : null}
          {sourceError ? (
            <p className="text-[12px] text-danger">
              {i18nT('pages.chat.workflowRunCard.source_unavailable')}
            </p>
          ) : null}
          {saveDefinition.error ? (
            <p className="text-[12px] text-danger">
              {i18nT('pages.overview.workflowLibrary.request_failed')}
            </p>
          ) : null}
        </div>
      </Modal>
    </>
  )
})

export default WorkflowRunCard
