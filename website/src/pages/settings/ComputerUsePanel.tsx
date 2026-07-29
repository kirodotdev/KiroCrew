import { useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { api } from '../../api/client'
import type { ComputerUseConfigData, ComputerUseConfigSave } from '../../api/client'

const QK = ['computer-use-config']

/** macOS System Settings deep links for the two TCC grants (mirrors the
 *  backend's SETTINGS_URL_* constants). */
const PANE_ACCESSIBILITY = 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
const PANE_SCREEN_RECORDING = 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'

const GRANTED = 'granted'
const MISSING = 'missing'
const DARWIN = 'macos'

/** Human labels for the backend's permission tokens. The raw values are wire
 *  vocabulary (`missing`, `unsupported`, `unknown`) and read as jargon in a badge;
 *  an unmapped value falls back to itself so a new backend state still renders. */
const PERM_LABELS: Record<string, string> = {
  granted: 'Granted',
  missing: 'Not detected',
  unsupported: 'Not applicable',
  unknown: 'Could not check',
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
 *  through a blur + async-mutation race, which is what let the empty-field bug
 *  hide: an EMPTY (or whitespace-only) field is a no-op, not a value —
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

const ADVISORY = 'Not detected does not always mean unavailable — macOS attributes a permission grant to the process that launched KiroCrew, not to KiroCrew itself.'
const LIMITS_INTRO = 'How much of a window the agent reads at once. These are cost and speed dials, not security settings — every value stays within a built-in ceiling. Leave them alone unless a window is too large to read in one pass, or screenshots feel slow.'
const NODES_DESC = 'How many controls one window reading returns. A window with more than this gets truncated, and the agent is told so. Raise it for dense apps (a spreadsheet, an IDE); lower it to spend fewer tokens per reading.'
const WIDTH_DESC = 'Longest edge of the screenshot, in pixels. Smaller is cheaper and faster to read; larger keeps small text legible if the agent has to fall back to the image.'
const UNBOUNDED = 'Computer use lets the agent read any app window and send clicks and keystrokes into it. Most clicks are delivered straight to the app and leave your pointer alone, but the agent can ask to move the real pointer when a target needs it. Password fields are never read; nothing else limits which apps it may drive.'
const CURSOR_MOTION_DESC = 'Draw a cursor that glides to each target and pulses when it clicks, so you can follow along on screen. Purely visual — it changes nothing about what the agent is allowed to do, and it only appears for clicks that move the real pointer.'
const RESTARTED = 'Your chat sessions were restarted so this takes effect right away — Kiro reads its tool list once per session, so an open chat would not have picked it up otherwise. Your messages are still there; the agent re-reads its context on your next message.'
/** Shown when the keystone file's app lists could not be parsed. Names the file so
 *  the fix is actionable, and says what the agent does meanwhile — an unexplained
 *  warning here would leave the operator unsure whether the feature is safe to use. */
const POLICY_UNREADABLE = 'The app lists in computer_use.json could not be read, so they are shown empty here. The agent still refuses every action that depends on them until the file is valid JSON.'

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
        <Badge variant={variant}>{PERM_LABELS[state] ?? state}</Badge>
        {state !== GRANTED && (
          <Btn onClick={() => { window.location.href = pane }} aria-label={`Open System Settings for ${label}`}>
            Open System Settings <ExternalLink className="lucide-inline" />
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
      setSaveError('Could not save computer-use settings.')
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

  if (cfgQ.isError) {
    return (
      <SettingsSection title="Computer Use">
        <SettingsCard>
          <div className="text-[13px] text-danger">
            Could not load computer-use settings.{' '}
            <Btn onClick={() => cfgQ.refetch()}>Retry</Btn>
          </div>
        </SettingsCard>
      </SettingsSection>
    )
  }

  if (!cfg) {
    return (
      <SettingsSection title="Computer Use">
        <SettingsCard><FormSkeleton rows={['toggle', 'field', 'field']} /></SettingsCard>
      </SettingsSection>
    )
  }

  if (!cfg.supported) {
    return (
      <SettingsSection title="Computer Use">
        <SettingsCard>
          <div className="text-[13px] text-muted">
            {cfg.reason || `Computer use is not available on ${cfg.platform}.`}
          </div>
        </SettingsCard>
      </SettingsSection>
    )
  }

  return (
    <>
      {saveError && (
        <div className="mb-4 rounded-lg border border-danger/20 bg-danger/10 p-3 animate-rise">
          <span className="text-[13px] text-danger">{saveError}</span>
        </div>
      )}

      {/* A hand-edited keystone whose app lists could not be parsed. The page
          renders anyway — on purpose, because this is the only UI that can repair
          the file — but it must SAY so: the lists below come back empty in this
          state, and an empty allow-list otherwise reads as "no restriction
          configured", which is the opposite of what the operator wrote. */}
      {cfg.policy_error && (
        <div className="mb-4 rounded-lg border border-warn/20 bg-warn/10 p-3 animate-rise">
          <span className="text-[13px] text-text">{POLICY_UNREADABLE}</span>
        </div>
      )}

      <SettingsSection title="Computer Use">
        <SettingsCard>
          <SettingsToggle
            label="Enable computer use"
            description="Let the agent read desktop app windows through accessibility and act on their controls. Off until you turn it on here — an agent cannot enable it."
            checked={cfg.enabled}
            onChange={v => save({ enabled: v })}
            disabled={busy}
          />
          <SettingsToggle
            label="Attach screenshots"
            description="Also capture the target window and pass its file path. The accessibility tree stays the primary channel; windows containing a password field are never captured."
            checked={cfg.attach_screenshot}
            onChange={v => save({ attach_screenshot: v })}
            disabled={busy}
          />
          {/* Cursor Motion is purely visual, so it is shown only where it can
              actually draw (macOS). */}
          {cfg.enabled && cfg.cursor_motion_supported && (
            <SettingsToggle
              label="Show cursor motion"
              description={CURSOR_MOTION_DESC}
              checked={cfg.cursor_motion}
              onChange={v => save({ cursor_motion: v })}
              disabled={busy}
            />
          )}
          {restarted && (
            <div className="pt-1 text-[13px] text-muted animate-rise">{RESTARTED}</div>
          )}
          {cfg.enabled && (
            <div className="pt-1 text-[13px] text-muted">{UNBOUNDED}</div>
          )}
        </SettingsCard>
      </SettingsSection>

      {cfg.platform === DARWIN && (
        <SettingsSection title="Permissions">
          <SettingsCard>
            <div className="flex items-center gap-1.5 pb-1 text-[13px] text-muted">
              <span>Advisory only</span>
              <InfoTip text={ADVISORY} />
            </div>
            <PermRow label="Accessibility" state={cfg.permissions.accessibility} pane={PANE_ACCESSIBILITY} />
            <PermRow label="Screen Recording" state={cfg.permissions.screen_recording} pane={PANE_SCREEN_RECORDING} />
            {cfg.permissions.responsible_hint && (
              <div className="pt-1 text-[13px] text-muted">{cfg.permissions.responsible_hint}</div>
            )}
          </SettingsCard>
        </SettingsSection>
      )}

      <SettingsSection title="Limits">
        <SettingsCard>
          <div className="pb-2 text-[13px] text-muted">{LIMITS_INTRO}</div>
          <SettingsInput
            label="Max tree nodes"
            aria-label="Max tree nodes"
            description={NODES_DESC}
            type="number"
            value={draftNodes ?? String(cfg.max_tree_nodes)}
            onChange={setDraftNodes}
            onBlur={() => commit('max_tree_nodes', draftNodes, () => setDraftNodes(null))}
            disabled={busy}
          />
          <SettingsInput
            label="Screenshot width"
            aria-label="Screenshot width"
            description={WIDTH_DESC}
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
