/**
 * Settings -> Security -> Third-party apps and Registry trust: the blanket and
 * per-app trust grants for third-party app code, the per-registry Git-identity
 * trust grants, and the failure messages both cards render (`SecurityPanel.tsx`
 * re-exports `trustFailureMessage` and `registryTrustFailureMessage`).
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Trans } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Package, GitBranch } from 'lucide-react'
import { Badge, Btn, Checkbox } from '../../components/ui'
import { SettingsCard, SettingsToggle } from '../../components/settings'
import Modal from '../../components/Modal'
import {
  api, ApiError, type TrustedAppsData, type TrustedRegistriesData, type TrustedRegistryRow,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'

/* ── Third-party trust confirm target ── */
type TrustConfirmTarget =
  // Turning the BLANKET third-party-app trust flag on. Same acknowledgement gate
  // as a deny opt-out: both widen what un-reviewed code is allowed to do.
  | { kind: 'trust-all' }
  // Revoking one app's grant. Confirmed, unlike the other revoke-ish controls in
  // this panel, because it is the one that STOPS something the user is using: the
  // app is disabled and quits working. The consequence has to be on screen BEFORE
  // the click, not reported after it.
  | { kind: 'revoke-app'; name: string }

/**
 * The operator-facing reason a trust change failed, from an `ApiError`.
 *
 * The backend's prose is preferred over a generic "request failed" because the
 * two 409s here are not retry-and-hope conditions: `trust_setting_overlay_owned`
 * names the FILE and KEY the user has to edit (nothing the UI can do for them,
 * since `config.local.json` is user-owned and never written by Kiro Crew), and
 * `blanket_trust_sweep_incomplete` names the apps still executing after trust was
 * withdrawn. Collapsing either into "something went wrong" hides the only
 * actionable part.
 */
export function trustFailureMessage(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.body) as { error?: unknown }
      if (typeof parsed.error === 'string' && parsed.error.trim()) return parsed.error
    } catch {
      // not JSON — fall through to the mapped message
    }
    return err.message
  }
  return err instanceof Error && err.message
    ? err.message
    : i18nT('pages.settings.securityPanel.trustedApps.unknown_error')
}

/* ── Third-party app execution ── */

/** Per-app trust grants for third-party app code (`agent.apps_trusted`), plus the
 *  blanket `agent.apps_allow_third_party` escape hatch.
 *
 *  Third-party app code does not run unless it is trusted. Two levers: the
 *  per-app grants listed here, and the blanket allow-all flag. The allow-all row
 *  is a `SettingsToggle` rather than a hand-rolled row so it carries
 *  `data-setting-label` (the deep-link target the App Store links to) and is
 *  picked up by scripts/gen-settings-registry.mjs. */
