/**
 * Settings -> Security -> Redaction: the owner's credential-redaction switch, the
 * per-workspace hosts allowed long queries, and the section that stacks the two
 * (`SecurityPanel.tsx` re-exports `RedactionSection` and
 * `RedactionAllowedHostsCard`).
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { retryPolicy } from '../../api/queryClient'
import { STALE_OWNER_SESSION_CODE } from '../../api/staleOwnerSignal'
import { purgeDocumentBodiesForRedactionChange } from '../../hooks/usePanelTabs'
import { Btn, SkeletonToggleRow } from '../../components/ui'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api, ApiError, type CredentialRedactionEngine, type CredentialRedactionState } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'

/* ── Credential redaction switch ─────────────────────────────────────────────
 *
 * A VIEW over the switch the `/api/security/credential-redaction` endpoints own.
 * ON by default: every output surface runs the credential scrubber over what the
 * agent produced. The owner turns it OFF when the scrubber is swallowing a value
 * they legitimately need to read back (a one-time approval link's `?token=`, a
 * blob they generated on purpose). Exfiltration-URL redaction and every
 * request-blocking gate stay on regardless; the card says so, because a toggle
 * labelled "redaction" that silently left half of it on would misdescribe what
 * the owner just did.
 *
 * A FAILED READ renders NO switch. Rendering the default (ON) for a state we
 * could not read would show a reassuring position that may be wrong, and the
 * reassuring direction is the dangerous one.
 */
/** The owner gate refusing the viewer: a 403 that is NOT the token-auth
 *  middleware's own re-auth denial (`X-Auth-Required`, a mid-session cookie
 *  lapse the transport refreshes silently -- that one must stay a retryable
 *  read), or the backend's `stale_session_reauth` code (a session that predates
 *  the owner claim). Neither is a transient, so neither gets a retry or a Retry
 *  button; `FilePathMenu` draws the same 403-vs-authRequired line. */
function isOwnerGateRefusal(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (error.status === 403 && !error.authRequired) return true
  try {
    const code = (JSON.parse(error.body || '{}') as { code?: unknown }).code
    return code === STALE_OWNER_SESSION_CODE
  } catch { return false }
}

function useCredentialRedactionRead() {
  return useQuery<CredentialRedactionState>({
    queryKey: ['credential-redaction'],
    queryFn: api.credentialRedaction,
    // An owner-gated refusal is a fact about the viewer, not a transient:
    // retrying would only write a second audited refusal for the same open
    // (see `isOwnerGateRefusal` for what counts; a re-auth 403 does not).
    retry: (count, error) => !isOwnerGateRefusal(error) && retryPolicy(count, error),
  })
}

function CredentialRedactionCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError, error, isFetching, refetch } = useCredentialRedactionRead()
  const set = useMutation({
    mutationFn: (enabled: boolean) => api.setCredentialRedaction(enabled),
    onSuccess: state => {
      qc.setQueryData(['credential-redaction'], state)
      // DROP every file body THIS document holds (react-query bodies and open
      // tab bodies -- see `purgeDocumentBodiesForRedactionChange`) on a write
      // the server ACCEPTED. A refused write (403 stale session, 503 audit
      // unavailable) moved nothing, so nothing is dropped for it. Other open
      // dashboard documents purge on the `credential_redaction_changed` push.
      purgeDocumentBodiesForRedactionChange(qc)
    },
    // Re-read on EVERY settlement, failure included: a PUT whose write landed
    // but whose response was lost would otherwise leave the card showing the
    // position from before the click while the switch is already in force.
    onSettled: () => void qc.invalidateQueries({ queryKey: ['credential-redaction'] }),
  })
  // Same rule as the consent card: react-query RETAINS the last good `data`
  // across a rejected refetch, so `isError` -- not `data === undefined` -- is
  // the whole test for "unreadable".
  const view = isError ? undefined : data
  const ownerGateRefused = isError && isOwnerGateRefusal(error)
  const busy = isLoading || set.isPending

  return (
    <SettingsCard>
      {/* No card title: the section heading above already reads "Credential
          redaction", and a third identical label stacked inside it read as
          noise to the blind UX reader. The toggle label names the effect. */}
      {/* The one task this card exists for is the read-back recipe, so it is a
          numbered list the reader can follow, not a clause in a paragraph. */}
      <div className="text-[12px] text-muted mb-2 leading-relaxed">
        <p className="m-0">{i18nT('pages.settings.securityPanel.credential_redaction_desc_intro')}</p>
        {/* The recipe names "the switch below"; a viewer the owner gate refuses
            has no switch, so the steps are not shown to them. */}
        {!ownerGateRefused && (
          <ol className="my-1.5 pl-5 list-decimal space-y-0.5">
            <li>{i18nT('pages.settings.securityPanel.credential_redaction_step_write')}</li>
            <li>{i18nT('pages.settings.securityPanel.credential_redaction_step_off')}</li>
            <li>{i18nT('pages.settings.securityPanel.credential_redaction_step_open')}</li>
            <li>{i18nT('pages.settings.securityPanel.credential_redaction_step_on')}</li>
          </ol>
        )}
        <p className="m-0">{i18nT('pages.settings.securityPanel.credential_redaction_desc_tail')}</p>
      </div>
      {isLoading ? (
        // The read is in flight: show the shape of the control, so a slow
        // connection never renders a setting that appears to have no control.
        <div data-testid="credential-redaction-loading" aria-busy="true">
          <SkeletonToggleRow />
        </div>
      ) : view ? (
        <div data-testid="credential-redaction-row" className="flex flex-col gap-1.5 border border-border rounded-md px-3 py-2">
          <SettingsToggle
            label={i18nT('pages.settings.securityPanel.credential_redaction_toggle')}
            description={i18nT('pages.settings.securityPanel.credential_redaction_toggle_help_summary')} hint={i18nT('pages.settings.securityPanel.credential_redaction_toggle_help')}
            checked={view.enabled}
            disabled={busy}
            onChange={enabled => set.mutate(enabled)}
          />
          {!view.enabled && (
            <div data-testid="credential-redaction-off-notice" className="flex items-start gap-2 text-[11px] text-warn leading-relaxed">
              <AlertTriangle size={13} className="shrink-0 mt-px" />
              <span>
                {i18nT('pages.settings.securityPanel.credential_redaction_off_notice')}
                {view.changed_at && (
                  <> {i18nT('pages.settings.securityPanel.credential_redaction_off_since', { time: fmtDateTime(view.changed_at) })}</>
                )}
              </span>
            </div>
          )}
          {/* A rejected PUT is an error the agent can usually fix (an owner-gate
              refusal, a keystone write failure), so the hand-off is on; nothing
              on this card is destroyed by navigating away. */}
          {set.isError && (
            <ErrorNotice
              variant="inline"
              className="mt-1"
              message={i18nT('pages.settings.securityPanel.credential_redaction_write_failed', { state: i18nT(view.enabled ? 'pages.settings.securityPanel.credential_redaction_still_on' : 'pages.settings.securityPanel.credential_redaction_still_off') })}
              askAgent
              askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
              testId="credential-redaction-write-failed"
            />
          )}
        </div>
      ) : ownerGateRefused ? (
        // A non-owner: not a failure to recover from, a fact about the viewer.
        // No Retry (it can never succeed and each click would write another
        // audited refusal). The hand-off stays, as every ErrorNotice's does: the
        // agent cannot make the viewer the owner, but it can say who is and
        // what the viewer's session is, which is the next thing they need.
        <ErrorNotice
          variant="inline"
          message={i18nT('pages.settings.securityPanel.credential_redaction_owner_only')}
          askAgent
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="credential-redaction-owner-only"
        />
      ) : isError ? (
        // With the read failed there is no switch to move, so the notice carries
        // the only recovery that does not mean reloading the page: the query's
        // own retry (same shape as the crewmates config error). Block variant:
        // the inline one renders no footer.
        <ErrorNotice
          message={i18nT('pages.settings.securityPanel.credential_redaction_read_failed')}
          askAgent
          // The shared control's bare "Ask the agent" reads as a guess on an
          // error box (open a chat? file a report? let it change something?);
          // the label says what the click does.
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="credential-redaction-read-failed"
          footer={
            <Btn onClick={() => { void refetch() }} disabled={isFetching} data-testid="credential-redaction-retry">
              {i18nT('pages.settings.securityPanel.credential_redaction_retry')}
            </Btn>
          }
        />
      ) : null}
    </SettingsCard>
  )
}

