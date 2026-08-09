import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Check, AlertTriangle } from 'lucide-react'
import Modal from '../../components/Modal'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
type GatewayStatus = { enabled: boolean; apps_enabled: boolean; running: boolean; ping_ok: boolean; supported: boolean }

type Phase = 'idle' | 'confirm' | 'applying' | 'done' | 'failed'

/** Presentation state of the MCP Apps render switch.
 *
 * Pure so the rule is testable without rendering.
 *
 * Settable even while the broker is off, which is the point: this is an OPT-OUT
 * of executing server-authored UI, and `apps_enabled` defaults on, so gating it
 * behind a running broker would force a cautious user to enable the broker first
 * — exposing themselves to the capability — and then race to switch it off. The
 * endpoint writes config only and needs no broker, so recording the preference
 * early is both possible and the safer order. `needsGateway` drives a separate
 * line explaining that the stored choice is inert until the broker runs.
 *
 * There is no per-state description: the label describes what the switch
 * CONTROLS, not what is currently happening. A present-tense "renders in chat"
 * is false whenever the broker is off, and this control exists precisely to
 * answer "is this rendering?" — so it must not be the thing that misreports it.
 */
export function mcpAppsSwitchState(s: {
  gatewayEnabled: boolean
  appsEnabled: boolean
  loading: boolean
  busy: boolean
}): { checked: boolean; disabled: boolean; needsGateway: boolean } {
  return {
    checked: s.appsEnabled,
    disabled: s.loading || s.busy,
    needsGateway: !s.gatewayEnabled,
  }
}

// Stable id so the pre-click consequence can be tied into the MCP Apps switch's
// own accessible description via aria-describedby. There is one instance of this
// card per page, so a constant is safe and keeps the id readable in the DOM.
const APPS_WARN_ID = 'mcp-apps-will-start-gateway'

