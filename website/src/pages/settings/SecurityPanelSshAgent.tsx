/**
 * Settings -> Security -> SSH agent forwarding: the owner grant that forwards the
 * operator's ssh-agent socket into new sessions. One of the sections
 * `SecurityPanel.tsx` composes.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, ChevronRight, Layers } from 'lucide-react'
import { Badge, Btn } from '../../components/ui'
import { SettingsCard } from '../../components/settings'
import { api, type SshAgentConsentStatus } from '../../api/client'
import { acpBackendName } from '../../api/acpBackend'
import { AGENT_SANDBOX_PATH } from '../overview/agentSandboxLink'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'

/* ── SSH agent forwarding consent ───────────────────────────────────────────
 *
 * A VIEW over the grant the `/api/ssh-agent/consent` endpoints own: one row, one
 * badge, one action. The owner-gated handler is the only writer and records the
 * grant on Allow, the same one-step shape as the paid-AWS consent. The cached
 * read is never the authority: after a write this card INVALIDATES rather than
 * asserting the new state.
 *
 * Forwarding the ssh-agent socket lets any code a session runs authenticate as
 * the owner with every key the agent holds, for the whole session. That is why
 * the risk line is always visible rather than folded into a tooltip, and why the
 * grant is read at agent SPAWN: sessions already running are unaffected, so the
 * copy says "new sessions" and never implies a running one changed.
 *
 * `socket_present` says whether a new session would have an ssh-agent socket to
 * forward; when it would not, the card says so instead of offering a grant that
 * would forward nothing.
 */