export function ThirdPartyAppsCard() {
  const qc = useQueryClient()
  // Separate query from denied-commands: a different endpoint, a different
  // snapshot, and it must keep rendering when either of the two fails.
  const { data: taRaw, isError: taError } = useQuery<TrustedAppsData>({
    queryKey: ['trusted-apps'],
    queryFn: api.listTrustedApps,
  })
  // Normalize at the fetch boundary. `ineffective` is newer than `apps`, so a
  // newer dashboard talking to an older gateway (or any response shape that drops
  // a list) would otherwise reach `ta.ineffective.length` and throw — taking down
  // the WHOLE Security page, not just this card. The i18n render gate caught
  // exactly that on /settings/security.
  const ta = useMemo(
    () =>
      taRaw
        ? {
          ...taRaw,
          apps: Array.isArray(taRaw.apps) ? taRaw.apps : [],
          ineffective: Array.isArray(taRaw.ineffective) ? taRaw.ineffective : [],
          allowAll: taRaw.allowAll === true,
        }
        : undefined,
    [taRaw],
  )
  // A FAILED read, not a slow one: no actionable control, and say so.
  const taUnavailable = taError === true

  const [confirm, setConfirm] = useState<TrustConfirmTarget | null>(null)
  const [ack, setAck] = useState(false)
  // Name of the app whose revoke ALSO disabled it, so the panel can say so.
  // Cleared on the next revoke, so the notice always refers to the last action.
  const [revokeDisabledApp, setRevokeDisabledApp] = useState<string | null>(null)
  // A trust mutation can FAIL in ways the operator has to see rather than infer
  // from a toggle springing back: the setting may be owned by config.local.json
  // (so writing config.json would change nothing), or the blanket-off sweep may
  // have left apps running. Both arrive as a 409 with a `code`, and both mean
  // "the thing you asked for did not happen" — silence here would reproduce, in
  // the UI, exactly the false-success the backend fixed.
  const [trustError, setTrustError] = useState<string | null>(null)

  // The acknowledgment checkbox resets whenever the modal opens or closes.
  useEffect(() => { setAck(false) }, [confirm])

  const applyTrustSnapshot = (snap: TrustedAppsData) => {
    // Field-by-field rather than the whole response: the revoke result carries an
    // extra `disabled` flag that belongs to that one action, not to the snapshot.
    qc.setQueryData(['trusted-apps'], {
      apps: snap.apps,
      ineffective: snap.ineffective,
      allowAll: snap.allowAll,
    })
    // Trust changes gate whether an app's code may run, so the App Store's own
    // enable/disable state can change underneath us — refetch it too.
    qc.invalidateQueries({ queryKey: ['apps'] })
  }
  const setTrustAll = useMutation({
    mutationFn: (value: boolean) => api.setTrustAllApps(value),
    onSuccess: snap => {
      setTrustError(null)
      applyTrustSnapshot(snap)
    },
    onError: (err: unknown) => setTrustError(trustFailureMessage(err)),
  })
  const untrust = useMutation({
    mutationFn: (name: string) => api.untrustApp(name),
    onSuccess: (snap, name) => {
      setTrustError(null)
      applyTrustSnapshot(snap)
      // Only surface the notice when the backend actually disabled something —
      // revoking trust on an already-disabled app is a silent no-op there.
      setRevokeDisabledApp(snap.disabled ? name : null)
    },
    onError: (err: unknown) => setTrustError(trustFailureMessage(err)),
  })

  // Granting blanket trust WEAKENS protection → acknowledgement gate. Revoking
  // it tightens, so it applies immediately (same asymmetry as the deny rules).
  const onTrustAllToggle = (next: boolean) => {
    if (next) setConfirm({ kind: 'trust-all' })
    else setTrustAll.mutate(false)
  }

  const runConfirm = () => {
    if (!confirm) return
    if (confirm.kind === 'trust-all') setTrustAll.mutate(true)
    else untrust.mutate(confirm.name)
    setConfirm(null)
  }

  const confirmBody = !confirm ? '' : confirm.kind === 'trust-all'
    ? i18nT('pages.settings.securityPanel.trustedApps.allow_all_confirm_body')
    : i18nT('pages.settings.securityPanel.trustedApps.revoke_confirm_body', { name: confirm.name })
  // Revoking one grant TIGHTENS security — it withdraws permission and stops the
  // app. It still needs a confirm (the app stops working, which the user must see
  // coming) but not an "I understand this weakens protection" acknowledgement:
  // demanding one for the safe direction trains people to tick the box without
  // reading, which is exactly what makes it worthless on the dangerous direction.
  const needsAck = confirm?.kind === 'trust-all'

  return (
    <>
      <SettingsCard>
        <div className="text-[12px] text-muted mb-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.trustedApps.description')}
        </div>

        {/* UNKNOWN is not OFF. If the snapshot has not resolved, the persisted
            flag may well be on, and rendering the switch at OFF would be wrong
            twice over: the blanket-trust warning below stays hidden while
            third-party code is still admitted, and a click would write `true`
            onto an ALREADY-true setting rather than revoking it. So on a failed
            read render no switch at all — `role="switch"` has no "unknown"
            (aria-checked `mixed` is checkbox-only), so any switch here would
            assert a state we could not read. A still-loading read keeps the
            disabled switch, since it resolves on its own. */}
        {taUnavailable ? (
          // Read failure on a switch-only card → hand-off on.
          <ErrorNotice
            variant="inline"
            className="py-1.5"
            message={i18nT('pages.settings.securityPanel.third_party_apps_unavailable')}
            askAgent
          />
        ) : (
          <SettingsToggle
            label={i18nT('pages.settings.securityPanel.trustedApps.allow_all_label')}
            description={i18nT('pages.settings.securityPanel.trustedApps.allow_all_description')}
            checked={ta?.allowAll === true}
            onChange={onTrustAllToggle}
            disabled={!ta}
          />
        )}

        {/* What switching it back off actually does. #1414 shipped this string
            saying an already-running app stays up; that stopped being true once
            the falling edge gained a teardown sweep, so the copy was corrected
            and is rendered here rather than left orphaned in the catalogs. */}
        <div className="text-[12px] text-muted py-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.third_party_apps_scope_note')}
        </div>

        {/* The blanket flag's real cost, in the words #1414 already shipped and
            translated: it trusts every third-party app including future ones. */}
        {ta?.allowAll === true && (
          <div className="flex items-start gap-1.5 text-[12px] text-warn py-1.5 leading-relaxed">
            <AlertTriangle size={13} className="shrink-0 mt-0.5" />
            <span>{i18nT('pages.settings.securityPanel.third_party_apps_on_warning')}</span>
          </div>
        )}

        {ta && (ta.apps.length === 0 ? (
          <div className="text-[12px] text-muted py-2 leading-relaxed border-t border-border mt-1 pt-2">
            {i18nT('pages.settings.securityPanel.trustedApps.empty')}
          </div>
        ) : (
          <div className="divide-y divide-border border-t border-border mt-1">
            {ta.apps.map(name => (
              <div key={name} data-testid={`trusted-app-${name}`} className="flex items-center gap-2.5 py-2">
                <Package size={14} className="lucide-inline shrink-0 text-muted" />
                <code className="flex-1 min-w-0 text-[12px] font-mono text-text break-all">{name}</code>
                <Badge variant="warn">{i18nT('pages.settings.securityPanel.trustedApps.trusted_badge')}</Badge>
                {/* The consequence is stated BEFORE the click (hint here, full
                    sentence in the confirm), because revoking stops an app the
                    user is actively using — learning that afterwards is the
                    failure a first-run reviewer flagged as a blocker. */}
                <span className="text-[11px] text-muted hidden sm:inline">
                  {i18nT('pages.settings.securityPanel.trustedApps.revoke_hint')}
                </span>
                <Btn
                  danger
                  disabled={untrust.isPending}
                  onClick={() => setConfirm({ kind: 'revoke-app', name })}
                >
                  {i18nT('pages.settings.securityPanel.trustedApps.revoke')}
                </Btn>
              </div>
            ))}
          </div>
        ))}

        {/* Stored-but-unenforced entries. `ineffective` is the set the gate
            IGNORES because the name fails the app-name charset (a hand-edited
            config.json can hold `LD-App`, a trailing space, a fullwidth
            homoglyph, `..`, `*`). Rendering them inside the list above claimed
            trust that does not exist, and left the user with no explanation for
            why their app was still blocked. Revoke is offered here too — the
            endpoint deliberately does NOT validate the name being removed, so
            junk that can never be granted can still be cleared out.

            Explicit color-mix rather than a `bg-card/88` opacity modifier:
            theme colors are raw `var(--x)` with no <alpha-value>, so a Tailwind
            opacity suffix silently generates nothing. */}
        {ta && ta.ineffective.length > 0 && (
          <div
            data-testid="trusted-apps-ineffective"
            className="mt-2 rounded-md border border-border bg-[color-mix(in_srgb,var(--card)_88%,transparent)] px-3 py-2.5"
          >
            <div className="flex items-start gap-2.5">
              <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
              <div className="min-w-0">
                <div className="text-[12px] font-semibold text-text">
                  {i18nT('pages.settings.securityPanel.trustedApps.ineffective_label')}
                </div>
                <div className="text-[12px] text-muted leading-relaxed">
                  {i18nT('pages.settings.securityPanel.trustedApps.ineffective_description')}
                </div>
              </div>
            </div>
            <div className="divide-y divide-border border-t border-border mt-2">
              {ta.ineffective.map(name => (
                <div key={name} data-testid={`ineffective-app-${name}`} className="flex items-center gap-2.5 py-2">
                  <Package size={14} className="lucide-inline shrink-0 text-muted" />
                  <code className="flex-1 min-w-0 text-[12px] font-mono text-muted line-through break-all">{name}</code>
                  <Btn danger disabled={untrust.isPending} onClick={() => untrust.mutate(name)}>
                    {i18nT('pages.settings.securityPanel.trustedApps.revoke')}
                  </Btn>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* A refused trust change (409 codes via trustFailureMessage). The card
            holds toggles and revoke buttons only — nothing to lose → hand-off on. */}
        {trustError && (
          <ErrorNotice
            className="mt-2"
            message={i18nT('pages.settings.securityPanel.trustedApps.change_failed', { detail: trustError })}
            askAgent
            onDismiss={() => setTrustError(null)}
          />
        )}
        {revokeDisabledApp && (
          <div className="flex items-start gap-2.5 mt-2 rounded-md bg-bg-elevated border border-border px-3 py-2">
            <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
            <span className="text-[12px] text-muted leading-relaxed">
              {/* `name` is passed whether or not the catalog string interpolates
                  it — an unused variable is ignored, a missing one would render
                  the raw `{{name}}` placeholder to the user. */}
              {i18nT('pages.settings.securityPanel.trustedApps.revoke_disables', { name: revokeDisabledApp })}
            </span>
          </div>
        )}
      </SettingsCard>

      {/* ── Confirm modal (trust every app / revoke one grant) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        guideTrustRoot
        title={confirm?.kind === 'trust-all'
          ? i18nT('pages.settings.securityPanel.trustedApps.allow_all_label')
          : confirm
            ? i18nT('pages.settings.securityPanel.trustedApps.revoke_confirm_title', { name: confirm.name })
            : ''}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            {/* "Disable" is the wrong verb for granting blanket trust. Reuses the
                existing one-word `trustDropdown.trust` string rather than minting an
                eleventh catalog key for a word already translated in all locales. */}
            <Btn danger disabled={needsAck && !ack} onClick={runConfirm}>
              {confirm?.kind === 'trust-all'
                ? i18nT('components.trustDropdown.trust')
                : i18nT('pages.settings.securityPanel.trustedApps.revoke_confirm_ok')}
            </Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="text-[13px] text-text leading-relaxed">{confirmBody}</div>
        </div>
        {needsAck && (
          <label className="flex items-center gap-2.5 mt-4 cursor-pointer">
            <Checkbox checked={ack} onChange={e => setAck(e.target.checked)} />
            <span className="text-[13px] text-text">{i18nT('pages.settings.securityPanel.trustedApps.allow_all_confirm_ack')}</span>
          </label>
        )}
      </Modal>
    </>
  )
}

/** Whether a registry trust change failed because the trust file is damaged. */
function isRegistryTrustCorrupt(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false
  try {
    return (JSON.parse(err.body || '{}') as { code?: unknown }).code === 'corrupt'
  } catch {
    return false
  }
}

/**
 * The operator-facing reason a registry TRUST change failed, in user vocabulary.
 *
 * The coded refusals here are not "request failed": `corrupt` means the trust
 * file is damaged and must be fixed or deleted by hand, `not_served` means the
 * registry is dropped by the merge so a grant would do nothing until it is
 * renamed in the registries editor, and `unknown_registry` means the registry was
 * removed from Apps while the card was open. Rendering the raw backend
 * `detail` for either would bury the one thing the operator can act on, so those
 * codes map to the card's own copy; every other error falls through to
 * `trustFailureMessage` (backend detail, then the mapped status message).
 */
export function registryTrustFailureMessage(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const code = (JSON.parse(err.body || '{}') as { code?: unknown }).code
      if (code === 'corrupt') return i18nT('pages.settings.securityPanel.trustedRegistries.corrupt_notice', { path: 'registry_trust.json' })
      if (code === 'not_served') return i18nT('pages.settings.securityPanel.trustedRegistries.not_served')
      if (code === 'unknown_registry') return i18nT('pages.settings.securityPanel.trustedRegistries.unknown_registry')
    } catch {
      // not JSON — fall through to the generic detail message
    }
  }
  return trustFailureMessage(err)
}

/* ── Registry git-identity trust ── */

/** Which registries may clone the repositories they list with the operator's own
 *  git identity.
 *
 *  A hand-added registry clones WITHOUT credentials by default, so an app kept in
 *  a private repository shows no art and cannot be installed. Granting trust
 *  hands that registry the operator's ssh keys for the repositories it names —
 *  and the registry's authors, not the operator, choose what those are. That is
 *  the asymmetry the confirm exists to state: granting widens what someone else
 *  decides, so it is confirmed; revoking narrows it, so it applies at once.
 *
 *  Build-pinned registries never appear: the build trusts them already, so a row
 *  for one would offer a decision that is not the operator's to make. */
export function TrustedRegistriesCard() {
  const qc = useQueryClient()
  const { data: trRaw, isError: trError, isFetching, refetch } = useQuery<TrustedRegistriesData>({
    queryKey: ['trusted-registries'],
    queryFn: api.listTrustedRegistries,
  })
  // Normalize at the fetch boundary, for the reason ThirdPartyAppsCard does: a
  // response shape that drops the list would otherwise reach `.map` and take the
  // WHOLE Security page down rather than this one card.
  const registries = useMemo(
    () => (Array.isArray(trRaw?.registries) ? trRaw.registries : undefined),
    [trRaw],
  )
  // A FAILED read, not a slow one: no actionable control, and say so.
  const unavailable = trError === true
  // The trust file is damaged: the runtime already fails closed (every row
  // untrusted), so render a notice rather than a healthy-looking all-untrusted
  // list.
  const corrupt = trRaw?.corrupt === true

  // The registry awaiting its grant confirm. Null when no modal is open.
  const [confirm, setConfirm] = useState<TrustedRegistryRow | null>(null)
  const [changeError, setChangeError] = useState<string | null>(null)

  const applyRegistrySnapshot = (snap: TrustedRegistriesData) => {
    qc.setQueryData(['trusted-registries'], snap)
    // Trust decides whether a private repository can be cloned at all, so the
    // App Store's own rows (art, installability) change underneath us.
    qc.invalidateQueries({ queryKey: ['apps'] })
    // RegistryManager's ['registries'] list caches trust: "owner" for 30s; left
    // stale it echoes the pre-revoke tier back into PUT /api/apps/registries,
    // which denies the whole save. Refetch it so the manager sees the new tier.
    qc.invalidateQueries({ queryKey: ['registries'] })
  }
  // Every mutation's onError ALSO invalidates ['trusted-registries']: a refused
  // write means the store's real state may differ from what the card last drew
  // (a keystone that became corrupt surfaces the corrupt notice
  // immediately, not after a manual reload), and the error copy is translated
  // into user vocabulary for the coded refusals rather than shown as raw detail.
  const onMutationError = (err: unknown) => {
    // A damaged file is already explained by the card's own notice, which the
    // refetch below renders with the file's full path; a second copy here would
    // show the same problem twice.
    setChangeError(isRegistryTrustCorrupt(err) ? null : registryTrustFailureMessage(err))
    qc.invalidateQueries({ queryKey: ['trusted-registries'] })
  }
  const grant = useMutation({
    mutationFn: (repo: string) => api.grantTrustedRegistry(repo),
    onSuccess: snap => {
      setChangeError(null)
      applyRegistrySnapshot(snap)
    },
    onError: onMutationError,
  })
  const revoke = useMutation({
    mutationFn: (repo: string) => api.revokeTrustedRegistry(repo),
    onSuccess: snap => {
      setChangeError(null)
      applyRegistrySnapshot(snap)
    },
    onError: onMutationError,
  })

  const runConfirm = () => {
    if (!confirm) return
    grant.mutate(confirm.repo)
    setConfirm(null)
  }

  return (
    <>
      <SettingsCard>
        <div className="text-[12px] text-muted mb-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.trustedRegistries.description')}
        </div>
        <div className="text-[12px] text-muted py-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.trustedRegistries.pinned_note')}
        </div>

        {/* The trust file is damaged: no registry is trusted until the operator
            fixes or deletes it. No product writer produces a damaged file, so
            there is no in-app repair. */}
        {corrupt && (
          // Hand-off on: there is no draft on this card to protect, and the agent
          // can help the operator find and read the file (it cannot write it).
          <ErrorNotice
            className="mt-1"
            message={i18nT('pages.settings.securityPanel.trustedRegistries.corrupt_notice', {
              path: trRaw?.corrupt_path || 'registry_trust.json',
            })}
            askAgent
          />
        )}

        {unavailable && (
          // Read failure on a card whose only controls are buttons — nothing to
          // lose → hand-off on. A Retry footer refetches the query rather than
          // forcing a page reload: the read can fail transiently (a slow keystone
          // read), and there is no other way to recover the list in place. Block
          // variant: the inline notice renders no footer.
          <ErrorNotice
            className="mt-1"
            message={i18nT('pages.settings.securityPanel.trustedRegistries.unavailable')}
            askAgent
            footer={
              <Btn onClick={() => { void refetch() }} disabled={isFetching} data-testid="trusted-registries-retry">
                {i18nT('pages.settings.securityPanel.trustedRegistries.retry')}
              </Btn>
            }
          />
        )}

        {!unavailable && registries && (registries.length === 0 ? (
          <div className="text-[12px] text-muted py-2 leading-relaxed border-t border-border mt-1 pt-2">
            {/* The empty state points at where a registry is added — the App
                Store's Apps page, which hosts the registry-sources manager — so
                it is not a dead end. One key carrying the whole sentence with a
                <store> run pins every locale to its own clause order. The tag is
                deliberately NOT <link>: that is an HTML void element, so the
                Trans parser would drop the link text inside it. */}
            <Trans
              i18nKey="pages.settings.securityPanel.trustedRegistries.empty"
              components={{ store: <Link to="/apps" className="text-accent hover:underline" /> }}
            />
          </div>
        ) : (
          <div className="divide-y divide-border border-t border-border mt-1">
            {registries.map(row => (
              <div
                key={`${row.repo}#${row.name}`}
                data-testid={`trusted-registry-${row.repo}#${row.name}`}
                className="flex items-center gap-2.5 py-2"
              >
                <GitBranch size={14} className="lucide-inline shrink-0 text-muted" />
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] text-text truncate">{row.name}</div>
                  {/* One key carrying both facts rather than two adjacent calls:
                      a sentence assembled from several keys pins every target
                      language to English clause order. */}
                  <div className="text-[11px] text-muted font-mono truncate">
                    {/* The repository path, not just the host: two registries on
                        one forge must read differently before Remove trust. An
                        orphan grant has no branch, so it shows the path alone. */}
                    {row.branch
                      ? i18nT('pages.settings.securityPanel.trustedRegistries.row_meta', {
                          host: row.repo.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '') || row.host,
                          branch: row.branch,
                        })
                      : row.repo.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '') || row.host}
                  </div>
                  {/* A row the merge drops is served by neither claimant, so its
                      apps are never listed and it can never badge Trusted — say
                      so plainly. `not_served_reason` distinguishes a build-pinned
                      name contest from an orphan grant whose config row was
                      removed by hand; a missing/unknown reason falls back to the
                      generic note. */}
                  {row.served === false && (
                    <div className="text-[11px] text-muted leading-relaxed">
                      {row.not_served_reason === 'pinned_name' ? (
                        // The fix is a rename in the registries editor, so link it.
                        <Trans
                          i18nKey="pages.settings.securityPanel.trustedRegistries.not_served_pinned_name"
                          components={{ apps: <Link to="/apps" className="text-accent hover:underline" /> }}
                        />
                      ) : (
                        i18nT(
                          row.not_served_reason === 'not_configured'
                            ? 'pages.settings.securityPanel.trustedRegistries.not_served_not_configured'
                            : 'pages.settings.securityPanel.trustedRegistries.not_served'
                        )
                      )}
                    </div>
                  )}
                </div>
                {/* The same badge the registries editor uses for a trusted source:
                    a state the operator chose, not a warning. */}
                {row.trusted && (
                  <Badge variant="aim">
                    {i18nT('pages.settings.securityPanel.trustedRegistries.trusted_badge')}
                  </Badge>
                )}
                {/* Revoke renders whenever a stored grant exists for the row —
                    served or not — so a grant that went dormant because the merge
                    dropped the row (an operator added a same-name registry) can
                    still be cleared; leaving it would re-arm the moment the
                    collision resolves. Grant renders only on a SERVED, ungranted
                    row with no trust block: elsewhere a Grant would be invisible
                    (the snapshot reads it not-trusted, the button never flips) and
                    the backend refuses it — so the row shows no control and is
                    fixed by renaming/removing it in the registries editor (the
                    note above says so). */}
                {/* A damaged trust file refuses every grant and revoke, so no
                    control is offered until it is fixed. */}
                {corrupt ? null : row.granted ? (
                  // Removing trust narrows what the registry may do, so it is the
                  // safe action and carries no danger styling.
                  <Btn disabled={revoke.isPending} onClick={() => revoke.mutate(row.repo)}>
                    {i18nT('pages.settings.securityPanel.trustedRegistries.revoke')}
                  </Btn>
                ) : row.served !== false ? (
                  <Btn disabled={grant.isPending} onClick={() => setConfirm(row)}>
                    {i18nT('pages.settings.securityPanel.trustedRegistries.grant')}
                  </Btn>
                ) : null}
              </div>
            ))}
          </div>
        ))}

        {changeError && (
          <ErrorNotice
            className="mt-2"
            message={i18nT('pages.settings.securityPanel.trustedRegistries.change_failed', { detail: changeError })}
            askAgent
            onDismiss={() => setChangeError(null)}
          />
        )}
      </SettingsCard>

      {/* ── Confirm modal (hand one registry the operator's git identity) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        guideTrustRoot
        title={confirm
          ? i18nT('pages.settings.securityPanel.trustedRegistries.grant_confirm_title', { name: confirm.name })
          : ''}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            <Btn danger onClick={runConfirm}>
              {i18nT('pages.settings.securityPanel.trustedRegistries.grant_confirm_ok')}
            </Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="min-w-0 text-[13px] text-text leading-relaxed">
            {/* The display name is index content the registry's author controls,
                so the dialog also states the URL the grant is keyed on — the one
                the operator typed into config — where a planted name cannot
                impersonate another registry. */}
            <div className="text-[12px] mb-2">
              {confirm ? (
                <Trans
                  i18nKey="pages.settings.securityPanel.trustedRegistries.grant_confirm_repo"
                  values={{ repo: confirm.repo }}
                  components={{ url: <span className="font-mono [overflow-wrap:anywhere]" /> }}
                />
              ) : null}
            </div>
            {confirm
              ? i18nT('pages.settings.securityPanel.trustedRegistries.grant_confirm_body', { name: confirm.name })
              : ''}
          </div>
        </div>
      </Modal>

    </>
  )
}