/* ── Redaction engine selector ───────────────────────────────────────────────
 *
 * The same record as the two switches (`engine`), and a different question:
 * the switches decide whether redaction runs on the Files view and on the
 * prompt and logs; this picks which engine redacts agent output wherever it
 * runs. Its own section and heading, so the toggles cannot be read as one setting.
 */
function RedactionEngineCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError, error } = useCredentialRedactionRead()
  const set = useMutation({
    mutationFn: (engine: CredentialRedactionEngine) => api.setCredentialRedactionEngine(engine),
    onSuccess: state => {
      qc.setQueryData(['credential-redaction'], state)
      // A body read under the other engine may show values this one keeps (or
      // the reverse), so drop it exactly as a switch change does.
      purgeDocumentBodiesForRedactionChange(qc)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['credential-redaction'] }),
  })
  const view = isError ? undefined : data
  // The switch card above already explains a failed or refused read; this one
  // shows no control rather than a default it could not confirm.
  if (isLoading) {
    return (
      <SettingsCard>
        <div data-testid="credential-redaction-engine-loading" aria-busy="true"><SkeletonToggleRow /></div>
      </SettingsCard>
    )
  }
  if (!view || (isError && isOwnerGateRefusal(error))) return null
  const structured = (view.engine ?? 'structured') === 'structured'
  return (
    <SettingsCard>
      <div data-testid="credential-redaction-engine-row" className="flex flex-col gap-1.5 border border-border rounded-md px-3 py-2">
        <SettingsToggle
          label={i18nT('pages.settings.securityPanel.credential_redaction_engine_toggle')}
          description={i18nT('pages.settings.securityPanel.credential_redaction_engine_toggle_help_summary')}
          hint={i18nT('pages.settings.securityPanel.credential_redaction_engine_toggle_help')}
          checked={structured}
          disabled={set.isPending}
          onChange={on => set.mutate(on ? 'structured' : 'legacy')}
        />
        {!structured && (
          <div data-testid="credential-redaction-engine-legacy-notice" className="text-[11px] text-muted leading-relaxed">
            {i18nT('pages.settings.securityPanel.credential_redaction_engine_legacy_notice')}
          </div>
        )}
        {set.isError && (
          <ErrorNotice
            variant="inline"
            className="mt-1"
            message={i18nT('pages.settings.securityPanel.credential_redaction_engine_write_failed', { state: i18nT(structured ? 'pages.settings.securityPanel.credential_redaction_engine_still_structured' : 'pages.settings.securityPanel.credential_redaction_engine_still_legacy') })}
            askAgent
            askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
            testId="credential-redaction-engine-write-failed"
          />
        )}
      </div>
    </SettingsCard>
  )
}

/* ── Output-seams switch ──────────────────────────────────────────────────────
 *
 * The second switch in the same record (`seams_enabled`), with its own toggle so
 * a stored file-view OFF keeps the meaning it had when the owner chose it. It
 * covers the conversation replayed into the agent's prompt and the gateway's log
 * lines; `src/kiro_crew/redaction_seams.py` is the list. The card shows only the
 * toggle, a one-line effect and a short hint, so the owner reads what turning it
 * off does without the mechanism behind it.
 *
 * Same read as the card above (one query key, so one request). A failed or
 * refused read renders no switch here either; the card above already shows the
 * error and its recovery, so this one does not repeat them.
 */
function CredentialRedactionSeamsCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useCredentialRedactionRead()
  const set = useMutation({
    mutationFn: (enabled: boolean) => api.setCredentialRedactionSeams(enabled),
    // No document purge: this switch reaches no file body.
    onSuccess: state => { qc.setQueryData(['credential-redaction'], state) },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['credential-redaction'] }),
  })
  const view = isError ? undefined : data
  // A server without the field has no such switch, and its seams always redact.
  const on = view?.seams_enabled !== false
  return (
    <SettingsCard>
      {isLoading ? (
        <div data-testid="credential-redaction-seams-loading" aria-busy="true">
          <SkeletonToggleRow />
        </div>
      ) : view ? (
        <div data-testid="credential-redaction-seams-row" className="flex flex-col gap-1.5 border border-border rounded-md px-3 py-2">
          <SettingsToggle
            label={i18nT('pages.settings.securityPanel.credential_redaction_seams_toggle')}
            description={i18nT('pages.settings.securityPanel.credential_redaction_seams_toggle_help_summary')} hint={i18nT('pages.settings.securityPanel.credential_redaction_seams_toggle_help')}
            checked={on}
            disabled={set.isPending}
            onChange={enabled => set.mutate(enabled)}
          />
          {!on && (
            <div data-testid="credential-redaction-seams-off-notice" className="flex items-start gap-2 text-[11px] text-warn leading-relaxed">
              <AlertTriangle size={13} className="shrink-0 mt-px" />
              <span>
                {i18nT('pages.settings.securityPanel.credential_redaction_seams_off_notice')}
                {view.seams_changed_at && (
                  <> {i18nT('pages.settings.securityPanel.credential_redaction_off_since', { time: fmtDateTime(view.seams_changed_at) })}</>
                )}
              </span>
            </div>
          )}
          {set.isError && (
            <ErrorNotice
              variant="inline"
              className="mt-1"
              message={i18nT('pages.settings.securityPanel.credential_redaction_write_failed', { state: i18nT(on ? 'pages.settings.securityPanel.credential_redaction_seams_still_on' : 'pages.settings.securityPanel.credential_redaction_seams_still_off') })}
              askAgent
              askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
              testId="credential-redaction-seams-write-failed"
            />
          )}
        </div>
      ) : null}
    </SettingsCard>
  )
}

