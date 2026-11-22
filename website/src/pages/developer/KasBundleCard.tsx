import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Check, CheckCircle2, Download, Loader2, RotateCcw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api, type KasBundleStatus } from '../../api/client'
import { ACP_BACKEND_KAS } from '../../api/acpBackend'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsLink } from '../../components/SettingsLink'
import { SettingsButtonGroup } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { Btn } from '../../components/ui'
import { useOutageFailure } from '../../hooks/useOutageFailure'
import { fmtBytes, fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { reportForError } from '../../utils/errorReport'
import { isKasEngineHighlight, KAS_BUNDLE_CONFIG_KEY } from './kasEngineLink'
import { KIRO_SIGN_IN_TARGET } from './kiroSignInLink'

export { KAS_BUNDLE_CONFIG_KEY, KAS_ENGINE_SETTING_ID } from './kasEngineLink'

const STATUS_QUERY_KEY = ['kas-bundle'] as const

/** The harness setting, saved with the engine choice for a promoted Kiro CLI pick. */
const ACP_BACKEND_CONFIG_KEY = 'agent.acp_backend'

/** How often the card re-reads status while an install is running. */
const INSTALL_POLL_MS = 1000

/** Settings search and the all-settings link must open KAS before the engine
 *  choice, which lives in its detail, can be highlighted. */
export function KasEngineDeepLink({ onOpen }: { onOpen: () => void }) {
  const [params] = useSearchParams()
  const wanted = isKasEngineHighlight(params.get('highlight'))
  const callback = useRef(onOpen)
  callback.current = onOpen
  useEffect(() => {
    if (wanted) callback.current()
  }, [wanted])
  return null
}

/** The two engine choices, as `engine_choice` names them on `GET /api/kas-bundle`. */
type EngineChoice = 'engine' | 'kiro_cli'

/**
 * Settings > Agent Harness, inside the KAS row: which engine KAS runs on, Kiro
 * Crew's own or kiro-cli.
 *
 * The choice is `agent.kas_bundle` (an ordinary config PATCH): the built-in engine
 * saves `true`, Kiro CLI saves `false`. Unsaved, the control shows what this build
 * prefers (`engine_choice`): a packaged build prefers the built-in engine. The
 * built-in engine can only be chosen once the pinned engine is installed -- the
 * card offers the install instead. Kiro CLI is always allowed, so a broken install
 * never traps anyone on that route.
 *
 * The install runs in the gateway; this card starts it and polls status once a
 * second while it runs. Status is the single authority: nothing here mirrors
 * the server's answer in local state.
 *
 * `agent.kas_bundle` is boot-only (the running KAS process keeps its route until
 * it is replaced), so a saved switch is not yet an applied one. The card says so
 * in its own words after a save -- what restarts and that it has not happened --
 * rather than leaving the restart pill to imply it.
 *
 * `kiroPickRunsOnKas`: the saved harness is a Kiro CLI pick the gateway runs on
 * Kiro Agent. Choosing the built-in engine then saves Kiro Agent too, so the pick
 * does not fall back to the Kiro CLI harness after the restart.
 */
export function KasBundleCard({ kiroPickRunsOnKas = false }: { kiroPickRunsOnKas?: boolean } = {}) {
  const qc = useQueryClient()
  const statusQ = useQuery<KasBundleStatus>({
    queryKey: STATUS_QUERY_KEY,
    queryFn: () => api.kasBundleStatus(),
    retry: false,
    staleTime: 0,
    refetchInterval: query => (query.state.data?.installing ? INSTALL_POLL_MS : false),
  })
  const status = statusQ.data
  // A read that failed because the gateway was down (a restart; the engine
  // notice polls this same entry): the offline state says so, not this card.
  // The card does not poll, so the hook re-reads it once the gateway is back.
  const outageFailure = useOutageFailure(
    statusQ.isError,
    statusQ.errorUpdatedAt,
    () => statusQ.refetch({ cancelRefetch: false }),
  )
  const statusFailed = statusQ.isError && !outageFailure

  const installMut = useMutation({
    mutationFn: () => api.kasBundleInstall(),
    onSuccess: data => qc.setQueryData(STATUS_QUERY_KEY, data),
  })

  // Saved but not applied: true from the first successful save until this card
  // unmounts, because the setting cannot take effect while this gateway runs.
  const [restartPending, setRestartPending] = useState(false)
  const toggleMut = useMutation({
    mutationFn: async (value: boolean) => {
      // A stored Kiro CLI pick reaches Kiro Agent only while the engine is not
      // saved on, so saving it alone would move chats back to Kiro CLI. Save
      // Kiro Agent first, the same save the panel's own notice offers.
      if (value && kiroPickRunsOnKas) await api.patchConfig(ACP_BACKEND_CONFIG_KEY, ACP_BACKEND_KAS)
      return api.patchConfig(KAS_BUNDLE_CONFIG_KEY, value)
    },
    onSuccess: () => {
      setRestartPending(true)
      void qc.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  // The poll stops on its own once the server reports the install finished.
  const installing = status?.installing === true || installMut.isPending

  if (!status) {
    // The same recovery the tab offers when its own config read fails: a read
    // that failed once is not a dead end, and a page reload is not the remedy.
    // Retry stays through an outage: only the alert is the offline state's.
    return statusQ.isError ? (
      <div className="space-y-2" data-testid="kas-bundle-card">
        {statusFailed && (
          <ErrorNotice
            message={i18nT('pages.developer.kasBundleCard.status_unavailable')}
            report={reportForError(statusQ.error)}
            variant="inline"
            askAgent
          />
        )}
        <Btn type="button" onClick={() => void statusQ.refetch()} data-testid="kas-bundle-retry">
          {i18nT('pages.developer.agentBackendTab.retry')}
        </Btn>
      </div>
    ) : null
  }

  const installed = status.installed
  const canTurnOn = installed !== null && !installing
  // An older gateway sends no choice: read the saved switch as the choice.
  const choice: EngineChoice = status.engine_choice ?? (status.enabled ? 'engine' : 'kiro_cli')
  const onEngine = choice === 'engine'
  // The last KAS spawn took Crew's own engine without agent.kas_bundle: kiro-cli's
  // relay cannot run here, or the ACP runtime sharing preview picked it. The build's
  // own preference ('build_default') needs no line: the choice above already says it.
  const automatic = status.route?.automatic === true
  const automaticText = !automatic || status.route?.reason === 'build_default'
    ? ''
    : status.route?.reason === 'chat_runtime_sharing'
      ? i18nT('pages.developer.kasBundleCard.route_auto_sharing_preview')
      : status.route?.reason === 'kiro_cli_signed_out'
      ? i18nT('pages.developer.kasBundleCard.route_auto_kiro_cli_signed_out')
      : i18nT('pages.developer.kasBundleCard.route_auto_no_kiro_cli')
  const progress = status.progress
  const progressText =
    progress.total > 0
      ? i18nT('pages.developer.kasBundleCard.installing_progress', {
          version: status.runs_version,
          percent: fmtPercent(progress.done / progress.total),
          done: fmtBytes(progress.done),
          total: fmtBytes(progress.total),
        })
      : i18nT('pages.developer.kasBundleCard.installing', { version: status.runs_version })

  return (
    <div className="space-y-2" data-testid="kas-bundle-card">
      <div className="flex flex-wrap items-center gap-2">
        {/* busy while its OWN save runs, so the switch keeps keyboard focus
            instead of a native disabled dropping it to the page. The
            can't-turn-on case stays native disabled: the switch is off and the
            engine is not installed yet, which is not the save running. (This
            note sits outside the element on purpose: the settings extractor
            scans the tag's own text to a 4000-char ceiling, and a backtick in
            an inline comment would open a template-string state that hides the
            closing tag and drops the setting from the registry.) */}
        <div className="min-w-0 flex-1">
          <SettingsButtonGroup
            configKey="agent.kas_bundle"
            settingId="agent.engine"
            label={i18nT('pages.developer.kasBundleCard.engine_label')}
            description={i18nT('pages.developer.kasBundleCard.engine_description')}
            value={choice}
            // A tick on the selected option, so the choice reads without the fill.
            options={[
              {
                value: 'engine',
                label: i18nT('pages.developer.kasBundleCard.engine_builtin'),
                disabled: !onEngine && !canTurnOn,
                icon: onEngine ? <Check className="lucide-inline" aria-hidden="true" data-testid="kas-engine-tick" /> : undefined,
              },
              {
                value: 'kiro_cli',
                label: i18nT('pages.developer.kasBundleCard.engine_kiro_cli'),
                icon: onEngine ? undefined : <Check className="lucide-inline" aria-hidden="true" data-testid="kas-engine-tick" />,
              },
            ]}
            // Not disabled while its own save runs: a native disabled would drop the
            // pressed button's keyboard focus to the page. A second press is ignored,
            // and so is a press on the option already SAVED; one only shown by the
            // build's pick still saves, so "Kiro CLI" can be kept for good.
            onChange={value => {
              if (toggleMut.isPending) return
              if (value === choice && status.choice_saved !== false) return
              toggleMut.mutate(value === 'engine')
            }}
          />
        </div>
        {/* Its own notice below says what is pending once a save lands, so the
            generic pill is hidden then. Hidden in place, not unmounted: the row
            keeps its width and the switch does not move under the pointer. */}
        <span className={restartPending ? 'invisible' : undefined} aria-hidden={restartPending || undefined} data-testid="kas-bundle-restart-pill">
          <SchemaRestartBadge configKey={KAS_BUNDLE_CONFIG_KEY} />
        </span>
      </div>

      {/* What the restart does, beside the choice. The pending notice below says
          it once a save lands, so this line steps aside then. */}
      {!restartPending && (
        <div className="text-[12px] text-muted" data-testid="kas-bundle-restart-note">
          {i18nT('pages.developer.kasBundleCard.engine_restart')}
        </div>
      )}

      {automatic && automaticText && (
        <div className="text-[12px] text-muted" role="status" data-testid="kas-bundle-route-auto">
          {automaticText}
        </div>
      )}

      {/* Nothing saved and the build prefers the engine, but chats run on kiro-cli
          until Kiro Crew is signed in: say why the recommended option is not chosen. */}
      {!status.choice_saved && status.sign_in_moves_to_engine === true && !onEngine && (
        <div className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted" role="status" data-testid="kas-bundle-sign-in-reason">
          <span>{i18nT('pages.developer.kasBundleCard.engine_sign_in_reason')}</span>
          {/* The same way to the sign-in as the row's account line: the Kiro
              account tab, whose card shows the sign-in choices in place. */}
          <SettingsLink
            {...KIRO_SIGN_IN_TARGET}
            data-testid="kas-bundle-sign-in-link"
            className="inline-flex items-center gap-1 text-accent hover:underline focus-ring-accent"
          >
            {i18nT('components.kiroSignInPrompt.sign_in')}
            <ArrowRight className="lucide-inline" aria-hidden="true" />
          </SettingsLink>
        </div>
      )}

      {restartPending && (
        <div
          className="flex items-center gap-2 text-[12px] text-warn"
          role="status"
          data-testid="kas-bundle-restart-pending"
        >
          <RotateCcw className="lucide-inline" aria-hidden="true" />
          <span>{i18nT('pages.developer.kasBundleCard.saved_restart_pending')}</span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-[12px]" data-testid="kas-bundle-state">
        {installing ? (
          <>
            <Loader2 className="lucide-inline animate-spin text-muted" aria-hidden="true" />
            <span role="status" aria-live="polite">{progressText}</span>
          </>
        ) : installed ? (
          <>
            {/* Green only while the engine is what runs: under an OFF switch, or
                after a save the gateway has not restarted onto yet, a green tick
                says "running" before it is true. */}
            <CheckCircle2
              className={`lucide-inline ${onEngine && !restartPending ? 'text-ok' : 'text-muted'}`}
              aria-hidden="true"
              data-testid="kas-bundle-installed-tick"
            />
            <span>
              {installed.bundled
                ? i18nT('pages.developer.kasBundleCard.installed_bundled', {
                    version: installed.version,
                  })
                : i18nT('pages.developer.kasBundleCard.installed', { version: installed.version })}
            </span>
          </>
        ) : status.host_unsupported ? (
          // Every install here is refused, so the reason replaces the button.
          // Once one has been refused the failure is the ErrorNotice's below,
          // so the row stays empty rather than say it twice.
          status.error ? null : (
            <span className="text-muted" data-testid="kas-bundle-host-unsupported">
              {status.host_unsupported}
            </span>
          )
        ) : (
          <>
            <span className="text-muted">
              {i18nT('pages.developer.kasBundleCard.not_installed', { version: status.runs_version })}
            </span>
            <Btn type="button" primary onClick={() => installMut.mutate()} data-testid="kas-bundle-install">
              <Download className="lucide-inline" aria-hidden="true" />
              {i18nT('pages.developer.kasBundleCard.install')}
            </Btn>
          </>
        )}
      </div>

      {/* No hand-off draft to lose: the install and the switch are both server state.
          A failed re-read comes first: with an earlier status still cached, the
          lines above are that stale answer, and this notice is what says so. */}
      <ErrorNotice
        message={
          statusFailed
            ? i18nT('pages.developer.kasBundleCard.status_unavailable')
            : status.error
              ? status.error
              : installMut.isError
                ? i18nT('pages.developer.kasBundleCard.install_failed')
                : toggleMut.isError
                  ? i18nT('pages.developer.kasBundleCard.save_failed')
                  : undefined
        }
        // The same order as the message, so the report is the shown failure's own.
        report={
          statusFailed
            ? reportForError(statusQ.error)
            : status.error
              ? undefined
              : installMut.isError
                ? reportForError(installMut.error)
                : toggleMut.isError
                  ? reportForError(toggleMut.error)
                  : undefined
        }
        variant="inline"
        askAgent
      />
    </div>
  )
}
