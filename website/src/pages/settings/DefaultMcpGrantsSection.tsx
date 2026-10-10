/**
 * Settings > Developer > **Agent MCP servers**: add the two opt-in Kiro Crew MCP
 * sets to the DEFAULT agent, or remove them -- `kirocrew-dashboard` (folders, tags,
 * session control) and `kirocrew-debug` (gateway debug reads).
 *
 * The default agent's spec is the only state: each row shows whether the spec on
 * disk mounts that server, and the two buttons edit that spec through
 * `POST /api/agent/default-mcp-grants`. Both actions are idempotent, act on the
 * pair together, and report either success or the step that failed. Remove also
 * takes out an entry added by hand, and its confirm says so.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import type { DefaultMcpGrantResult, DefaultMcpGrants } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { i18nT } from '../../i18n/t'

const QK = ['defaultMcpGrants']
const DASHBOARD = 'kirocrew-dashboard'
const DEBUG = 'kirocrew-debug'

type Action = 'add' | 'remove'
type Outcome = { kind: 'done'; action: Action } | { kind: 'failed'; action: Action; step: string | null; refusal: string }

/** The failed step's label, from the POST's own `failed_step` field. */
function stepLabel(step: string | null): string {
  if (step === 'write_spec') return i18nT('pages.settings.defaultMcpGrantsSection.step_write_spec')
  if (step === 'rebuild') return i18nT('pages.settings.defaultMcpGrantsSection.step_rebuild')
  if (step === 'verify') return i18nT('pages.settings.defaultMcpGrantsSection.step_verify')
  return ''
}

function failedStep(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  try {
    const body = JSON.parse(error.body) as Partial<DefaultMcpGrantResult>
    return typeof body.failed_step === 'string' ? body.failed_step : null
  } catch {
    return null
  }
}

/** The gateway's own message when it answered without running a step (a 403
 * owner_only, a 400 action_invalid); '' when the request never got an answer. */
function refusalText(error: unknown): string {
  return error instanceof ApiError ? error.message : ''
}

function failureMessage(action: Action, step: string | null, refusal: string): string {
  const label = stepLabel(step)
  if (!label) return refusal || i18nT('pages.settings.defaultMcpGrantsSection.failed_request')
  return action === 'add'
    ? i18nT('pages.settings.defaultMcpGrantsSection.failed_add', { step: label })
    : i18nT('pages.settings.defaultMcpGrantsSection.failed_remove', { step: label })
}

function Row({ label, desc, mounted, testId }: { label: string; desc: string; mounted: boolean | undefined; testId: string }) {
  return (
    <div className="flex flex-col gap-1 py-1.5 md:flex-row md:items-start md:justify-between md:gap-4">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-text">{label}</div>
        <div className="text-[12px] text-muted mt-0.5">{desc}</div>
      </div>
      <div className="text-[12px] text-muted md:shrink-0" data-testid={testId}>
        {mounted === undefined
          ? ''
          : mounted
            ? i18nT('pages.settings.defaultMcpGrantsSection.state_mounted')
            : i18nT('pages.settings.defaultMcpGrantsSection.state_not_mounted')}
      </div>
    </div>
  )
}

export function DefaultMcpGrantsSection() {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const q = useQuery<DefaultMcpGrants>({ queryKey: QK, queryFn: () => api.defaultMcpGrants() })
  const mut = useMutation({
    mutationFn: (action: Action) => api.setDefaultMcpGrants(action),
    onMutate: () => setOutcome(null),
    onSuccess: (data, action) => {
      setOutcome({ kind: 'done', action })
      if (data?.servers) qc.setQueryData<DefaultMcpGrants>(QK, { servers: data.servers, session_control: data.session_control, unreached_backends: data.unreached_backends })
    },
    onError: (error: unknown, action) => setOutcome({ kind: 'failed', action, step: failedStep(error), refusal: refusalText(error) }),
    onSettled: () => { void qc.invalidateQueries({ queryKey: QK }) },
  })

  const mountedOf = (name: string) => q.data?.servers?.find(s => s.name === name)?.mounted
  const busy = !q.isSuccess || mut.isPending
  const sessionControl = q.data?.session_control !== false
  const unreached = q.data?.unreached_backends ?? []

  const remove = async () => {
    const ok = await confirm({
      title: i18nT('pages.settings.defaultMcpGrantsSection.remove_confirm_title'),
      body: i18nT('pages.settings.defaultMcpGrantsSection.remove_confirm_body'),
      confirmLabel: i18nT('pages.settings.defaultMcpGrantsSection.remove_button'),
    })
    if (ok) mut.mutate('remove')
  }

  return (
    <SettingsSection title={i18nT('pages.settings.defaultMcpGrantsSection.title')}>
      {q.isError && (
        <ErrorNotice
          message={i18nT('pages.settings.defaultMcpGrantsSection.failed_to_load')}
          askAgent
          className="mb-2"
          testId="default-mcp-grants-error"
          footer={
            <Btn onClick={() => { void q.refetch() }} disabled={q.isFetching}>
              {i18nT('pages.settings.defaultMcpGrantsSection.retry')}
            </Btn>
          }
        />
      )}
      <SettingsCard>
        <div className="pb-2 text-[13px] text-muted">{i18nT('pages.settings.defaultMcpGrantsSection.intro')}</div>
        <Row
          label={i18nT('pages.settings.defaultMcpGrantsSection.dashboard_label')}
          desc={i18nT('pages.settings.defaultMcpGrantsSection.dashboard_desc')}
          mounted={mountedOf(DASHBOARD)}
          testId="default-mcp-state-kirocrew-dashboard"
        />
        {!sessionControl && (
          <div className="pb-2 text-[13px] text-muted" data-testid="default-mcp-session-control-note">
            {i18nT('pages.settings.defaultMcpGrantsSection.session_control_off')}
          </div>
        )}
        <Row
          label={i18nT('pages.settings.defaultMcpGrantsSection.debug_label')}
          desc={i18nT('pages.settings.defaultMcpGrantsSection.debug_desc')}
          mounted={mountedOf(DEBUG)}
          testId="default-mcp-state-kirocrew-debug"
        />
        {unreached.length > 0 && (
          <div className="pb-2 text-[13px] text-muted" data-testid="default-mcp-unreached-note">
            {i18nT('pages.settings.defaultMcpGrantsSection.backends_unreached', { backends: unreached.join(', ') })}
          </div>
        )}
        <div className="flex flex-wrap gap-2 pt-2">
          <Btn onClick={() => mut.mutate('add')} disabled={busy}>
            {i18nT('pages.settings.defaultMcpGrantsSection.add_button')}
          </Btn>
          <Btn onClick={() => { void remove() }} disabled={busy}>
            {i18nT('pages.settings.defaultMcpGrantsSection.remove_button')}
          </Btn>
        </div>
        {outcome?.kind === 'done' && (
          <div className="pt-2 text-[13px] text-muted" role="status" data-testid="default-mcp-grants-done">
            {outcome.action === 'add'
              ? i18nT('pages.settings.defaultMcpGrantsSection.add_done')
              : i18nT('pages.settings.defaultMcpGrantsSection.remove_done')}
          </div>
        )}
        {outcome?.kind === 'failed' && (
          <ErrorNotice
            message={failureMessage(outcome.action, outcome.step, outcome.refusal)}
            onDismiss={() => setOutcome(null)}
            askAgent
            className="mt-2"
            testId="default-mcp-grants-save-error"
          />
        )}
      </SettingsCard>
      {confirmDialog}
    </SettingsSection>
  )
}