export function SshAgentConsentCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<SshAgentConsentStatus>({
    queryKey: ['ssh-agent-consent'],
    queryFn: api.sshAgentConsent,
    // Not the app-wide `staleTime: Infinity`: another owner tab, or a hand edit
    // of the settings file, can change the grant, and a stale "Not allowed" over
    // a live grant is the dangerous direction. Re-read on focus and remount.
    staleTime: 0,
    refetchOnWindowFocus: true,
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['ssh-agent-consent'] })
  }
  const grant = useMutation({
    mutationFn: () => api.grantSshAgentConsent(),
    onSettled: invalidate,
  })
  const revoke = useMutation({
    mutationFn: () => api.revokeSshAgentConsent(),
    onSettled: invalidate,
  })

  // An unreadable state renders NO state, gated on `isError` rather than on
  // `data === undefined`, because react-query keeps the last good `data` across
  // a failed refetch and "Not allowed" for a grant that is now live is the
  // dangerous direction of wrong.
  const view = isError ? undefined : data
  const granted = !!view?.granted
  const busy = isLoading || grant.isPending || revoke.isPending
  // Which sessions this setting decides. `all`: the sandbox level forwards for
  // every harness, so the setting decides for none and Allow is not offered.
  // `some`: the named harnesses forward regardless, so the row is labelled with
  // the harnesses it does decide. `none`: the setting decides for every session.
  // Names are product names (the shared translated label, else the server's),
  // never the word "agent", which on this card already means the SSH agent.
  const labels = view?.harness_labels ?? {}
  const nameOf = (id: string) => acpBackendName({ id, policy_id: labels[id] })
  const forwarded = view?.forwarded_harnesses ?? []
  const forwardedNames = forwarded.map(nameOf)
  const decidedNames = Object.keys(labels).filter(id => !forwarded.includes(id)).sort().map(nameOf)
  const tierScope: 'all' | 'some' | 'none' = view?.forwarded_without_consent
    ? 'all'
    : forwardedNames.length > 0 ? 'some' : 'none'
  // The row and its Allow name the sessions the setting decides only when the
  // gateway sent the names to say so; otherwise both keep the generic wording.
  const scoped = tierScope === 'some' && decidedNames.length > 0
  // "Until you withdraw this" holds only where the setting decides every session.
  const riskKey = tierScope === 'none'
    ? 'pages.settings.securityPanel.ssh_agent_risk'
    : 'pages.settings.securityPanel.ssh_agent_risk_tier'
  const sandboxLink = (
    <Link
      to={AGENT_SANDBOX_PATH}
      data-testid="ssh-agent-sandbox-link"
      className="mt-0.5 inline-flex min-w-0 self-start items-center gap-0.5 rounded text-[11px] text-accent hover:underline focus-ring"
    >
      <span className="min-w-0">{i18nT('pages.settings.securityPanel.ssh_agent_sandbox_link')}</span>
      <ChevronRight size={12} aria-hidden="true" className="shrink-0" />
    </Link>
  )

  return (
    <SettingsCard>
      <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.ssh_agent_title')}</div>
      {/* Purpose FIRST, in body colour: a reader who meets the warning before
          the reason has no reason to ever press the control. Then the risk,
          stated in the open, not behind an InfoTip. */}
      <div className="text-[12px] text-text mt-0.5 leading-relaxed">{i18nT('pages.settings.securityPanel.ssh_agent_when')}</div>
      <div className="text-[12px] text-muted mt-1 mb-2 leading-relaxed flex items-start gap-1" data-testid="ssh-agent-risk">
        <AlertTriangle size={12} className="shrink-0 mt-[3px] text-warn" aria-hidden="true" />
        <div className="min-w-0">
          <p>{i18nT(riskKey)}</p>
          <p>{i18nT('pages.settings.securityPanel.ssh_agent_risk_running')}</p>
        </div>
      </div>

      {view && (
        <div
          data-testid="ssh-agent-row"
          className="flex flex-col gap-1.5 border border-border rounded-md px-3 py-2"
        >
          <div className="flex items-start gap-2">
            <div className="flex-1 min-w-0">
              <div className="text-[12px] text-text flex items-center gap-1.5 flex-wrap">
                <span data-testid="ssh-agent-row-label">
                  {scoped
                    ? i18nT('pages.settings.securityPanel.ssh_agent_row_label_scoped')
                    : i18nT('pages.settings.securityPanel.ssh_agent_row_label')}
                </span>
                {/* Allowed is the risky state, so it is never the green "ok".
                    The sandbox-level state is a different fact (the owner's grant
                    plays no part), so it gets its own neutral badge with an icon
                    rather than the same yellow as Allowed. */}
                <Badge variant={tierScope !== 'all' && granted ? 'warn' : 'muted'} data-testid="ssh-agent-state">
                  {tierScope === 'all'
                    ? (
                      <span className="inline-flex items-center gap-1">
                        <Layers size={11} aria-hidden="true" className="shrink-0" />
                        {i18nT('pages.settings.securityPanel.ssh_agent_state_on_by_tier')}
                      </span>
                    )
                    : i18nT(granted
                      ? 'pages.settings.securityPanel.ssh_agent_state_allowed'
                      : 'pages.settings.securityPanel.ssh_agent_state_not_allowed')}
                </Badge>
              </div>
              {/* The label says which sessions in plain words; the product
                  names sit underneath, muted, for the reader who wants them. */}
              {scoped && (
                <div className="text-[11px] text-muted mt-0.5" data-testid="ssh-agent-row-names">
                  {i18nT('pages.settings.securityPanel.ssh_agent_row_scoped_names', { harnesses: decidedNames.join(', ') })}
                </div>
              )}
              {/* What Allow gets the owner, and that it can be undone, sit next
                  to the control rather than under the risk text. */}
              {!granted && tierScope !== 'all' && (
                <div className="text-[11px] mt-0.5 leading-relaxed" data-testid="ssh-agent-allow-benefit">
                  <div className="text-text">{i18nT('pages.settings.securityPanel.ssh_agent_allow_benefit')}</div>
                  {view.socket_present && (
                    <div className="text-muted">{i18nT('pages.settings.securityPanel.ssh_agent_allow_help')}</div>
                  )}
                </div>
              )}
              {/* On a tier that leaves ~/.ssh readable the spawn keeps the socket
                  with no grant for every harness, so the badge says it is on and
                  the card points at the sandbox level, the control that changes it. */}
              {tierScope === 'all' && (
                <div className="text-[11px] text-text mt-0.5 leading-relaxed" data-testid="ssh-agent-tier-forwards">
                  {i18nT('pages.settings.securityPanel.ssh_agent_tier_forwards')}
                </div>
              )}
              {tierScope === 'all' && sandboxLink}
              {!view.socket_present && (
                <div className="text-[11px] text-muted mt-0.5 leading-relaxed" data-testid="ssh-agent-no-socket">
                  {i18nT('pages.settings.securityPanel.ssh_agent_no_socket')}
                </div>
              )}
              {granted && (
                <div className="text-[11px] text-muted mt-0.5">
                  {/* A grant with no timestamp was written to the settings file
                      outside this page; say so, or two "Allowed" rows look like
                      two different states for no visible reason. */}
                  {view.granted_at
                    ? <div>{i18nT('pages.settings.securityPanel.ssh_agent_since', { time: fmtDateTime(view.granted_at) })}</div>
                    : <div>{i18nT('pages.settings.securityPanel.ssh_agent_allowed_by_file')}</div>}
                  <div>{i18nT('pages.settings.securityPanel.ssh_agent_applies_new_sessions')}</div>
                </div>
              )}
            </div>
            {granted
              ? (
                // Where the level forwards for everyone the grant counts only at
                // the Strict level, so the label says that.
                <Btn disabled={busy} onClick={() => revoke.mutate()} data-testid="ssh-agent-revoke">
                  {i18nT(tierScope === 'all'
                    ? 'pages.settings.securityPanel.ssh_agent_revoke_tier'
                    : 'pages.settings.securityPanel.ssh_agent_revoke')}
                </Btn>
              )
              // Every harness already forwards at this sandbox level, so Allow
              // would change nothing; the sandbox link above is the control.
              : tierScope === 'all' ? null : (
                // Disabled while no ssh-agent is visible: a grant made then
                // forwards nothing, and the note above says what to do first.
                <Btn
                  primary
                  disabled={busy || !view.socket_present}
                  onClick={() => grant.mutate()}
                  data-testid="ssh-agent-allow"
                >
                  {i18nT(scoped
                    ? 'pages.settings.securityPanel.ssh_agent_allow_scoped'
                    : 'pages.settings.securityPanel.ssh_agent_allow')}
                </Btn>
              )}
          </div>
          {/* A grant held while the level forwards for everyone: Withdraw still
              removes it, but sessions keep the agent, so say both. */}
          {granted && tierScope === 'all' && (
            <div className="text-[11px] text-text mt-0.5 leading-relaxed" data-testid="ssh-agent-revoke-tier-help">
              {i18nT('pages.settings.securityPanel.ssh_agent_revoke_tier_help')}
            </div>
          )}
          {/* The usual default: some harnesses (Kiro CLI on the standard level)
              keep the socket with no grant while masked ones need it. The note
              naming them sits BELOW the action: between the label and the
              control it reads as "this switch protects nothing". */}
          {tierScope === 'some' && (
            <div className="flex flex-col border-t border-border pt-1.5 mt-0.5">
              <div className="text-[11px] text-muted leading-relaxed" data-testid="ssh-agent-tier-forwards-some">
                {i18nT('pages.settings.securityPanel.ssh_agent_tier_forwards_some', {
                  harnesses: forwardedNames.join(', '),
                })}
              </div>
              {sandboxLink}
            </div>
          )}
        </div>
      )}

      {isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.ssh_agent_load_failed')} askAgent />
      )}
      {(grant.isError || revoke.isError) && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.ssh_agent_save_failed')} askAgent />
      )}
    </SettingsCard>
  )
}
