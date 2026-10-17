/**
 * Settings -> Security -> Tailnet: the tailnet origin card, and the catalog key
 * per tailnet state that the Security rail summary reads too. One of the
 * sections `SecurityPanel.tsx` composes.
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldAlert, Lock, AlertTriangle, ExternalLink, Network, Copy, Check } from 'lucide-react'
import { Badge, Btn, Toggle } from '../../components/ui'
import { SettingsCard } from '../../components/settings'
import InfoTip from '../../components/InfoTip'
import { api, type TailnetStatusData } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtTimeNumeric } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { copyToClipboard } from '../../utils/clipboard'
import { CODE_BASE, StatusChip } from './SecurityPanelPosture'

/**
 * Catalog KEY per tailnet `state`, and the badge tone that goes with it.
 *
 * Two flat `Record`s of plain literals rather than one record of objects, so the
 * key-reference gate can still resolve `i18nT(TAILNET_STATE_KEY[state])`
 * statically — a nested `MAP[state].key` is two hops and falls through to the
 * unresolvable-site count. Module scope for the maps is fine because they hold
 * KEYS, not copy: a module-scope `i18nT()` would freeze the boot language.
 *
 * `pinned` reuses the panel's existing policy wording instead of a second
 * sentence about admin pins.
 */
export const TAILNET_STATE_KEY: Record<TailnetStatusData['state'], string> = {
  active: 'pages.settings.securityPanel.tailnet_state_active',
  unresolved: 'pages.settings.securityPanel.tailnet_state_unresolved',
  off: 'pages.settings.securityPanel.tailnet_state_off',
  pinned: 'pages.settings.securityPanel.disabled_by_policy',
}

const TAILNET_STATE_VARIANT: Record<TailnetStatusData['state'], 'ok' | 'warn' | 'muted'> = {
  active: 'ok',
  unresolved: 'warn',
  off: 'muted',
  pinned: 'muted',
}

/* ── Tailnet origin section ─────────────────────────────────────────────────
 *
 * WHY THIS LIVES IN THE SECURITY PANEL, not in a Tailscale/network panel:
 *
 *  1. The setting IS an origin/Host allow-list control. Turning it on appends
 *     this machine's MagicDNS name to the same allowed-origins set that the CSRF
 *     Origin/Referer gate checks on every write and WebSocket upgrade — the
 *     "CSRF Protection" layer listed a few sections down. It is a security
 *     control that happens to be spelled as a Tailscale hostname, not a
 *     networking preference.
 *  2. It belongs beside the security-posture rows, which already report
 *     session-pin state — and the pin caveat below is precisely about that row
 *     stopping being enforceable behind `tailscale serve`.
 *  3. The governance pin for `capabilities.tailnet_origin` shows up in THIS
 *     panel's governance view with no extra wiring, because that view iterates
 *     `SCOPE_CATALOG`. Putting the control anywhere else would split the switch
 *     from the policy row that overrides it.
 *
 * The card renders off `state` and never recomputes it: the backend owns the
 * state machine (`pinned` > `off` > `unresolved` > `active`) so the two layers
 * cannot disagree about what "active" means.
 */