export function SharedMcpGatewayToggle() {
  const qc = useQueryClient()
  const statusQ = useQuery<GatewayStatus>({ queryKey: ['mcpGatewayStatus'], queryFn: () => api.mcpGatewayStatus() })
  // Same query key the poolable-servers card below uses, so this shares its cache
  // rather than issuing a second request. Needed because MCP Apps renders through
  // the stub, and a server only gets a stub when it is poolable — so with an empty
  // allowlist the feature is on and still cannot render anything.
  const serversQ = useQuery<{ servers: { name: string; poolable: boolean }[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers() as never,
  })
  // Array-checked rather than `data?.servers.filter(...)`: optional-chaining `data`
  // does not protect the property below it, so any response without a `servers`
  // array (older backend, error envelope, partial payload) threw and took the whole
  // MCP Pool page down with it. Undefined here means "not known yet" — neither the
  // empty-allowlist pointer nor the closure line claims anything on an unknown count.
  const poolableCount = Array.isArray(serversQ.data?.servers)
    ? serversQ.data.servers.filter(s => s.poolable).length
    : undefined
  const enabled = statusQ.data?.enabled ?? false
  const pingOk = statusQ.data?.ping_ok ?? false
  // Default true so a still-loading status (or an older backend that predates
  // the field) never disables the control; only a definite `false` gates it.
  const supported = statusQ.data?.supported ?? true

  const [phase, setPhase] = useState<Phase>('idle')
  const [target, setTarget] = useState(false)
  const busy = phase === 'applying'

  // Optimistic value held only for the duration of the request. The status query
  // is the source of truth, so the override is DROPPED once the refetch lands —
  // holding it indefinitely would pin this tab to its own last write and hide a
  // change made anywhere else.
  const [appsPending, setAppsPending] = useState<boolean | null>(null)
  const [appsBusy, setAppsBusy] = useState(false)
  const [appsError, setAppsError] = useState<string | null>(null)
  const [appsApplied, setAppsApplied] = useState(false)
  // True only while the gateway follow is in flight, so the row can say what the
  // click set in motion. The follow restarts active sessions; with no confirm
  // step this line is the only thing on screen that explains why.
  const [followBusy, setFollowBusy] = useState(false)
  // Scopes the self-clearing below to the FOLLOW's error only. A config-write
  // refusal (env-override / overlay-owned 409) is not cured by the broker coming
  // up, so clearing every appsError on ping_ok would erase an instruction the user
  // still needs.
  const [followFailed, setFollowFailed] = useState(false)
  // True after a follow that actually brought the broker up, so the closure line
  // can say what happened. `mcp_apps_applies_to_new` is wrong there: it says
  // "ones already running pick it up when they recycle", but the follow just
  // restarted them — they picked it up now, not later.
  const [followStarted, setFollowStarted] = useState(false)
  const appsEnabled = appsPending ?? statusQ.data?.apps_enabled ?? true
  const appsState = mcpAppsSwitchState({
    gatewayEnabled: enabled,
    appsEnabled,
    loading: statusQ.isLoading,
    busy: appsBusy,
  })

  // The one state where flipping the switch has a side effect beyond the config
  // write: MCP Apps off, broker down, and a platform that can actually run it.
  const willStartGateway = appsState.needsGateway && !followBusy && supported && !appsState.checked

  // The gateway follow: same POST the gateway switch uses, without its modal
  // state machine. Failure surfaces on the MCP Apps error line rather than in a
  // dialog, because the apps write already succeeded — the user's stored choice
  // is intact and only the convenience follow fell through.
  // Retire the follow's error once the broker is actually up. The user can bring
  // it up by the gateway's own toggle, which would otherwise leave a red "did not
  // come up" line on this row directly beneath a gateway row reporting Active.
  useEffect(() => {
    if (pingOk && followFailed) {
      setFollowFailed(false)
      setAppsError(null)
    }
  }, [pingOk, followFailed])

  const runGatewayFollow = async () => {
    setFollowBusy(true)
    try {
      const r = await api.mcpGatewayEnable(true)
      if (!r.ping_ok) {
        setFollowFailed(true)
        setAppsError(i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_gateway_follow_failed'))
      } else {
        setFollowStarted(true)
      }
    } catch {
      setFollowFailed(true)
      setAppsError(i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_gateway_follow_failed'))
    } finally {
      // Refetch on EVERY outcome, not just success. A failed enable still leaves
      // config at enabled=true with the broker unreachable, so skipping the
      // refetch left the gateway row rendering its stale OFF state next to an
      // error telling the user to turn it on — two adjacent instructions that
      // disagree. Invalidating lets the gateway row show its own authoritative
      // "enabled, broker not reachable" recovery text.
      await qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      setFollowBusy(false)
    }
  }

  const runApps = async (next: boolean) => {
    setAppsBusy(true)
    setAppsError(null)
    setAppsApplied(false)
    // Must reset with it: `followStarted` decides which closure line renders, and a
    // stale true made the NEXT toggle claim "gateway started, sessions restarted"
    // when nothing started — including when that toggle turned MCP Apps OFF.
    setFollowStarted(false)
    setAppsPending(next)
    try {
      const r = await api.mcpGatewayAppsEnable(next)
      // Seed the cache from the RESPONSE before invalidating. Dropping the local
      // override on the way out is only safe if the cache already carries the new
      // value — otherwise a refetch that fails leaves the switch showing the
      // stale cached state while config on disk says otherwise.
      qc.setQueryData(['mcpGatewayStatus'], (prev: GatewayStatus | undefined) =>
        prev ? { ...prev, apps_enabled: r.enabled } : prev)
      await qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      setAppsApplied(true)
      // Turning MCP Apps ON with the broker down would save a preference that
      // renders nothing, so the gateway follows automatically in that one case.
      //
      // PROVISIONAL — delete this follow when stub emission stops depending on the
      // pooling allowlist. MCP Apps needs the gateway only because `rewriter.py`
      // emits a stub solely for poolable servers, and the stub carries the render
      // and callback path. Once the stub is unconditional, MCP Apps no longer needs
      // the broker, and auto-starting it here would restart every active session
      // for a dependency that no longer exists. Tracked in #2374.
      // The follow is deliberately one-directional and one-way-only:
      //   apps ON  + gateway off -> gateway follows on
      //   apps ON  + gateway on  -> nothing to follow
      //   apps OFF + gateway on  -> gateway STAYS ON, never follows off
      // Turning Apps off must not tear down pooling: other consumers depend on
      // it and it is independently valuable, which is the separation this
      // control exists for.
      //
      // Deliberately NOT routed through `run`: that drives the confirm/apply
      // modal, which covers this very card — so the gateway switch would flip
      // behind a dialog and the user would never see the control they just
      // caused to change. The follow is meant to read as the upper switch
      // moving in sympathy, so it applies inline and reports failure on the
      // same error line as the apps write.
      // Only the ON direction does this, and only where the broker can actually
      // run: on Windows the shared gateway needs Unix-domain sockets, so
      // `supported` is false, the gateway toggle is rendered disabled, and firing
      // the follow there would promise a start that cannot happen and then point
      // the user at a control they cannot operate.
      if (next && !enabled && supported) {
        await runGatewayFollow()
      }
    } catch (e) {
      // Prefer the server's message: the refusals this endpoint can return are
      // actionable ("…is set in config.local.json; edit that file instead") and
      // collapsing them into one generic line throws away the only instruction
      // that would let the user fix it. `ApiError.message` carries the response
      // body's `error` prose. Generic text is the fallback, not the default.
      const msg = e instanceof Error ? e.message.trim() : ''
      setAppsError(msg || i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_failed'))
    } finally {
      setAppsPending(null)
      setAppsBusy(false)
    }
  }

  // In-process apply: the POST starts/stops the broker, drops + relinks all
  // agent sessions, and verifies connectivity — no gateway restart, so this
  // dashboard session stays logged in.  The response is the verified state.
  //
  // Stays on this page on success. It used to navigate to Developer > System,
  // which was wrong twice over: enabling the pool is the FIRST half of the job
  // (the user then picks which servers to pool, on this very page), and the
  // destination did not even carry the `plane` the metrics card lives on, so it
  // landed on the Sessions table instead. Reporting the verified state here and
  // letting the user choose where to go next is the honest shape.
  const run = async (next: boolean) => {
    setTarget(next)
    setPhase('applying')
    try {
      const r = await api.mcpGatewayEnable(next)
      const ok = next ? r.ping_ok : !r.running
      if (ok) qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      setPhase(ok ? 'done' : 'failed')
    } catch {
      setPhase('failed')
    }
  }

  const subStatus = !supported ? i18nT('pages.settings.sharedMcpGatewayToggle.not_available_on_windows')
    : !enabled ? i18nT('pages.settings.sharedMcpGatewayToggle.disabled_each_session_spawns_its_own_mcp_backend')
    : pingOk ? i18nT('pages.settings.sharedMcpGatewayToggle.active_sessions_share_pooled_mcp_backends_see_th')
    : i18nT('pages.settings.sharedMcpGatewayToggle.enabled_broker_not_reachable_toggle_off_and_on_t')

  const btn = 'text-[13px] px-3 py-1.5 rounded-md transition-colors cursor-pointer'

  return (
    <SettingsSection title={i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway')}
          description={subStatus}
          checked={enabled}
          disabled={statusQ.isLoading || busy || (!supported && !enabled)}
          onChange={next => { if (!supported && next) return; setTarget(next); setPhase('confirm') }}
        />
      </SettingsCard>

      {/* Render switch for server-authored UI. The config write itself applies
          instantly with no confirm — the broker re-reads the flag per tool result.
          It is NOT true that "nothing restarts", though: turning this ON while the
          broker is down starts the gateway, which drops and relinks every active
          session. That consequence is disclosed before the click and tied into the
          switch's accessible description below. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps')}
          description={i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_capability')}
          checked={appsState.checked}
          disabled={appsState.disabled}
          describedBy={willStartGateway ? APPS_WARN_ID : undefined}
          onChange={next => void runApps(next)}
        />
        {/* Rendered OUTSIDE SettingsToggle: as its description it would inherit a
            disabled row's opacity-40, dimming the line that explains the state.

            Suppressed entirely when the platform cannot run the broker. Both
            branches below talk about the gateway coming on; on Windows it never
            can, and `apps_enabled` defaults ON, so the checked branch is the state
            EVERY Windows user lands in on first visit — "takes effect once the
            shared MCP gateway above is on" directly beneath a permanently disabled
            row reading "Not available on Windows". The gateway row states the
            platform fact once; this row stays quiet.

            Otherwise, two facts chosen by which way the switch would move:
            - OFF: flipping it on ALSO starts the gateway, which restarts active
              sessions. Disclosed here at the point of intent rather than in a
              confirm dialog after the fact — the follow is not gated, so this is
              the informed-consent surface, and it renders at description weight
              with a warning icon rather than as a muted footnote.
            - ON: the stored choice is simply inert until the broker runs (the
              follow failed, or the gateway was turned off separately). Genuinely a
              footnote — nothing is about to happen. */}
        {willStartGateway && (
          <div id={APPS_WARN_ID} className="flex items-start gap-1.5 text-[13px] text-text">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warn" />
            <span>{i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_will_start_gateway')}</span>
          </div>
        )}
        {appsState.needsGateway && !followBusy && supported && appsState.checked && (
          <div className="text-[12px] text-text-muted">
            {i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_needs_gateway')}
          </div>
        )}
        {/* Windows: `supported` suppresses the state line above and `needsGateway`
            suppresses the closure line below, which left EVERY toggle here rendering
            nothing at all — the same "turned it on and nothing happened" silence
            this feature exists to remove. Acknowledge the save and name the reason
            it cannot take effect. */}
        {!supported && appsApplied && !appsError && (
          <div className="text-[12px] text-text-muted">
            {i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_saved_unsupported')}
          </div>
        )}
        {/* Live region: the follow restarts the user's sessions, so its progress and
            outcome must be ANNOUNCED, not only drawn. */}
        <div aria-live="polite">
        {followBusy && (
          <div className="text-[13px] text-text">
            {i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_starting_gateway')}
          </div>
        )}
        {/* Suppressed while the broker is off: "ones already running pick it up
            when they recycle" describes connections that do not exist yet, and it
            would stack a second "Saved…" line against `needsGateway` giving two
            divergent explanations of the same state. `needsGateway` is the one
            that is true and actionable there.

            With the broker UP and MCP Apps on, an EMPTY poolable allowlist is its
            own dead end: the render path runs through the stub, and a server only
            gets a stub when it is poolable, so nothing can render no matter what
            this switch says. `poolable_servers` defaults to an empty list, so that
            is the out-of-the-box state — exactly the first-time user this feature
            targets. Pointing at the list below closes the funnel; a "Saved."
            closure there would repeat the "turned it on and nothing happened"
            failure one prerequisite down. */}
        {/* Order matters: the restart acknowledgement comes FIRST, then the pointer
            at what is still missing. A successful follow restarted the user's
            sessions — exactly what the pre-click warning promised — so it gets an
            acknowledgement even when the allowlist is empty and nothing can render
            yet. Suppressing it there left the promised restart unconfirmed. */}
        {appsApplied && !appsError && !appsState.needsGateway
          && (followStarted || poolableCount !== 0) && (
          <div className="text-[12px] text-text-muted">
            {followStarted
              ? i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_gateway_started')
              : i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_applies_to_new')}
          </div>
        )}
        {appsState.checked && !appsState.needsGateway && poolableCount === 0 && (
          <div className="flex items-start gap-1.5 text-[13px] text-text">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warn" />
            <span>{i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_no_poolable_servers')}</span>
          </div>
        )}
        </div>
        {appsError && (
          <div className="flex items-start gap-1.5 text-[12px] text-danger" aria-live="polite">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <span>{appsError}</span>
            {/* Recovering through the gateway toggle costs a confirm modal plus an
                apply modal, twice (off then on) — six clicks to retry a one-click
                follow. This retries the follow directly. */}
            {followFailed && supported && (
              <button
                className="shrink-0 underline hover:no-underline cursor-pointer"
                onClick={() => { setAppsError(null); setFollowFailed(false); void runGatewayFollow() }}
              >
                {i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_retry_gateway')}
              </button>
            )}
          </div>
        )}
      </SettingsCard>

      {/* Confirm */}
      <Modal
        open={phase === 'confirm'}
        onClose={() => setPhase('idle')}
        title={target ? i18nT('pages.settings.sharedMcpGatewayToggle.enable_shared_mcp_gateway') : i18nT('pages.settings.sharedMcpGatewayToggle.disable_shared_mcp_gateway')}
        maxWidth={460}
        footer={<>
          <button className={`${btn} border border-border text-text hover:bg-bg-hover`} onClick={() => setPhase('idle')}>{i18nT('pages.settings.sharedMcpGatewayToggle.cancel')}</button>
          <button className={`${btn} bg-accent text-accent-fg hover:bg-accent-hover`} onClick={() => run(target)}>{i18nT('pages.settings.sharedMcpGatewayToggle.continue')}</button>
        </>}
      >
        <div className="text-[13px] text-text">{i18nT('pages.settings.sharedMcpGatewayToggle.this_restarts_all_active_sessions_onto_the_new_m')}</div>
      </Modal>

      {/* Applying + terminal states */}
      <Modal
        open={busy || phase === 'done' || phase === 'failed'}
        onClose={() => { if (!busy) setPhase('idle') }}
        title={phase === 'done' ? i18nT('pages.settings.sharedMcpGatewayToggle.done') : phase === 'failed' ? i18nT('pages.settings.sharedMcpGatewayToggle.could_not_apply') : (target ? i18nT('pages.settings.sharedMcpGatewayToggle.enabling_shared_mcp_gateway') : i18nT('pages.settings.sharedMcpGatewayToggle.disabling_shared_mcp_gateway'))}
        maxWidth={460}
        footer={phase === 'done' ? (
          <button className={`${btn} bg-accent text-accent-fg hover:bg-accent-hover`} onClick={() => setPhase('idle')}>{i18nT('pages.settings.sharedMcpGatewayToggle.close')}</button>
        ) : phase === 'failed' ? (<>
          <button className={`${btn} border border-border text-text hover:bg-bg-hover`} onClick={() => setPhase('idle')}>{i18nT('pages.settings.sharedMcpGatewayToggle.close')}</button>
          {target && <button className={`${btn} bg-danger text-white hover:opacity-90`} onClick={() => run(false)}>{i18nT('pages.settings.sharedMcpGatewayToggle.roll_back_disable')}</button>}
        </>) : undefined}
      >
        {phase === 'done' ? (
          <div className="flex items-center gap-2 text-[13px] text-text">
            <Check size={16} className="text-ok" />
            {target ? i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway_is_active') : i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway_is_disabled')}
          </div>
        ) : phase === 'failed' ? (
          <div className="flex items-start gap-2 text-[13px] text-text">
            <AlertTriangle size={16} className="text-danger mt-0.5 shrink-0" />
            <span>{target
              ? i18nT('pages.settings.sharedMcpGatewayToggle.gateway_stuck_roll_back')
              : i18nT('pages.settings.sharedMcpGatewayToggle.gateway_stuck_retry')}</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-[13px] text-text">
            <Loader2 size={16} className="text-accent animate-spin shrink-0" />
            {target ? i18nT('pages.settings.sharedMcpGatewayToggle.starting_broker_restarting_sessions_verifying_co') : i18nT('pages.settings.sharedMcpGatewayToggle.stopping_broker_and_restarting_sessions')}
          </div>
        )}
      </Modal>
    </SettingsSection>
  )
}
