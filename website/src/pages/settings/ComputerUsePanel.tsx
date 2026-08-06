import { useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { api } from '../../api/client'
import type { ComputerUseConfigData, ComputerUseConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'

const QK = ['computer-use-config']

/** macOS System Settings deep links for the two TCC grants (mirrors the
 *  backend's SETTINGS_URL_* constants). */
const PANE_ACCESSIBILITY = 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
const PANE_SCREEN_RECORDING = 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'

const GRANTED = 'granted'
const MISSING = 'missing'
const DARWIN = 'macos'

/** Catalog KEY for each of the backend's permission tokens. The raw values are wire
 *  vocabulary (`missing`, `unsupported`, `unknown`) and read as jargon in a badge.
 *
 *  Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 *  here would freeze the boot language. The lookup happens in `permLabel()`, which
 *  runs during render. Shaped as a flat `Record` of full literal keys, indexed
 *  inline at the `i18nT()` call, because that is the form
 *  `scripts/check-i18n-keys.mjs` can resolve statically. */
const PERM_LABEL_KEY: Record<string, string> = {
  granted: 'pages.settings.computerUsePanel.granted',
  missing: 'pages.settings.computerUsePanel.not_detected',
  unsupported: 'pages.settings.computerUsePanel.not_applicable',
  unknown: 'pages.settings.computerUsePanel.could_not_check',
}

/** Localised badge text for a permission token. An unmapped value falls back to
 *  ITSELF, so a state a newer backend invents still renders (as the raw token)
 *  instead of blanking the badge.
 *
 *  `hasOwnProperty`, not `in`: the token comes off the wire, so a backend
 *  reporting `toString` would otherwise resolve to an inherited Object.prototype
 *  member and hand a function to i18next. */
function permLabel(state: string): string {
  return Object.prototype.hasOwnProperty.call(PERM_LABEL_KEY, state)
    ? i18nT(PERM_LABEL_KEY[state])
    : state
}

/** Permission states the backend calls TERMINAL: re-probing cannot change them.
 *  `granted` is done; `unsupported` means there is no TCC to grant on this
 *  platform; `unknown` means the probe itself could not run (framework load
 *  failure), which a retry in 5s will not fix. Only `missing` is worth polling —
 *  and even that is not authoritative, since macOS attributes a grant to the
 *  process that launched KiroCrew. */
const TERMINAL_PERM_STATES = new Set([GRANTED, 'unsupported', 'unknown'])

/** Permission poll cadence, and the cap on how long it runs.
 *  Bounded because the poll shells out to a `kirocrew computer doctor --json`
 *  child on every tick: an unbounded poll on a host that never reports `granted`
 *  (the documented-normal case) spawns a subprocess every 5s forever, for as long
 *  as the Settings page stays open. 3 minutes is far longer than the round trip
 *  through System Settings, and the row still updates on any manual refetch. */
const PERM_POLL_MS = 5000
const PERM_POLL_MAX_MS = 180_000

/** Decide the next poll delay from the query state. Exported so the bound is
 *  testable without driving 36 fake-timer ticks through the whole panel. */
export function permissionPollInterval(
  state: string | undefined,
  firstFetchedAt: number,
  now: number,
): number | false {
  if (state === undefined || TERMINAL_PERM_STATES.has(state)) return false
  if (firstFetchedAt > 0 && now - firstFetchedAt >= PERM_POLL_MAX_MS) return false
  return PERM_POLL_MS
}

/** Resolve a numeric draft to the value to PUT, or `null` for "discard the edit".
 *
 *  Exported and pure so the discard rules are asserted directly rather than
 *  through a blur + async-mutation race: an EMPTY (or whitespace-only) field is
 *  a no-op, not a value —
 *  `Number('')` is 0, and clamping 0 to the published range yields the FLOOR, so
 *  select-all-and-retype would transiently save 1 node / 320px.
 */
export function commitNumericDraft(
  raw: string | null,
  current: number,
  bounds: [number, number] | undefined,
): number | null {
  if (raw === null) return null
  const trimmed = raw.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  if (!Number.isInteger(parsed)) return null
  const [low, high] = bounds ?? [parsed, parsed]
  const bounded = Math.min(high, Math.max(low, parsed))
  return bounded === current ? null : bounded
}

/* The panel's long-form copy is NOT held in module-level constants: those are
 * evaluated once at import, which would freeze the boot language. Each string is
 * resolved with `i18nT()` at its single JSX use site, inside the component body,
 * so a language switch re-reads the catalog. */

/** Hand a System Settings deep link to the OS.
 *
 *  MUST be `window.open`, not `window.location.href`. The dashboard renders
 *  inside an instance <iframe> (InstancesViewport), and a FRAME navigation is
 *  governed by the CSP `frame-src` directive, which the dashboard declares as a
 *  loopback/cloudfront allowlist naming no custom scheme — so assigning
 *  `location.href` to an
 *  `x-apple.systempreferences:` URL is refused with ERR_BLOCKED_BY_CSP and the
 *  button is a dead click in the desktop app. `window.open` is a new top-level
 *  request instead: the browser hands it to the OS, and in Electron it reaches
 *  the main process's `setWindowOpenHandler` (see electron/external-scheme.js),
 *  which forwards the allowlisted scheme to `shell.openExternal`.
 *
 *  Exported so the delivery mechanism is asserted directly — a regression back
 *  to `location.href` would otherwise only show up as a dead button in a
 *  packaged build.
 */
export function openSystemSettings(pane: string): void {
  // `noopener` keeps the opened context from retaining a handle on the
  // dashboard window; nothing needs the returned reference.
  window.open(pane, '_blank', 'noopener,noreferrer')
}

/** One advisory permission row: name, state badge, and a grant shortcut.
 *  Local to this panel rather than shared with SecurityPanel's StatusRow — the
 *  two carry different semantics (a permission hint is never a security state)
 *  and coupling them would make one of the two read wrongly. */
function PermRow({ label, state, pane }: { label: string; state: string; pane: string }) {
  const variant = state === GRANTED ? 'ok' : state === MISSING ? 'warn' : 'muted'
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-[13px] text-text">{label}</span>
      <span className="flex items-center gap-2">
        <Badge variant={variant}>{permLabel(state)}</Badge>
        {state !== GRANTED && (
          <Btn onClick={() => openSystemSettings(pane)} aria-label={i18nT('pages.settings.computerUsePanel.open_system_settings_for', { label })}>
            {i18nT('pages.settings.computerUsePanel.open_system_settings')} <ExternalLink className="lucide-inline" />
          </Btn>
        )}
      </span>
    </div>
  )
}

/**
 * Settings → Computer Use.
 *
 * Two shapes:
 *  1. `supported === false` — the platform has no driver. Reason only, no toggle:
 *     offering a switch that cannot do anything is worse than explaining why.
 *  2. Otherwise the opt-in surface: ONE enable, then the display/limit knobs.
 *
 * The primary enable is NOT a config.json field — the server writes it to the
 * keystone `computer_use.json`, which the agent can neither read nor write. That
 * is why this panel is the only way to turn the feature on.
 */
export function ComputerUsePanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  // Numeric fields are edited locally and committed on blur: saving per keystroke
  // would write "1", "12", "120" on the way to 1200 and each of those is a real
  // clamp the server would accept.
  const [draftNodes, setDraftNodes] = useState<string | null>(null)
  const [draftWidth, setDraftWidth] = useState<string | null>(null)
  // Set when the server reports it restarted sessions to apply the enable.
  const [restarted, setRestarted] = useState(false)

  // Mount time, for the poll's wall-clock bound. A ref (not state) so reading it
  // never re-renders and the deadline survives every refetch.
  const mountedAt = useRef(Date.now())

  const cfgQ = useQuery<ComputerUseConfigData>({
    queryKey: QK,
    queryFn: api.getComputerUseConfig,
    // Poll ONLY while a grant is genuinely outstanding, so flipping the switch in
    // System Settings updates the row without a reload — and stop for the states
    // that a retry cannot change (see TERMINAL_PERM_STATES) or once the bound
    // elapses. Each tick spawns a `kirocrew computer doctor --json` child, so an
    // unbounded poll on a host that legitimately never reports `granted` would
    // spawn one every 5s for as long as the page stays open.
    refetchInterval: q =>
      permissionPollInterval(
        q.state.data?.permissions?.accessibility,
        mountedAt.current,
        Date.now(),
      ),
  })

  const saveMut = useMutation({
    mutationFn: (patch: Partial<ComputerUseConfigSave>) => api.saveComputerUseConfig(patch),
    onMutate: async patch => {
      await qc.cancelQueries({ queryKey: QK })
      const prev = qc.getQueryData<ComputerUseConfigData>(QK)
      if (prev) qc.setQueryData<ComputerUseConfigData>(QK, { ...prev, ...patch })
      setRestarted(false)
      return { prev }
    },
    onSuccess: data => {
      // The server restarts sessions when the enable FLIPS, because kiro-cli
      // caches its tool list per session. Say so — an unexplained session reset
      // reads as a crash, and a user who is not told will still wonder why the
      // tools did not show up.
      if ((data?.sessions_reset ?? 0) > 0) setRestarted(true)
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData<ComputerUseConfigData>(QK, ctx.prev)
      setSaveError(i18nT('pages.settings.computerUsePanel.could_not_save_computer_use_settings'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: QK }),
  })

  const cfg = cfgQ.data
  const busy = saveMut.isPending
  const save = (patch: Partial<ComputerUseConfigSave>) => {
    setSaveError('')
    saveMut.mutate(patch)
  }
  // Commit a numeric draft (see commitNumericDraft): clamped to the
  // server-published bound rather than sending an out-of-range value the PUT would
  // 400 on, and an empty / unparseable / unchanged draft is discarded — which
  // snaps the field back to the persisted value.
  const commit = (key: 'max_tree_nodes' | 'screenshot_max_px', raw: string | null, clear: () => void) => {
    clear()
    if (!cfg) return
    const bounded = commitNumericDraft(raw, cfg[key], cfg.limits?.[key])
    if (bounded !== null) save({ [key]: bounded })
  }

  // Platform tag on the section header, shown in EVERY state (loading, error,
  // supported, unsupported). Computer use has a macOS-only driver — the Windows
  // and Linux backends are typed refusals — so the panel says so up front rather
  // than only after the reason text on an unsupported host. On macOS it still
  // reads correctly: it tells the operator this capability does not follow them
  // to another OS.
  const macOnlyBadge = <Badge variant="muted">{i18nT('pages.settings.computerUsePanel.macos_only')}</Badge>

  if (cfgQ.isError) {
    return (
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={macOnlyBadge}>
        <SettingsCard>
          <div className="text-[13px] text-danger">
            {i18nT('pages.settings.computerUsePanel.could_not_load_computer_use_settings')}{' '}
            <Btn onClick={() => cfgQ.refetch()}>{i18nT('pages.settings.computerUsePanel.retry')}</Btn>
          </div>
        </SettingsCard>
      </SettingsSection>
    )
  }

  if (!cfg) {
    return (
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={macOnlyBadge}>
        <SettingsCard><FormSkeleton rows={['toggle', 'field', 'field']} /></SettingsCard>
      </SettingsSection>
    )
  }

  if (!cfg.supported) {
    return (
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={macOnlyBadge}>
        <SettingsCard>
          <div className="text-[13px] text-muted">
            {cfg.reason || i18nT('pages.settings.computerUsePanel.computer_use_not_available', { platform: cfg.platform })}
          </div>
        </SettingsCard>
      </SettingsSection>
    )
  }

  return (
    <>
      <ErrorNotice message={saveError} className="mb-4 animate-rise" />

      {/* A hand-edited keystone whose app lists could not be parsed. The page
          renders anyway — on purpose, because this is the only UI that can repair
          the file — but it must SAY so: the lists below come back empty in this
          state, and an empty allow-list otherwise reads as "no restriction
          configured", which is the opposite of what the operator wrote. */}
      {cfg.policy_error && (
        <div className="mb-4 rounded-lg border border-warn/20 bg-warn/10 p-3 animate-rise">
          <span className="text-[13px] text-text">{i18nT('pages.settings.computerUsePanel.the_app_lists_in_computer_use_json_could_not_be')}</span>
        </div>
      )}

      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={macOnlyBadge}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.computerUsePanel.enable_computer_use')}
            description={i18nT('pages.settings.computerUsePanel.let_the_agent_read_desktop_app_windows_through_a')}
            checked={cfg.enabled}
            onChange={v => save({ enabled: v })}
            disabled={busy}
          />
          <SettingsToggle
            label={i18nT('pages.settings.computerUsePanel.attach_screenshots')}
            description={i18nT('pages.settings.computerUsePanel.also_capture_the_target_window_and_pass_its_file')}
            checked={cfg.attach_screenshot}
            onChange={v => save({ attach_screenshot: v })}
            disabled={busy}
          />
          {/* Cursor Motion is purely visual, so it is shown only where it can
              actually draw (macOS). */}
          {cfg.enabled && cfg.cursor_motion_supported && (
            <SettingsToggle
              label={i18nT('pages.settings.computerUsePanel.show_cursor_motion')}
              description={i18nT('pages.settings.computerUsePanel.draw_a_cursor_that_glides_to_each_target_and_pul')}
              checked={cfg.cursor_motion}
              onChange={v => save({ cursor_motion: v })}
              disabled={busy}
            />
          )}
          {restarted && (
            <div className="pt-1 text-[13px] text-muted animate-rise">{i18nT('pages.settings.computerUsePanel.your_chat_sessions_were_restarted_so_this_takes')}</div>
          )}
          {cfg.enabled && (
            <div className="pt-1 text-[13px] text-muted">{i18nT('pages.settings.computerUsePanel.computer_use_lets_the_agent_read_any_app_window')}</div>
          )}
        </SettingsCard>
      </SettingsSection>

      {cfg.platform === DARWIN && (
        <SettingsSection title={i18nT('pages.settings.computerUsePanel.permissions')}>
          <SettingsCard>
            <div className="flex items-center gap-1.5 pb-1 text-[13px] text-muted">
              <span>{i18nT('pages.settings.computerUsePanel.advisory_only')}</span>
              <InfoTip text={i18nT('pages.settings.computerUsePanel.not_detected_does_not_always_mean_unavailable_ma')} />
            </div>
            <PermRow label={i18nT('pages.settings.computerUsePanel.accessibility')} state={cfg.permissions.accessibility} pane={PANE_ACCESSIBILITY} />
            <PermRow label={i18nT('pages.settings.computerUsePanel.screen_recording')} state={cfg.permissions.screen_recording} pane={PANE_SCREEN_RECORDING} />
            {cfg.permissions.responsible_hint && (
              <div className="pt-1 text-[13px] text-muted">{cfg.permissions.responsible_hint}</div>
            )}
          </SettingsCard>
        </SettingsSection>
      )}

      <SettingsSection title={i18nT('pages.settings.computerUsePanel.limits')}>
        <SettingsCard>
          <div className="pb-2 text-[13px] text-muted">{i18nT('pages.settings.computerUsePanel.how_much_of_a_window_the_agent_reads_at_once_the')}</div>
          <SettingsInput
            label={i18nT('pages.settings.computerUsePanel.max_tree_nodes')}
            aria-label={i18nT('pages.settings.computerUsePanel.max_tree_nodes')}
            description={i18nT('pages.settings.computerUsePanel.how_many_controls_one_window_reading_returns_a_w')}
            type="number"
            value={draftNodes ?? String(cfg.max_tree_nodes)}
            onChange={setDraftNodes}
            onBlur={() => commit('max_tree_nodes', draftNodes, () => setDraftNodes(null))}
            disabled={busy}
          />
          <SettingsInput
            label={i18nT('pages.settings.computerUsePanel.screenshot_width')}
            aria-label={i18nT('pages.settings.computerUsePanel.screenshot_width')}
            description={i18nT('pages.settings.computerUsePanel.longest_edge_of_the_screenshot_in_pixels_smaller')}
            type="number"
            value={draftWidth ?? String(cfg.screenshot_max_px)}
            onChange={setDraftWidth}
            onBlur={() => commit('screenshot_max_px', draftWidth, () => setDraftWidth(null))}
            disabled={busy}
          />
        </SettingsCard>
      </SettingsSection>

    </>
  )
}