/* ── Redaction: hosts allowed long queries, per workspace ── */
export function RedactionAllowedHostsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: ['redaction-allowed-hosts'],
    queryFn: () => api.redactionAllowedHosts(),
  })
  const revoke = useMutation({
    mutationFn: ({ workspace, host }: { workspace: string; host: string }) => api.redactionRevokeHost(workspace, host),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['redaction-allowed-hosts'] }),
  })
  const rows = Object.entries(data?.workspaces ?? {}).flatMap(([workspace, hosts]) => hosts.map(host => ({ workspace, host })))
  return (
    <SettingsCard>
      <div className="mb-3 text-[12px] leading-relaxed text-muted" data-testid="redaction-settings-note">
        {i18nT('pages.settings.securityPanel.redaction_always_on')}
      </div>
      <ErrorNotice
        variant="inline"
        askAgent
        message={error ? i18nT('pages.settings.securityPanel.redaction_load_failed') : null}
      />
      <ErrorNotice
        variant="inline"
        askAgent
        message={revoke.isError ? i18nT('pages.settings.securityPanel.redaction_revoke_failed') : null}
        onDismiss={() => revoke.reset()}
      />
      {!isLoading && !error && rows.length === 0 && (
        <div className="text-[12px] text-muted" data-testid="redaction-allowed-empty">
          {i18nT('pages.settings.securityPanel.redaction_no_allowed_hosts')}
        </div>
      )}
      {rows.length > 0 && (
        <div className="divide-y divide-border" data-testid="redaction-allowed-list">
          {rows.map(({ workspace, host }) => (
            <div key={`${workspace}\u0000${host}`} className="flex items-center justify-between gap-3 py-2">
              <div className="min-w-0">
                <div className="truncate font-mono text-[13px] text-text">{host}</div>
                <div className="text-[11px] text-muted">
                  {i18nT('pages.settings.securityPanel.redaction_workspace', { workspace })}
                </div>
              </div>
              <Btn
                danger
                disabled={revoke.isPending}
                onClick={() => revoke.mutate({ workspace, host })}
                data-testid="redaction-revoke"
              >
                {i18nT('pages.settings.securityPanel.redaction_revoke')}
              </Btn>
            </div>
          ))}
        </div>
      )}
    </SettingsCard>
  )
}

/** Settings -> Security -> Redaction: the file-view switch, the allowed link hosts
 *  (whose note names "the switch above", the file-view one), the prompt-and-logs
 *  switch, then the engine selector, which applies to every path. */
export function RedactionSection() {
  return (
    <>
      <SettingsSection title={i18nT('pages.settings.securityPanel.credential_redaction_section')}>
        <CredentialRedactionCard />
      </SettingsSection>
      <SettingsSection title={i18nT('pages.settings.securityPanel.redaction_allowed_hosts_section')}>
        <RedactionAllowedHostsCard />
      </SettingsSection>
      <SettingsSection title={i18nT('pages.settings.securityPanel.credential_redaction_seams_section')}>
        <CredentialRedactionSeamsCard />
      </SettingsSection>
      <SettingsSection title={i18nT('pages.settings.securityPanel.credential_redaction_engine_section')}>
        <RedactionEngineCard />
      </SettingsSection>
    </>
  )
}
