/**
 * Settings > Developer > **Agent MCP servers**: mount the two opt-in Kiro Crew MCP
 * sets on the DEFAULT agent, or take them off -- `kirocrew-dashboard` (folders, tags,
 * session control) and `kirocrew-debug` (gateway debug reads).
 *
 * The state is the default agent's spec, read with `GET /api/agents/detail/kirocrew`:
 * a set is on when the spec holds its entry and its `@server` ref, and it came from
 * the mount-only record when the entry carries that record's stamp. Add records each
 * set that is off (`POST .../mcp-additions`, where the gateway builds the entry);
 * Remove deletes both records (`DELETE .../mcp-additions/{server}`). The rebuild each
 * call runs is what changes the spec. An entry the user wrote by hand carries no
 * stamp, so this section shows it and leaves it alone.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import { ApiError, apiErrorCode } from '../../api/apiError'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { i18nT } from '../../i18n/t'

const QK = ['defaultAgentMcpSets']
const SETS = ['kirocrew-dashboard', 'kirocrew-debug'] as const
type SetName = (typeof SETS)[number]
/** The mount-only record's stamp on a spec entry (`template_mcp_additions.MERGED_MARK`). */
const STAMP = 'x-kirocrew-card-addition'

type Spec = { mcpServers?: Record<string, unknown>; tools?: unknown[] }
type KirocrewConfigShape = { agent?: { session_control?: boolean } }
type SetState = 'recorded' | 'by_hand' | 'off'
type Action = 'add' | 'remove'
type Outcome = { kind: 'done'; action: Action } | { kind: 'failed'; action: Action; reason: string }

function stateOf(spec: Spec | undefined, name: SetName): SetState | undefined {
  if (!spec) return undefined
  const entry = spec.mcpServers?.[name]
  if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) return 'off'
  if (!Array.isArray(spec.tools) || !spec.tools.includes(`@${name}`)) return 'off'
  return (entry as Record<string, unknown>)[STAMP] === true ? 'recorded' : 'by_hand'
}

/** Add records each set that is off; Remove deletes both records. A refusal that
 * says the record already matches the action counts as done. */
async function run(action: Action, spec: Spec | undefined): Promise<void> {
  for (const name of SETS) {
    try {
      if (action === 'add') {
        if (stateOf(spec, name) === 'off') await api.defaultMcpSetAdd(name)
      } else {
        await api.defaultMcpSetRemove(name)
      }
    } catch (error) {
      const code = apiErrorCode(error)
      if (action === 'add' ? code !== 'already_added' : code !== 'not_found') throw error
    }
  }
}

function stateLabel(state: SetState | undefined): string {
  if (state === 'recorded') return i18nT('pages.settings.defaultMcpGrantsSection.state_mounted')
  if (state === 'by_hand') return i18nT('pages.settings.defaultMcpGrantsSection.state_by_hand')
  if (state === 'off') return i18nT('pages.settings.defaultMcpGrantsSection.state_not_mounted')
  return ''
}

function Row({ label, desc, state, testId }: { label: string; desc: string; state: SetState | undefined; testId: string }) {
  return (
    <div className="flex flex-col gap-1 py-1.5 md:flex-row md:items-start md:justify-between md:gap-4">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-text">{label}</div>
        <div className="text-[12px] text-muted mt-0.5">{desc}</div>
      </div>
      <div className="text-[12px] text-muted md:shrink-0 md:max-w-[40%]" data-testid={testId}>
        {stateLabel(state)}
      </div>
    </div>
  )
}

/** `boxed` draws the card as one bordered box, a hairline between its rows
 *  (`SettingsCard boxed`), for a page that boxes its other cards too. */
export function DefaultMcpGrantsSection({ boxed }: { boxed?: boolean } = {}) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  // No server event refreshes this read, so it goes stale at once instead of on
  // the shared Infinity default: coming back to the tab or the section refetches
  // the spec, which picks up a change another tab or a change card made.
  const q = useQuery<Spec>({
    queryKey: QK,
    queryFn: () => api.agentDetail('kirocrew') as Promise<Spec>,
    staleTime: 0,
  })
  const cfgQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig() as Promise<KirocrewConfigShape>,
  })
  const mut = useMutation({
    mutationFn: (action: Action) => run(action, q.data),
    onMutate: () => setOutcome(null),
    onSuccess: (_data, action) => setOutcome({ kind: 'done', action }),
    onError: (error: unknown, action) =>
      setOutcome({
        kind: 'failed',
        action,
        reason: error instanceof ApiError && error.message
          ? error.message
          : i18nT('pages.settings.defaultMcpGrantsSection.failed_request'),
      }),
    onSettled: () => { void qc.invalidateQueries({ queryKey: QK }) },
  })

  const states = SETS.map(name => stateOf(q.data, name))
  const busy = !q.isSuccess || mut.isPending
  // A config not read yet (loading or failed) is unknown, never on.
  const sessionControl = cfgQ.data === undefined
    ? 'unknown'
    : cfgQ.data.agent?.session_control === false ? 'off' : 'on'

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
      <SettingsCard boxed={boxed}>
        <div className="pb-2 text-[13px] text-muted">{i18nT('pages.settings.defaultMcpGrantsSection.intro')}</div>
        <Row
          label={i18nT('pages.settings.defaultMcpGrantsSection.dashboard_label')}
          desc={i18nT('pages.settings.defaultMcpGrantsSection.dashboard_desc')}
          state={states[0]}
          testId="default-mcp-state-kirocrew-dashboard"
        />
        {sessionControl === 'off' && (
          <div className="pb-2 text-[13px] text-muted" data-testid="default-mcp-session-control-note">
            {i18nT('pages.settings.defaultMcpGrantsSection.session_control_off')}
          </div>
        )}
        {cfgQ.isError && (
          <ErrorNotice
            message={i18nT('pages.settings.defaultMcpGrantsSection.failed_to_load_session_control')}
            askAgent
            className="mb-2"
            testId="default-mcp-config-error"
            footer={
              <Btn onClick={() => { void cfgQ.refetch() }} disabled={cfgQ.isFetching} data-testid="default-mcp-config-retry">
                {i18nT('pages.settings.defaultMcpGrantsSection.retry')}
              </Btn>
            }
          />
        )}
        <Row
          label={i18nT('pages.settings.defaultMcpGrantsSection.debug_label')}
          desc={i18nT('pages.settings.defaultMcpGrantsSection.debug_desc')}
          state={states[1]}
          testId="default-mcp-state-kirocrew-debug"
        />
        <div className="flex flex-wrap gap-2 pt-2">
          <Btn onClick={() => mut.mutate('add')} disabled={busy || !states.includes('off')}>
            {i18nT('pages.settings.defaultMcpGrantsSection.add_button')}
          </Btn>
          <Btn onClick={() => { void remove() }} disabled={busy || !states.includes('recorded')}>
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
            message={outcome.action === 'add'
              ? i18nT('pages.settings.defaultMcpGrantsSection.failed_add', { reason: outcome.reason })
              : i18nT('pages.settings.defaultMcpGrantsSection.failed_remove', { reason: outcome.reason })}
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
