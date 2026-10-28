/**
 * "Update your Slack app": what the Slack Code switches change in the Slack app
 * manifest, whether the installed app already carries it, and how to apply it.
 *
 * Slack has no safe way for us to push a manifest into an existing app (see the
 * RFC: `apps.manifest.update` replaces the WHOLE app configuration and needs a
 * workspace-wide configuration token), so the flow is copy → paste into the
 * app's App Manifest page → reinstall → Check again.
 */
import { useCallback, useEffect, useState } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, CheckCircle2, ExternalLink, RefreshCw } from 'lucide-react'
import { Badge, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { api, type SlackConfigData, type SlackManifestChange, type SlackManifestStatus } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'
import type { SlackCodeDraft } from './SlackCodePanel'
import { SLACK_CODE_SWITCHES, SLACK_CODE_SWITCH_LABEL, isSlackCodeSwitch } from './slackCodeSwitches'

const SLACK_APPS = 'https://api.slack.com/apps'
// Every switch this panel lists, reads or diffs comes from the registry
// (`slackCodeSwitches`): a newly registered switch shows up here with no change.
const FEATURES = SLACK_CODE_SWITCHES

const STATUS_KEY: Record<string, [string, 'ok' | 'warn' | 'err' | 'muted']> = {
  up_to_date: ['pages.settings.slackPanel.slackCode.status_up_to_date', 'ok'],
  needs_update: ['pages.settings.slackPanel.slackCode.status_needs_update', 'warn'],
  feature_disabled: ['pages.settings.slackPanel.slackCode.status_feature_disabled', 'warn'],
  unknown: ['pages.settings.slackPanel.slackCode.status_unknown', 'muted'],
  not_configured: ['pages.settings.slackPanel.slackCode.status_not_configured', 'muted'],
  token_rejected: ['pages.settings.slackPanel.slackCode.status_token_rejected', 'err'],
  token_type: ['pages.settings.slackPanel.slackCode.status_token_type', 'err'],
}

/** Readings whose fix is a different bot token, not an app update. */
const TOKEN_ERRORS = new Set(['token_rejected', 'token_type'])

const STATUS_DETAIL_KEY: Record<string, string> = {
  up_to_date: 'pages.settings.slackPanel.slackCode.detail_up_to_date',
  needs_update: 'pages.settings.slackPanel.slackCode.detail_needs_update',
  feature_disabled: 'pages.settings.slackPanel.slackCode.detail_feature_disabled',
  unknown: 'pages.settings.slackPanel.slackCode.detail_unknown',
  not_configured: 'pages.settings.slackPanel.slackCode.detail_not_configured',
  token_rejected: 'pages.settings.slackPanel.slackCode.detail_token_rejected',
  token_type: 'pages.settings.slackPanel.slackCode.detail_token_type',
}

/** A switch's name for a feature the server reports, or the raw name for one
 *  this build does not register (an older or newer gateway). */
function featureLabel(f: string): string {
  return isSlackCodeSwitch(f) ? i18nT(SLACK_CODE_SWITCH_LABEL[f]) : f
}

/** Install statuses an update + reinstall can fix. A reading in which Slack
 *  answered without the scopes only might be: its steps wait behind "Show
 *  update steps", so a one-way step is never asked for beside "cannot tell
 *  whether it is current". */
const STEPS_HELP = new Set(['needs_update', 'unknown'])

/** Install statuses whose reading includes the app's granted scopes. */
const SCOPES_KNOWN = new Set(['up_to_date', 'needs_update', 'feature_disabled'])

function StatusPill({ status }: { status: string }) {
  const [key, variant] = STATUS_KEY[status] ?? STATUS_KEY.unknown
  return (
    <Badge variant={variant} data-testid="slack-manifest-status" data-status={status}>
      {i18nT(key)}
    </Badge>
  )
}

/** A monospace manifest line. ``add``: saving or updating adds it (``missing``
 *  when Slack confirms the install lacks it); ``have``: Slack confirms the
 *  install holds it; ``line``: in the manifest, which Slack cannot confirm
 *  (events, feature blocks, scopes it did not report); ``remove``: held but no
 *  longer asked for. */
function Chip({ text, kind, missing }: { text: string; kind: 'add' | 'have' | 'line' | 'remove'; missing?: boolean }) {
  const tone = kind === 'remove'
    ? 'border-border text-muted line-through'
    : missing ? 'border-warn text-warn' : 'border-border text-text'
  const sign = kind === 'add' ? '+ ' : kind === 'remove' ? '− ' : ''
  return (
    <span className={`inline-flex flex-wrap items-center gap-x-1 max-w-full min-w-0 rounded border bg-bg px-1.5 py-0.5 text-[11.5px] font-mono ${tone}`}>
      {kind === 'have' && <Check size={11} className="text-ok flex-none" aria-hidden />}
      <span className="min-w-0 [overflow-wrap:anywhere]">{sign}{text}</span>
      {missing && <span className="font-sans text-[10.5px]">· {i18nT('pages.settings.slackPanel.slackCode.missing')}</span>}
    </span>
  )
}

/** The manifest lines one switch adds, grouped by kind. *pending* = the switch
 *  is not saved yet, so every line is still to be added; otherwise a scope Slack
 *  reports as granted reads as held. */
function ChangeLines({ change, missing, scopesKnown, pending }: {
  change: SlackManifestChange; missing: Set<string>; scopesKnown: boolean; pending: boolean
}) {
  const rows: [string, string[], boolean][] = [
    ['pages.settings.slackPanel.slackCode.lines_features', change.added_features.map(f => `features.${f}`), false],
    ['pages.settings.slackPanel.slackCode.lines_scopes', change.added_scopes, true],
    ['pages.settings.slackPanel.slackCode.lines_events', change.added_events, false],
  ]
  const kindFor = (item: string, isScope: boolean): 'add' | 'have' | 'line' => {
    // "+" only for what will be added: an unsaved switch's lines, or a scope
    // Slack confirms the install lacks. A scope Slack did not report is a
    // plain line, neither held nor missing.
    if (pending || missing.has(item)) return 'add'
    if (isScope && scopesKnown) return 'have'
    return 'line'
  }
  return (
    <div className="flex flex-col gap-1.5">
      {rows.filter(([, items]) => items.length > 0).map(([labelKey, items, isScope]) => (
        // Label above the chips on a narrow screen, beside them from sm up; the
        // chip column may shrink, so a long line wraps instead of overflowing.
        <div key={labelKey} className="flex flex-col gap-0.5 sm:flex-row sm:items-start sm:gap-2" data-testid="slack-manifest-lines">
          <span className="text-[11px] text-muted sm:w-[64px] sm:flex-none sm:pt-0.5">{i18nT(labelKey)}</span>
          <div className="flex flex-wrap gap-1 min-w-0">
            {items.map(item => <Chip key={item} text={item} kind={kindFor(item, isScope)} missing={missing.has(item)} />)}
          </div>
        </div>
      ))}
    </div>
  )
}

/** Which install the cached readings are about, per query client: bumped
 *  whenever they are forgotten, so a Check again started before that cannot
 *  write the previous app's reading back when it lands. */
const installGeneration = new WeakMap<QueryClient, number>()
const generationOf = (qc: QueryClient) => installGeneration.get(qc) ?? 0

/** Forget everything read about the installed app: its status reading, the
 *  leftover scopes confirmed from earlier readings and a failed Check again.
 *  For when the bot token changes, which may point at a different app; the
 *  reading is then re-fetched from scratch rather than leaving the old app's
 *  reading in place. */
export function forgetSlackInstallReading(qc: QueryClient): Promise<void> {
  installGeneration.set(qc, generationOf(qc) + 1)
  qc.setQueryData(RECHECK_FAILED_KEY, null)
  void qc.resetQueries({ queryKey: LEFTOVER_KEY })
  return qc.resetQueries({ queryKey: ['slack-manifest-status'] })
}

/** The install reading, shared by this panel and the capability lines under
 *  each switch (one query key, so both read one fetch). */
export function useSlackManifestStatus() {
  return useQuery<SlackManifestStatus>({
    queryKey: ['slack-manifest-status'],
    queryFn: () => api.getSlackManifestStatus(false),
    retry: false,
    refetchOnWindowFocus: false,
  })
}

/** One shared empty list, so a reading without leftovers keeps one identity
 *  across renders. */
const NO_SCOPES: string[] = []

/** The leftover scopes the last reading that read the install's scopes found.
 *  Kept in the query cache rather than component state because a failed
 *  Check again replaces the cached install reading, and leaving the page and
 *  coming back must not forget what that failure could not re-read. Written
 *  only by the panel, never fetched. */
const LEFTOVER_KEY = ['slack-manifest-leftover-scopes']
/** Leftovers are what the install holds beyond what the SAVED switches need,
 *  so a list is only true for the switch set it was read under. */
type Leftover = { saved: string; scopes: string[] }
const NO_LEFTOVER: Leftover = { saved: '', scopes: NO_SCOPES }
function useConfirmedLeftover(saved: string): string[] {
  const { data } = useQuery<Leftover>({
    queryKey: LEFTOVER_KEY,
    queryFn: () => NO_LEFTOVER,
    initialData: NO_LEFTOVER,
    staleTime: Infinity,
    gcTime: Infinity,
  })
  return data.saved === saved ? data.scopes : NO_SCOPES
}

/** Which reading (its ``dataUpdatedAt``) a failed Check again was meant to
 *  replace. In the query cache, not component state, so the switch notes above
 *  this panel read the same answer; a newer reading clears it on its own, since
 *  a failed check must not keep a good reading hidden. Never fetched. */
const RECHECK_FAILED_KEY = ['slack-manifest-recheck-failed']
export function useInstallRecheckFailed(dataUpdatedAt: number): [boolean, (failed: boolean) => void] {
  const qc = useQueryClient()
  const { data } = useQuery<number | null>({
    queryKey: RECHECK_FAILED_KEY,
    queryFn: () => null,
    initialData: null,
    staleTime: Infinity,
    gcTime: Infinity,
  })
  const set = useCallback((failed: boolean) => {
    qc.setQueryData<number | null>(RECHECK_FAILED_KEY, failed ? qc.getQueryState(['slack-manifest-status'])?.dataUpdatedAt ?? 0 : null)
  }, [qc])
  return [data !== null && data === dataUpdatedAt, set]
}

/** A Slack call that failed outright (network, Slack error) rather than one
 *  that answered without the scope header: a failure, so it is an error. */
function checkFailed(status: SlackManifestStatus | undefined): boolean {
  const install = status?.install
  return !!install && install.status === 'unknown' && !!install.reason && install.reason !== 'scopes_unreadable'
}

export function SlackManifestUpdate({ data, draft, manifestText, onSave, saving = false }: {
  data: SlackConfigData
  draft: SlackCodeDraft
  manifestText: string | undefined
  /** The page's save, shown beside the pending note so the step after a flip
   *  is right here; omitted on a read-only session. */
  onSave?: () => void
  saving?: boolean
}) {
  const statusQ = useSlackManifestStatus()
  const [checking, setChecking] = useState(false)
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  // The user asked for the update steps while Slack would not report the scopes.
  const [stepsAnyway, setStepsAnyway] = useState(false)

  const qc = useQueryClient()
  const [recheckFailed, setRecheckFailed] = useInstallRecheckFailed(statusQ.dataUpdatedAt)
  // "Check again" bypasses the server's short cache: it is what a user presses
  // right after reinstalling, when a cached "Needs update" would be wrong.
  const recheck = useCallback(async () => {
    setChecking(true)
    setRecheckFailed(false)
    const generation = generationOf(qc)
    try {
      const reading = await api.getSlackManifestStatus(true)
      // The token changed while this was in flight: it read the old app.
      if (generationOf(qc) !== generation) return
      qc.setQueryData(['slack-manifest-status'], reading)
      // The check just re-read the install's scopes, which the capability line
      // under each switch (from the config query) reports: refresh it too, or
      // it keeps saying "Not active yet" after a successful reinstall.
      await qc.invalidateQueries({ queryKey: ['slack-config'] })
      // A switch changed elsewhere (the CLI) also changes the YAML to copy.
      await qc.invalidateQueries({ queryKey: ['slack-manifest'] })
    } catch {
      // A failed read of the old app says nothing about the new one.
      if (generationOf(qc) === generation) setRecheckFailed(true)
    } finally {
      setChecking(false)
    }
  }, [qc, setRecheckFailed])

  const copy = useCallback(async () => {
    if (!manifestText) return
    setCopyFailed(false)
    let ok = false
    try { ok = await copyToClipboard(manifestText) } catch { ok = false }
    if (!ok) { setCopyFailed(true); return }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [manifestText])

  // Only a body that carries an install reading counts; anything else reads as
  // "could not check" rather than throwing.
  const status = statusQ.data?.install ? statusQ.data : undefined
  const savedOn = FEATURES.filter(f => !!data[f])
  const draftOn = FEATURES.filter(f => draft.switches[f])
  const pendingOn = draftOn.filter(f => !savedOn.includes(f))
  const pendingOff = savedOn.filter(f => !draftOn.includes(f))
  const pending = pendingOn.length > 0 || pendingOff.length > 0
  // Only a reading that read the install's scopes says which are left over. A
  // failed one (Slack unreachable, token rejected, scopes unreadable, the
  // request itself) reports none, but that means "not known", not "none
  // left". Keep the last such reading's leftovers so a failed Check again
  // keeps the panel, its error and its retry on screen; only a reading that
  // read the scopes and found none left hides it.
  const scopesRead = !!status && !statusQ.isError && !recheckFailed && SCOPES_KNOWN.has(status.install.status)
  const savedKey = savedOn.join(',')
  const confirmed = useConfirmedLeftover(savedKey)
  const goodExtra = scopesRead ? status.install.extra_scopes ?? NO_SCOPES : null
  // Tagged with the switches the server read it under (its `features`), not
  // the ones saved now: right after a save the cached reading is still the
  // old set's until the refetch lands.
  const readUnder = scopesRead ? FEATURES.filter(f => status.features?.includes(f)).join(',') : ''
  useEffect(() => {
    if (goodExtra) qc.setQueryData<Leftover>(LEFTOVER_KEY, { saved: readUnder, scopes: goodExtra })
  }, [goodExtra, readUnder, qc])
  // A reading for another switch set measured its leftovers against that
  // set's needs, so on screen it counts only for the switches saved now.
  const leftover = goodExtra && readUnder === savedKey ? goodExtra : confirmed
  // The install reading speaks for the saved switches or for leftover scopes.
  const readingShown = savedOn.length > 0 || leftover.length > 0
  if (!status && !statusQ.isError) return null
  if (savedOn.length === 0 && pendingOn.length === 0 && leftover.length === 0) return null

  // What the install holds is claimed only from a reading taken for the
  // switches saved now, by a check that did not fail. After a save the cached
  // reading is the old switch set's until the refetch lands, and a failed
  // refetch keeps it: trusting it would say "Up to date" and mark scopes the
  // new set needs as held. Such a reading shows as unverified instead.
  const current = !!status && !statusQ.isError && !recheckFailed
    && FEATURES.filter(f => status.features?.includes(f)).join(',') === savedKey
  // Not current only because the refetch for the new switch set is still in
  // flight: that is a check under way, not a failure, so no pill and no update
  // steps until it lands.
  const settling = !current && !!status && !statusQ.isError && !recheckFailed && statusQ.isFetching
  const reading = current ? status : undefined
  // Slack refusing the bot token's TYPE for a saved switch is something only the
  // runtime sees (no install reading reports it). The panel states it, with
  // which token to paste, instead of "Up to date" beside a feature that cannot
  // run: one verdict on the page, and the line under the switch stays silent.
  const tokenTypeRefused = savedOn.some(f => data[`${f}_capability`]?.reason === 'not_allowed_token_type')
  const installStatus = tokenTypeRefused ? 'token_type' : reading?.install.status ?? 'unknown'
  const missing = new Set(reading?.install.missing_scopes ?? [])
  // The app's own manifest page only from a reading whose check did not fail:
  // a failed refetch keeps the previous reading, which may be another app's
  // (the bot token was just replaced), so fall back to the Slack apps list.
  const appManifestUrl = status && !statusQ.isError && !recheckFailed ? status.app_manifest_url : null
  const manifestUrl = appManifestUrl || SLACK_APPS
  const detailKey = STATUS_DETAIL_KEY[installStatus] ?? STATUS_DETAIL_KEY.unknown
  // Only a workspace without the feature is beyond a manifest update. An app
  // whose permissions are up to date may still lack an event subscription
  // (Slack does not report those, and native Stop needs one), so Copy and Open
  // stay offered for it.
  const noUpdate = current && !pending && savedOn.length > 0 && leftover.length === 0
    && installStatus === 'feature_disabled'
  // Agent sessions' permanent-step warning whenever the update carries it: a
  // granted assistant:write does not prove the agent view was saved (the older
  // assistant view holds that scope too), and Slack reports no features.
  const agentSessionsWarning = savedOn.includes('agent_sessions')
  // Slack answered without the app's scopes, so whether an update is needed is
  // not known. Check again comes first; the steps, their one-way warning and the
  // manifest to copy wait until the user asks for them.
  const scopesUnreported = current && installStatus === 'unknown' && !checkFailed(reading)
    && !pending && !settling && savedOn.length > 0
  const stepsHeld = scopesUnreported && !stepsAnyway
  const stepsShown = !pending && !settling && !checkFailed(reading) && STEPS_HELP.has(installStatus)
    && !stepsHeld

  return (
    <div className="border-t border-border mt-4 pt-4 flex flex-col gap-3" data-testid="slack-manifest-update">
      {/* role="status": a re-check's outcome (the pill and the line under it)
          is announced to a screen reader without moving focus. */}
      <div role="status" className="flex flex-col gap-3">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.slackPanel.slackCode.update_title')}</span>
        {/* While a switch change is unsaved the install reading is about the
            saved manifest, not the one on screen, so the pill says so. */}
        {pending
          ? <Badge variant="warn" data-testid="slack-manifest-status" data-status="pending">{i18nT('pages.settings.slackPanel.slackCode.status_pending')}</Badge>
          : readingShown && !settling ? <StatusPill status={installStatus} /> : null}
        {/* Its own row from the copy/open pair below: the re-check reads the
            install, the pair below acts on the manifest. */}
        <Btn onClick={() => { void recheck() }} disabled={checking} aria-busy={checking} className="ml-auto">
          <RefreshCw size={13} className={checking ? 'animate-spin' : ''} /> {i18nT('pages.settings.slackPanel.slackCode.check_again')}
        </Btn>
      </div>
      {/* A rejected token is a failure, so it goes through the error surface.
          No hand-off: the token fields and the unsaved Slack draft sit above on
          this panel, and the hand-off navigates away, which would discard them;
          the fix is pasting a token right there. */}
      {readingShown && TOKEN_ERRORS.has(installStatus) && (
        <ErrorNotice variant="inline" message={i18nT(detailKey)} />
      )}
      {/* A failed Slack call is an error too, retried in place with Check
          again; same no-hand-off reason as the token case above. */}
      {readingShown && checkFailed(reading) && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.detail_check_failed')} />
      )}
      {/* While a change is unsaved, the pending note below speaks instead:
          this reading is about the saved manifest, not the one on screen. */}
      {/* Leftover scopes alone get the line only when it explains a reading
          that could not be made; a good reading's box below says the rest.
          A reading that is not current gets no line: its status would be
          the old switch set's, and a failed check has its own notice. */}
      {current && (savedOn.length > 0 || (readingShown && installStatus === 'unknown')) && !pending && !TOKEN_ERRORS.has(installStatus) && !checkFailed(reading) && (
        <p className="text-[12px] text-muted m-0">
          {i18nT(detailKey, {
            features: (reading?.install.disabled_features ?? [])
              .map(featureLabel)
              .join(', '),
          })}
        </p>
      )}
      </div>
      {/* No hand-off: the check is retried in place with Check again, and the
          hand-off would navigate away from the unsaved Slack draft. */}
      {(statusQ.isError || recheckFailed) && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.status_load_failed')} />
      )}

      {pending && (
        <div className="rounded-md border border-warn/40 bg-bg px-3 py-2 flex items-center gap-3 flex-wrap" data-testid="slack-manifest-pending">
          <span className="text-[12px] text-warn flex-1 min-w-[200px]">{i18nT('pages.settings.slackPanel.slackCode.pending_save')}</span>
          {onSave && (
            <Btn primary onClick={onSave} disabled={saving} aria-busy={saving}>
              {saving ? i18nT('pages.settings.slackPanel.saving') : i18nT('pages.settings.slackPanel.save_slack_settings')}
            </Btn>
          )}
        </div>
      )}

      {/* A fixed feature order, so a box never moves when a switch flips. */}
      {status && FEATURES.filter(f => (savedOn.includes(f) || pendingOn.includes(f)) && !!status.feature_changes?.[f]).map(f => (
        <div key={f} className="rounded-md border border-border bg-bg-elevated px-3 py-2.5 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-semibold text-text">{featureLabel(f)}</span>
            {pendingOn.includes(f) && <span className="text-[11px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.after_save')}</span>}
          </div>
          <ChangeLines
            change={status.feature_changes[f]}
            missing={missing}
            scopesKnown={SCOPES_KNOWN.has(installStatus)}
            pending={pendingOn.includes(f)}
          />
        </div>
      ))}

      {leftover.length > 0 && (
        <div className="rounded-md border border-border bg-bg-elevated px-3 py-2.5 flex flex-col gap-2">
          <span className="text-[12px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.extra_scopes')}</span>
          <div className="flex flex-wrap gap-1">{leftover.map(s => <Chip key={s} text={s} kind="remove" />)}</div>
        </div>
      )}

      {/* The update steps only where an update is what helps: a missing scope
          or an install whose scopes Slack would not report, and only once saved.
          Not after a Slack call that failed: the error and Check again speak.
          A workspace without the feature, a rejected token or no token at all
          is not fixed by reinstalling, and the line above says what is. */}
      {/* Agent sessions' one-way step is THIS save in Slack, so its warning sits
          with the steps that perform it, not on the switch. */}
      {stepsHeld && (
        <div className="flex items-center gap-3 flex-wrap" data-testid="slack-manifest-unknown-next">
          <span className="text-[12px] text-text flex-1 min-w-[200px]">{i18nT('pages.settings.slackPanel.slackCode.unknown_next')}</span>
          <Btn onClick={() => setStepsAnyway(true)}>{i18nT('pages.settings.slackPanel.slackCode.show_update_steps')}</Btn>
        </div>
      )}
      {stepsShown && agentSessionsWarning && (
        <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2" data-testid="slack-manifest-agent-sessions-warning">
          <AlertTriangle size={12} className="text-warn flex-none mt-0.5" />
          <div className="flex flex-col gap-1 min-w-0">
            <span className="text-[12px] text-warn">{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_manifest_warning')}</span>
            {/* Part of the same point: nothing happens until that save. */}
            <span className="text-[12px] text-text" data-testid="slack-manifest-keeps-working">
              {i18nT('pages.settings.slackPanel.slackCode.update_keeps_working')}
            </span>
          </div>
        </div>
      )}
      {stepsShown && savedOn.length > 0 && (
        <ol className="m-0 pl-5 list-decimal text-[12px] text-text flex flex-col gap-1">
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_copy')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_paste')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_reinstall')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_check')}</li>
        </ol>
      )}
      {/* The steps first, so they read as the safe default; the exception
          after them, scoped in its first words to apps edited by hand. */}
      {/* "The lines above" exist only while the change boxes render. */}
      {stepsShown && savedOn.length > 0 && !!status && (
        <p className="m-0 text-[12px] text-text" data-testid="slack-manifest-replace-warning">
          {i18nT('pages.settings.slackPanel.slackCode.replace_warning')}
        </p>
      )}
      {installStatus === 'up_to_date' && savedOn.length > 0 && !pending && (
        <div className="flex items-center gap-1.5 text-[12px] text-ok">
          <CheckCircle2 size={13} className="flex-none" /> {i18nT('pages.settings.slackPanel.slackCode.up_to_date_events')}
        </div>
      )}

      {/* Nothing to copy or open once Slack confirms no update is needed:
          the app matches, or reinstalling would not help. Leftover scopes
          still offer them, since removing those is done on that page. Nor
          while a switch change is unsaved: the manifest on offer is the SAVED
          switches', so copying it then would hand over the old one. */}
      {!noUpdate && !pending && !stepsHeld && (
      <div className="flex items-center gap-2 flex-wrap">
        <Btn onClick={() => { void copy() }} disabled={!manifestText}>
          {copied ? <><Check size={13} /> {i18nT('pages.settings.slackPanel.copied')}</> : i18nT('pages.settings.slackPanel.copy_manifest_yaml')}
        </Btn>
        <a href={manifestUrl} target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border border-border text-text hover:bg-bg-hover transition-colors">
          {appManifestUrl
            ? i18nT('pages.settings.slackPanel.slackCode.open_app_manifest')
            : i18nT('pages.settings.slackPanel.slackCode.open_slack_apps')}
          <ExternalLink size={13} />
        </a>
      </div>
      )}
      {/* No hand-off: a refused clipboard write is retried in place, and the
          hand-off would navigate away from the unsaved Slack draft. */}
      {copyFailed && !noUpdate && !pending && !stepsHeld && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.copy_manifest_failed')} onDismiss={() => setCopyFailed(false)} />
      )}
      <p className="text-[11.5px] text-muted m-0">{i18nT('pages.settings.slackPanel.slackCode.events_unverifiable')}</p>
    </div>
  )
}