export function TailnetOriginCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<TailnetStatusData>({
    queryKey: ['tailnet-status'],
    queryFn: api.tailnetStatus,
    // The reported host is the STARTUP resolution, so it cannot change while the
    // page is open. Only the config-backed `enabled`/pin can, and both of those
    // invalidate this key on write.
    staleTime: 300_000,
  })
  const save = useMutation({
    // Write path is the generic config PATCH, not a tailnet-specific route: the
    // switch persists `dashboard.tailscale.enabled`, and the status endpoint is
    // read-only because what it reports (the resolved name) is fixed at startup.
    mutationFn: (next: boolean) => api.patchConfig('dashboard.tailscale.enabled', next),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tailnet-status'] }),
  })
  const [copied, setCopied] = useState(false)
  // Separate from `!copied`: idle and failed both read as not-copied, but only
  // the failure needs a notice (same split as MobileLoginCard).
  const [copyFailed, setCopyFailed] = useState(false)

  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(id)
  }, [copied])

  // A FAILED read is not "off" — the same rule ThirdPartyAppsCard follows. If the
  // read failed, the persisted setting may well be on, so collapsing it to off
  // would both hide the pin caveat while the origin is still trusted and make
  // the switch write `true` on click, leaving an active grant unrevokable here.
  if (isError || (!isLoading && data === undefined)) {
    return (
      <SettingsCard>
        <div className="flex items-center justify-between py-1.5">
          <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.tailnet_title')}</span>
          <span className="text-[12px] text-muted shrink-0">{i18nT('pages.settings.securityPanel.third_party_apps_state_unknown')}</span>
        </div>
        {/* Read failure on a switch-only card → hand-off on. */}
        <ErrorNotice
          variant="inline"
          className="mt-1"
          message={i18nT('pages.settings.securityPanel.tailnet_unavailable')}
          askAgent
        />
      </SettingsCard>
    )
  }

  const state = data?.state
  // Read off `state`, never off `enabled`: a governed install can carry
  // `enabled: true` in config while policy forces the capability off, and a
  // switch sitting at ON there would claim an origin that was never allowed.
  const effectiveOn = state === 'active' || state === 'unresolved'
  const pinned = state === 'pinned'

  return (
    <SettingsCard>
      {/* data-setting-label: deep-link anchor for the manual registry entry
          (settingsManual.ts) — the highlight hook queries the rendered label. */}
      <div className="flex items-start justify-between py-1.5 gap-4" data-setting-label={i18nT('pages.settings.securityPanel.tailnet_title')}>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Network size={14} className="lucide-inline text-muted shrink-0" />
            <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.tailnet_title')}</span>
            <InfoTip text={i18nT('pages.settings.securityPanel.tailnet_tip')} />
            {state && <Badge variant={TAILNET_STATE_VARIANT[state]}>{i18nT(TAILNET_STATE_KEY[state])}</Badge>}
          </div>
          <div className="text-[12px] text-muted mt-1 leading-relaxed">
            {i18nT('pages.settings.securityPanel.tailnet_desc')}
          </div>
        </div>
        <span className="shrink-0 flex items-center gap-1.5">
          {pinned && <Lock size={13} className="lucide-inline text-muted" aria-hidden="true" />}
          <Toggle
            checked={effectiveOn}
            onChange={next => save.mutate(next)}
            disabled={isLoading || pinned || save.isPending}
            label={i18nT('pages.settings.securityPanel.tailnet_title')}
          />
        </span>
      </div>

      {/* Three status chips. Each is a FACT the endpoint reported, not a verdict:
          whether a name went into the allow-list, when that happened, and
          whether the per-device session pin can still bind. Rendered whenever
          the feature is on, including `unresolved` — the negative values are the
          whole point of that state. */}
      {effectiveOn && (
        <div className="flex flex-wrap items-center gap-1.5 mt-2">
          <StatusChip
            label={i18nT('pages.settings.securityPanel.tailnet_chip_allowlist')}
            value={state === 'active'
              ? i18nT('pages.settings.securityPanel.tailnet_chip_allowlist_added')
              : i18nT('pages.settings.securityPanel.tailnet_chip_allowlist_absent')}
            variant={state === 'active' ? 'ok' : 'warn'}
          />
          <StatusChip
            label={i18nT('pages.settings.securityPanel.tailnet_chip_resolved')}
            value={data && data.resolved_at > 0
              ? fmtTimeNumeric(data.resolved_at * 1000)
              : i18nT('pages.settings.securityPanel.tailnet_chip_resolved_never')}
            variant={data && data.resolved_at > 0 ? 'muted' : 'warn'}
          />
          {/* Constant by construction, not a read: no same-host tunnel can make
              the pin bind, so this chip states a property of the deployment
              shape rather than a value the server measured. */}
          <StatusChip
            label={i18nT('pages.settings.securityPanel.tailnet_chip_pin')}
            value={i18nT('pages.settings.securityPanel.tailnet_chip_pin_unbound')}
            variant="warn"
          />
        </div>
      )}

      {/* Copyable origin row. Present only in `active`, because that is the only
          state in which an origin string exists AND is trusted. */}
      {state === 'active' && data && (
        <div className="mt-2.5 rounded-md border border-border bg-bg-elevated px-3 py-2">
          <div className="text-[11px] text-muted uppercase tracking-wider font-medium">
            {i18nT('pages.settings.securityPanel.tailnet_origin_label')}
          </div>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <code className="flex-1 min-w-0 truncate text-[13px] font-mono text-text-strong select-all" title={data.origin}>
              {data.origin}
            </code>
            <Btn
              // Acknowledge only on RESOLUTION. Setting "Copied" synchronously
              // claims a write that can still reject (clipboard permission, or
              // no `navigator.clipboard` at all outside a secure context), and a
              // false "Copied" is worse than no feedback: the user pastes stale
              // content believing this one is on the clipboard.
              onClick={() => {
                setCopyFailed(false)
                // The shared helper guards a missing Clipboard API and falls back
                // to `execCommand`, resolving `false` (never rejecting) when both
                // fail — so a plain-HTTP dashboard still copies where a direct
                // `navigator.clipboard.writeText` would not exist at all.
                copyToClipboard(data.origin).then(ok => {
                  if (ok) setCopied(true); else setCopyFailed(true)
                })
              }}
              aria-label={i18nT('pages.settings.securityPanel.tailnet_copy_origin')}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied
                ? i18nT('pages.settings.securityPanel.tailnet_copied')
                : i18nT('pages.settings.securityPanel.tailnet_copy')}
            </Btn>
            {/* An anchor, not a Btn: opening a URL is navigation, so it must be
                middle-clickable and reachable by a screen reader as a link.
                Btn's own classes are reused so the pair still reads as one
                control group. */}
            <a
              href={data.origin}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border bg-transparent text-[13px] text-muted no-underline font-body transition-all hover:text-text hover:border-border-strong hover:bg-bg-hover"
            >
              <ExternalLink size={12} />
              {i18nT('pages.settings.securityPanel.tailnet_open')}
            </a>
          </div>
        </div>
      )}

      {/* `unresolved`: on, but nothing was trusted. Says exactly that and no
          more — the endpoint deliberately ships no daemon-state field, so there
          is nothing here about whether tailscaled is running. */}
      {state === 'unresolved' && (
        <div className="text-[12px] text-warn mt-2 flex items-start gap-1.5 leading-relaxed">
          <AlertTriangle size={13} className="shrink-0 mt-0.5" />
          <span>{i18nT('pages.settings.securityPanel.tailnet_unresolved_note')}</span>
        </div>
      )}

      {/* The pin caveat is a real limitation of the shipped feature, not a
          footnote: behind `tailscale serve` every request arrives from
          127.0.0.1, so the per-device session pin has nothing to bind to and a
          dashboard link becomes a transferable bearer credential for the whole
          tailnet. Stated plainly, in the state where it actually applies. */}
      {state === 'active' && (
        <div className="text-[12px] text-warn mt-2 flex items-start gap-1.5 leading-relaxed">
          <ShieldAlert size={13} className="shrink-0 mt-0.5" />
          <span>
            {i18nT('pages.settings.securityPanel.tailnet_pin_caveat')}{' '}
            <a href={`${CODE_BASE}/issues/1762`} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
              {i18nT('pages.settings.securityPanel.tailnet_pin_caveat_link')}
            </a>
          </span>
        </div>
      )}

      {pinned && (
        <p className="text-[12px] text-muted mt-2 leading-relaxed">
          {i18nT('privacyDisclosure.governanceOverrideNote')}
        </p>
      )}

      {!pinned && (
        <div className="text-[11px] text-muted mt-2 leading-relaxed">
          {i18nT('pages.settings.securityPanel.tailnet_restart_note')}
        </div>
      )}

      {/* Switch-only card: nothing to lose on either notice. The origin is
          `select-all` above, so a failed copy still has a manual path. */}
      {copyFailed && (
        <ErrorNotice
          variant="inline"
          className="mt-1.5"
          message={i18nT('pages.settings.securityPanel.tailnet_copy_failed')}
          askAgent
          onDismiss={() => setCopyFailed(false)}
        />
      )}
      {save.isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.third_party_apps_save_failed')} askAgent />
      )}
    </SettingsCard>
  )
}
