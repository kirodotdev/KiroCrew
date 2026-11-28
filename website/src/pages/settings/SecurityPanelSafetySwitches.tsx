/**
 * Settings -> Security -> Safety switches: the owner's toggles for the two MCP
 * safety keys, `mcp.protect_kiro_permission_rules` and `mcp.honour_auto_approve`
 * (`SecurityPanel.tsx` mounts `SafetySwitchesSection`).
 *
 * A VIEW over `/api/security/safety-switches`, which is owner-only on both verbs
 * and the only dashboard writer of the two keys (the Advanced page shows them
 * read-only and links here). A non-owner's read is refused, so their rows are
 * drawn disabled from the config every dashboard session already reads, labelled
 * as saved values, with a neutral owner-only line. A failed read draws no switch: a default shown for a
 * state we could not read may be the reassuring wrong one.
 */
import type { ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Info, Lock } from 'lucide-react'
import { retryPolicy } from '../../api/queryClient'
import { Btn, SkeletonToggleRow } from '../../components/ui'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api, type SafetySwitchKey, type SafetySwitchesState } from '../../api/client'
import { apiErrorCode, FILE_PENDING_REVIEW_CODE, filePendingReviewMessage } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import { isOwnerGateRefusal } from './SecurityPanelRedaction'

const PROTECT: SafetySwitchKey = 'mcp.protect_kiro_permission_rules'
const HONOUR: SafetySwitchKey = 'mcp.honour_auto_approve'

/** The slice of `/api/config/kirocrew` a non-owner's disabled rows read. */
interface McpCfgShape { mcp?: { protect_kiro_permission_rules?: unknown; honour_auto_approve?: unknown } }

/** One switch as its row draws it. Both rows read ON = safer: the ask toggle is
 *  the inverse of `mcp.honour_auto_approve`; the config key and the API are unchanged.
 *  `checked` is the state IN FORCE; `saved` is what config.json holds. */
interface Shown { checked: boolean; saved: boolean }

function shownFor(key: SafetySwitchKey, enabled: boolean, inForce: boolean): Shown {
  return key === HONOUR ? { checked: !inForce, saved: !enabled } : { checked: inForce, saved: enabled }
}

function SafetySwitchesCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError, error, isFetching, refetch } = useQuery<SafetySwitchesState>({
    queryKey: ['safety-switches'],
    queryFn: api.safetySwitches,
    // An owner-gate refusal is a fact about the viewer: a retry would only
    // write another audited refusal.
    retry: (count, err) => !isOwnerGateRefusal(err) && retryPolicy(count, err),
  })
  const ownerGateRefused = isError && isOwnerGateRefusal(error)
  // Shared cache entry with the Security rail, so a non-owner adds no request.
  const cfgQuery = useQuery<McpCfgShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: api.kirocrewConfig,
    enabled: ownerGateRefused,
  })
  const set = useMutation({
    mutationFn: ({ key, enabled }: { key: SafetySwitchKey; enabled: boolean }) => api.setSafetySwitch(key, enabled),
    onSuccess: state => qc.setQueryData(['safety-switches'], state),
    // Re-read on every settlement: a PUT that landed but lost its response
    // must not leave the old position on screen. The config cache holds the
    // same keys, so it is refreshed too.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['safety-switches'] })
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  // `isError`, not `data === undefined`: react-query keeps the last good data
  // across a rejected refetch. The same holds for the non-owner's config read.
  const view = isError ? undefined : data
  const cfgFailed = ownerGateRefused && cfgQuery.isError
  const rows = new Map<SafetySwitchKey, Shown>()
  if (view) {
    for (const s of view.switches) rows.set(s.key, shownFor(s.key, s.enabled, s.in_force))
  } else if (ownerGateRefused && !cfgFailed && cfgQuery.data?.mcp) {
    // The config read holds SAVED values only; the state in force is owner-only.
    // So the row draws the saved value and its line says "Saved", never "in force".
    const { protect_kiro_permission_rules: p, honour_auto_approve: h } = cfgQuery.data.mcp
    if (typeof p === 'boolean') rows.set(PROTECT, { checked: p, saved: p })
    if (typeof h === 'boolean') rows.set(HONOUR, { checked: !h, saved: !h })
  }
  const readOnly = !view
  const busy = set.isPending
  const failedKey = set.isError ? set.variables?.key : undefined
  const failedMessage = apiErrorCode(set.error) === FILE_PENDING_REVIEW_CODE
    ? filePendingReviewMessage()
    : i18nT('pages.settings.securityPanel.safetySwitches.write_failed')

  // What every row shows under its toggle. The toggles themselves are written
  // out per row, so the settings registry reads their literal labels.
  const rowExtras = (key: SafetySwitchKey, shown: Shown, note: ReactNode = null) => (
    <>
      {readOnly && (
        // A disabled row is dimmed; this line, outside it, keeps on and off apart.
        // Only a non-owner sees it, and what they can read is the saved value.
        <div data-testid={`${key}-state`} className="text-[11px] font-semibold text-text">
          {shown.saved
            ? i18nT('pages.settings.securityPanel.safetySwitches.saved_on')
            : i18nT('pages.settings.securityPanel.safetySwitches.saved_off')}
        </div>
      )}
      {shown.checked !== shown.saved && (
        <div className="flex items-start gap-2 text-[11px] text-warn leading-relaxed">
          <AlertTriangle size={13} className="shrink-0 mt-px" />
          <span>
            {shown.checked
              ? i18nT('pages.settings.securityPanel.safetySwitches.in_force_on')
              : i18nT('pages.settings.securityPanel.safetySwitches.in_force_off')}
          </span>
        </div>
      )}
      {note}
      {failedKey === key && (
        <ErrorNotice
          variant="inline"
          message={failedMessage}
          // Inline notices sit on one line; a long refusal keeps the icon beside
          // its message and wraps only the hand-off link below it.
          className="flex-wrap"
          messageClassName="flex-1 basis-[calc(100%-2rem)]"
          askAgent
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="safety-switches-write-failed"
        />
      )}
    </>
  )
  const send = (key: SafetySwitchKey, on: boolean) => set.mutate({ key, enabled: key === HONOUR ? !on : on })
  const protect = rows.get(PROTECT)
  const honour = rows.get(HONOUR)

  return (
    <SettingsCard>
      {isLoading ? (
        <div data-testid="safety-switches-loading" aria-busy="true">
          <SkeletonToggleRow />
          <SkeletonToggleRow />
        </div>
      ) : view || ownerGateRefused ? (
        <div data-testid="safety-switches-rows" className="flex flex-col -my-3 divide-y divide-border [&>*]:py-3">
          {ownerGateRefused && (
            // A permission lock, not a failure: a neutral line with nothing to retry.
            <div data-testid="safety-switches-owner-only" className="flex items-start gap-2 text-[12px] text-muted leading-relaxed">
              <Lock size={13} className="shrink-0 mt-px" />
              <span>{i18nT('pages.settings.securityPanel.safetySwitches.owner_only')}</span>
            </div>
          )}
          {cfgFailed && (
            <ErrorNotice
              message={i18nT('pages.settings.securityPanel.safetySwitches.read_failed')}
              askAgent
              askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
              testId="safety-switches-config-read-failed"
              footer={
                <Btn onClick={() => { void cfgQuery.refetch() }} disabled={cfgQuery.isFetching}>
                  {i18nT('pages.settings.securityPanel.credential_redaction_retry')}
                </Btn>
              }
            />
          )}
          {protect && (
            <div data-testid="safety-switch-protect" className="flex flex-col gap-1">
              <SettingsToggle
                label={i18nT('pages.settings.securityPanel.safetySwitches.protect_label')}
                description={i18nT('pages.settings.securityPanel.safetySwitches.protect_desc')}
                configKey="mcp.protect_kiro_permission_rules"
                checked={protect.checked}
                disabled={readOnly || busy}
                onChange={on => send(PROTECT, on)}
              />
              {rowExtras(PROTECT, protect, (
                // The fence covers the agent's file-edit tools only, so the
                // terminal caveat gets a line of its own.
                <div data-testid="safety-switch-terminal-note" className="flex items-start gap-2 text-[11px] text-muted leading-relaxed">
                  <Info size={12} className="shrink-0 mt-px" />
                  <span>{i18nT('pages.settings.securityPanel.safetySwitches.protect_terminal_note')}</span>
                </div>
              ))}
            </div>
          )}
          {honour && (
            <div data-testid="safety-switch-honour" className="flex flex-col gap-1">
              <SettingsToggle
                label={i18nT('pages.settings.securityPanel.safetySwitches.ask_label')}
                description={i18nT('pages.settings.securityPanel.safetySwitches.ask_desc')}
                configKey="mcp.honour_auto_approve"
                checked={honour.checked}
                disabled={readOnly || busy}
                onChange={on => send(HONOUR, on)}
              />
              {rowExtras(HONOUR, honour, (
                // The key is read when the agent's config is written at start
                // (governance.strip_ungoverned_auto_approve), and no config
                // subscriber rewrites it on a save, so a change waits for a restart.
                <div data-testid="safety-switch-restart-note" className="flex items-start gap-2 text-[11px] text-muted leading-relaxed">
                  <Info size={12} className="shrink-0 mt-px" />
                  <span>{i18nT('pages.settings.securityPanel.safetySwitches.restart_note')}</span>
                </div>
              ))}
            </div>
          )}
          {view?.degraded && (
            <ErrorNotice
              impact="partial"
              message={i18nT('pages.settings.securityPanel.safetySwitches.degraded')}
              askAgent
              askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
              testId="safety-switches-degraded"
              footer={
                <Btn onClick={() => { void refetch() }} disabled={isFetching} data-testid="safety-switches-degraded-retry">
                  {i18nT('pages.settings.securityPanel.credential_redaction_retry')}
                </Btn>
              }
            />
          )}
        </div>
      ) : isError ? (
        <ErrorNotice
          message={i18nT('pages.settings.securityPanel.safetySwitches.read_failed')}
          askAgent
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="safety-switches-read-failed"
          footer={
            <Btn onClick={() => { void refetch() }} disabled={isFetching} data-testid="safety-switches-retry">
              {i18nT('pages.settings.securityPanel.credential_redaction_retry')}
            </Btn>
          }
        />
      ) : null}
    </SettingsCard>
  )
}

/** Settings -> Security -> Safety switches. */
export function SafetySwitchesSection() {
  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.safetySwitches.section_title')} description={i18nT('pages.settings.securityPanel.safetySwitches.intro')}>
      <SafetySwitchesCard />
    </SettingsSection>
  )
}
