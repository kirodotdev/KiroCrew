import { Suspense, lazy, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Lock } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import { LockedByPolicyBadge } from '../../components/settingRef/LockedByPolicyBadge'
import { api } from '../../api/client'
import type { ComputerUseConfigData, ComputerUseConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import AgentSpecNotRewrittenNotice, { agentSpecNotRewritten } from '../../components/AgentSpecNotRewrittenNotice'
import { ComputerUseSetupStatus } from './ComputerUseSetupStatus'
import { SETUP_QUERY_PARAM } from './computerUseSetup'
import { COMPUTER_USE_SETUP_QK } from '../../hooks/useComputerUseSetup'

// The setup dialog opens only on a click (or ?setup=1), so it is its own chunk
// rather than part of the Settings page bundle.
const ComputerUseSetupDialog = lazy(() =>
  import('./ComputerUseSetupDialog').then(m => ({ default: m.ComputerUseSetupDialog })),
)

const QK = ['computer-use-config']
/** The locked-by-policy note under the enable, which the switch's description points at. */
const LOCKED_NOTE_ID = 'computer-use-locked-note'

const DARWIN = 'macos'
const WINDOWS = 'windows'

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
  // Set when the enable saved but the agent spec was not rewritten.
  const [specNotRewritten, setSpecNotRewritten] = useState(false)
  // The first-use setup dialog. Its progress lives on the server, so closing it
  // loses nothing.
  const [setupOpen, setSetupOpen] = useState(false)
  // `?setup=1` is how the chat's Set up Computer Use button asks for the
  // dialog: open it once on arrival, then drop the parameter (replace, so Back
  // does not reopen it).
  const [searchParams, setSearchParams] = useSearchParams()
  const setupRequested = searchParams.has(SETUP_QUERY_PARAM)
  useEffect(() => {
    if (!setupRequested) return
    setSetupOpen(true)
    const next = new URLSearchParams(searchParams)
    next.delete(SETUP_QUERY_PARAM)
    setSearchParams(next, { replace: true })
  }, [setupRequested, searchParams, setSearchParams])

  const cfgQ = useQuery<ComputerUseConfigData>({
    queryKey: QK,
    queryFn: api.getComputerUseConfig,
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
      // Only a save that changed the enable carries the verdict; any other save
      // leaves the last one standing, since the spec is still what it was.
      if (data && 'agent_spec_rewritten' in data) setSpecNotRewritten(agentSpecNotRewritten(data))
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData<ComputerUseConfigData>(QK, ctx.prev)
      setSaveError(i18nT('pages.settings.computerUsePanel.could_not_save_computer_use_settings'))
    },
    onSettled: (_data, _err, patch) => {
      void qc.invalidateQueries({ queryKey: QK })
      // The setup card reads its own document, which the enable switch changes
      // (off <-> the setup state), so it must be read again too.
      if (patch && 'enabled' in patch) void qc.invalidateQueries({ queryKey: COMPUTER_USE_SETUP_QK })
    },
  })

  const cfg = cfgQ.data
  const busy = saveMut.isPending
  // An LTS build ignores the local opt-in; the enable is shown greyed out.
  const locked = cfg?.locked_by_policy === true
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
  // supported, unsupported), so it states the platform reality up front rather
  // than only in the unsupported-host reason text.
  //
  // Both supported platforms drive the full tool set, but they differ in a way the
  // operator has to know BEFORE enabling: on macOS an element-addressed action is
  // delivered per-process, while Windows has no per-process input at all — so a
  // keystroke there takes the operator's keyboard focus and a coordinate click moves
  // their real cursor. The badge names that, because it is the operator's own
  // machine and nothing else on this panel would tell them.
  //
  // Until `cfg` loads the platform is UNKNOWN, and no badge is shown for it. The
  // macOS-only wording is not a conservative fallback, it is a false statement on two
  // of the three platforms: a Windows operator who hits the error branch would be told
  // the feature is macOS-only and stop, and a Linux one would read the same claim
  // beside a reason that says otherwise. An absent badge withholds a fact; a wrong one
  // ends the operator's attempt. `cfg` is also read in the pre-`cfg` states below,
  // hence the optional chain.
  const platformBadge = (() => {
    if (!cfg?.supported) return undefined
    if (cfg.platform === WINDOWS) {
      return <Badge variant="muted">{i18nT('pages.settings.computerUsePanel.takes_focus_and_cursor')}</Badge>
    }
    if (cfg.platform === DARWIN) {
      return <Badge variant="muted">{i18nT('pages.settings.computerUsePanel.macos_only')}</Badge>
    }
    return undefined
  })()

  if (cfgQ.isError) {
    return (
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={platformBadge}>
        <SettingsCard>
          <div className="flex flex-wrap items-center gap-3">
            <ErrorNotice
              className="flex-1 min-w-[16rem]"
              message={i18nT('pages.settings.computerUsePanel.could_not_load_computer_use_settings')}
              askAgent
            />
            {/* Retry stays beside the notice: a refetch is the cheap first move,
                the hand-off is for when it keeps failing. */}
            <Btn onClick={() => cfgQ.refetch()}>{i18nT('pages.settings.computerUsePanel.retry')}</Btn>
          </div>
        </SettingsCard>
      </SettingsSection>
    )
  }

  if (!cfg) {
    return (
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={platformBadge}>
        <SettingsCard><FormSkeleton rows={['toggle', 'field', 'field']} /></SettingsCard>
      </SettingsSection>
    )
  }

  if (!cfg.supported) {
    return (
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={platformBadge}>
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
      {/* No hand-off: `draftNodes` / `draftWidth` are live numeric drafts. A
          hand-off click blurs the field, which commits the draft and STARTS the
          save — if that save then fails after the navigation has unmounted this
          panel, the typed limit is gone with nothing on screen to say so. */}
      <ErrorNotice message={saveError} className="mb-4 animate-rise" />

      {/* A hand-edited keystone whose app lists could not be parsed. The page
          renders anyway — on purpose, because this is the only UI that can repair
          the file — but it must SAY so: the lists below come back empty in this
          state, and an empty allow-list otherwise reads as "no restriction
          configured", which is the opposite of what the operator wrote.
          No hand-off: the `draftNodes` / `draftWidth` fields below share this
          panel — the navigation would unmount a half-typed limit. */}
      <ErrorNotice
        message={cfg.policy_error ? i18nT('pages.settings.computerUsePanel.the_app_lists_in_computer_use_json_could_not_be') : null}
        className="mb-4 animate-rise"
      />

      {/* One card, like Settings > Browser: the switch, the status in a few
          words and ONE primary action for the current state. */}
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.computer_use')} badge={platformBadge}>
        <SettingsCard>
          <div data-testid="cu-main-card">
            <SettingsToggle
              label={i18nT('pages.settings.computerUsePanel.enable_computer_use')}
              hint={i18nT('pages.settings.computerUsePanel.let_the_agent_read_desktop_app_windows_through_a')}
              checked={cfg.enabled}
              onChange={v => save({ enabled: v })}
              disabled={busy || locked}
              describedBy={locked ? LOCKED_NOTE_ID : undefined}
            />
            {/* Outside the toggle row, so it is not dimmed with the disabled switch. */}
            {locked && (
              <div id={LOCKED_NOTE_ID} className="flex items-center gap-2 text-[12px] text-muted" data-testid="computer-use-locked-note">
                <LockedByPolicyBadge />
                <span>{i18nT('components.lockedByPolicyBadge.tooltip')}</span>
              </div>
            )}
            <ComputerUseSetupStatus part="summary" onOpenSetup={() => setSetupOpen(true)} />
            {/* No hand-off: the limit fields under More hold drafts that commit on blur. */}
            {specNotRewritten && <AgentSpecNotRewrittenNotice className="pt-1" askAgent={false} />}
            {restarted && (
              <div className="pt-1 text-[13px] text-muted animate-rise">
                {specNotRewritten
                  ? i18nT('pages.settings.computerUsePanel.your_chat_sessions_were_restarted')
                  : i18nT('pages.settings.computerUsePanel.your_chat_sessions_were_restarted_so_this_takes')}
              </div>
            )}
          </div>
        </SettingsCard>
      </SettingsSection>
      {setupOpen && (
        <Suspense fallback={null}>
          <ComputerUseSetupDialog open={setupOpen} onOpenChange={setSetupOpen} config={cfg} />
        </Suspense>
      )}

      {/* The one main control an ordinary user changes. */}
      <div className="mt-3">
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.computerUsePanel.attach_screenshots')}
            description={i18nT('pages.settings.computerUsePanel.also_capture_the_target_window_and_pass_its_file_summary')} hint={i18nT('pages.settings.computerUsePanel.also_capture_the_target_window_and_pass_its_file')}
            checked={cfg.attach_screenshot}
            onChange={v => save({ attach_screenshot: v })}
            disabled={busy}
          />
        </SettingsCard>
      </div>

      {/* Everything else, collapsed by default. */}
      <SettingsSection title={i18nT('pages.settings.computerUsePanel.more_title')} collapsible>
        <SettingsCard>
          <div data-testid="cu-more">
            <p className="m-0 pb-2 text-[13px] text-muted">{i18nT('pages.settings.computerUsePanel.computer_use_lets_the_agent_read_any_app_window')}</p>
            {cfg.platform === WINDOWS && (
              <p className="m-0 pb-2 text-[13px] text-muted">{i18nT('pages.settings.computerUsePanel.on_windows_keystrokes_take_your_keyboard_focus')}</p>
            )}
            {/* Purely visual, and only where it can draw (macOS). */}
            {cfg.cursor_motion_supported && (
              <SettingsToggle
                label={i18nT('pages.settings.computerUsePanel.show_cursor_motion')}
                hint={i18nT('pages.settings.computerUsePanel.draw_a_cursor_that_glides_to_each_target_and_pul')}
                checked={cfg.cursor_motion}
                onChange={v => save({ cursor_motion: v })}
                disabled={busy}
              />
            )}
            <SettingsInput
              label={i18nT('pages.settings.computerUsePanel.max_tree_nodes')}
              aria-label={i18nT('pages.settings.computerUsePanel.max_tree_nodes')}
              hint={i18nT('pages.settings.computerUsePanel.how_many_controls_one_window_reading_returns_a_w')}
              type="number"
              value={draftNodes ?? String(cfg.max_tree_nodes)}
              onChange={setDraftNodes}
              onBlur={() => commit('max_tree_nodes', draftNodes, () => setDraftNodes(null))}
              disabled={busy}
            />
            <SettingsInput
              label={i18nT('pages.settings.computerUsePanel.screenshot_width')}
              aria-label={i18nT('pages.settings.computerUsePanel.screenshot_width')}
              hint={i18nT('pages.settings.computerUsePanel.longest_edge_of_the_screenshot_in_pixels_smaller')}
              type="number"
              value={draftWidth ?? String(cfg.screenshot_max_px)}
              onChange={setDraftWidth}
              onBlur={() => commit('screenshot_max_px', draftWidth, () => setDraftWidth(null))}
              disabled={busy}
            />
            <ComputerUseSetupStatus part="details" onOpenSetup={() => setSetupOpen(true)} />
          </div>
        </SettingsCard>
      </SettingsSection>

      <p className="flex items-center gap-1.5 mt-4 mb-0 text-[12px] text-muted" data-testid="cu-privacy-footer">
        <Lock size={12} className="shrink-0" aria-hidden />
        {i18nT('pages.settings.computerUsePanel.privacy_footer')}
      </p>
    </>
  )
}
