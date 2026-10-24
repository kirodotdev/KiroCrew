/**
 * "Update your Slack app": what the Slack Code switches change in the Slack app
 * manifest, whether the installed app already carries it, and how to apply it.
 *
 * Slack has no safe way for us to push a manifest into an existing app (see the
 * RFC: `apps.manifest.update` replaces the WHOLE app configuration and needs a
 * workspace-wide configuration token), so the flow is copy → paste into the
 * app's App Manifest page → reinstall → Check again.
 */
import { useCallback, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CheckCircle2, ExternalLink, RefreshCw } from 'lucide-react'
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
}

const STATUS_DETAIL_KEY: Record<string, string> = {
  up_to_date: 'pages.settings.slackPanel.slackCode.detail_up_to_date',
  needs_update: 'pages.settings.slackPanel.slackCode.detail_needs_update',
  feature_disabled: 'pages.settings.slackPanel.slackCode.detail_feature_disabled',
  unknown: 'pages.settings.slackPanel.slackCode.detail_unknown',
  not_configured: 'pages.settings.slackPanel.slackCode.detail_not_configured',
  token_rejected: 'pages.settings.slackPanel.slackCode.detail_token_rejected',
}

/** A switch's name for a feature the server reports, or the raw name for one
 *  this build does not register (an older or newer gateway). */
function featureLabel(f: string): string {
  return isSlackCodeSwitch(f) ? i18nT(SLACK_CODE_SWITCH_LABEL[f]) : f
}

/** Install statuses an update + reinstall can fix. */
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
    <span className={`inline-flex items-center gap-1 rounded border bg-bg px-1.5 py-0.5 text-[11.5px] font-mono ${tone}`}>
      {kind === 'have' && <Check size={11} className="text-ok flex-none" aria-hidden />}
      {sign}{text}
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
        <div key={labelKey} className="flex items-start gap-2">
          <span className="text-[11px] text-muted w-[64px] flex-none pt-0.5">{i18nT(labelKey)}</span>
          <div className="flex flex-wrap gap-1">
            {items.map(item => <Chip key={item} text={item} kind={kindFor(item, isScope)} missing={missing.has(item)} />)}
          </div>
        </div>
      ))}
    </div>
  )
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

  const qc = useQueryClient()
  const [recheckFailed, setRecheckFailed] = useState(false)
  // "Check again" bypasses the server's short cache: it is what a user presses
  // right after reinstalling, when a cached "Needs update" would be wrong.
  const recheck = useCallback(async () => {
    setChecking(true)
    setRecheckFailed(false)
    try {
      qc.setQueryData(['slack-manifest-status'], await api.getSlackManifestStatus(true))
      // The check just re-read the install's scopes, which the capability line
      // under each switch (from the config query) reports: refresh it too, or
      // it keeps saying "Not active yet" after a successful reinstall.
      await qc.invalidateQueries({ queryKey: ['slack-config'] })
      // A switch changed elsewhere (the CLI) also changes the YAML to copy.
      await qc.invalidateQueries({ queryKey: ['slack-manifest'] })
    } catch {
      setRecheckFailed(true)
    } finally {
      setChecking(false)
    }
  }, [qc])

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
  const extra = status?.install.extra_scopes ?? []
  if (!status && !statusQ.isError) return null
  if (savedOn.length === 0 && pendingOn.length === 0 && extra.length === 0) return null

  const installStatus = status?.install.status ?? 'unknown'
  const missing = new Set(status?.install.missing_scopes ?? [])
  const manifestUrl = status?.app_manifest_url || SLACK_APPS
  const detailKey = STATUS_DETAIL_KEY[installStatus] ?? STATUS_DETAIL_KEY.unknown

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
          : savedOn.length > 0 || extra.length > 0 ? <StatusPill status={installStatus} /> : null}
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
      {savedOn.length > 0 && installStatus === 'token_rejected' && (
        <ErrorNotice variant="inline" message={i18nT(detailKey)} />
      )}
      {/* A failed Slack call is an error too, retried in place with Check
          again; same no-hand-off reason as the token case above. */}
      {savedOn.length > 0 && checkFailed(status) && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.detail_check_failed')} />
      )}
      {/* While a change is unsaved, the pending note below speaks instead:
          this reading is about the saved manifest, not the one on screen. */}
      {savedOn.length > 0 && !pending && installStatus !== 'token_rejected' && !checkFailed(status) && (
        <p className="text-[12px] text-muted m-0">
          {i18nT(detailKey, {
            features: (status?.install.disabled_features ?? [])
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

      {extra.length > 0 && (
        <div className="rounded-md border border-border bg-bg-elevated px-3 py-2.5 flex flex-col gap-2">
          <span className="text-[12px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.extra_scopes')}</span>
          <div className="flex flex-wrap gap-1">{extra.map(s => <Chip key={s} text={s} kind="remove" />)}</div>
        </div>
      )}

      {/* The update steps only where an update is what helps: a missing scope
          or an install whose scopes Slack would not report, and only once saved.
          A workspace without the feature, a rejected token or no token at all
          is not fixed by reinstalling, and the line above says what is. */}
      {/* The exception before the steps, so the steps read as the safe
          default rather than a risk discovered after reading them. */}
      {!pending && STEPS_HELP.has(installStatus) && savedOn.length > 0 && (
        <p className="m-0 text-[12px] text-text" data-testid="slack-manifest-replace-warning">
          {i18nT('pages.settings.slackPanel.slackCode.replace_warning')}
        </p>
      )}
      {!pending && STEPS_HELP.has(installStatus) && savedOn.length > 0 && (
        <ol className="m-0 pl-5 list-decimal text-[12px] text-text flex flex-col gap-1">
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_copy')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_paste')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_reinstall')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_check')}</li>
        </ol>
      )}
      {installStatus === 'up_to_date' && savedOn.length > 0 && !pending && (
        <div className="flex items-center gap-1.5 text-[12px] text-ok">
          <CheckCircle2 size={13} /> {i18nT('pages.settings.slackPanel.slackCode.nothing_to_do')}
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <Btn onClick={() => { void copy() }} disabled={!manifestText}>
          {copied ? <><Check size={13} /> {i18nT('pages.settings.slackPanel.copied')}</> : i18nT('pages.settings.slackPanel.copy_manifest_yaml')}
        </Btn>
        <a href={manifestUrl} target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border border-border text-text hover:bg-bg-hover transition-colors">
          {status?.app_manifest_url
            ? i18nT('pages.settings.slackPanel.slackCode.open_app_manifest')
            : i18nT('pages.settings.slackPanel.slackCode.open_slack_apps')}
          <ExternalLink size={13} />
        </a>
      </div>
      {/* No hand-off: a refused clipboard write is retried in place, and the
          hand-off would navigate away from the unsaved Slack draft. */}
      {copyFailed && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.copy_manifest_failed')} onDismiss={() => setCopyFailed(false)} />
      )}
      <p className="text-[11.5px] text-muted m-0">{i18nT('pages.settings.slackPanel.slackCode.events_unverifiable')}</p>
    </div>
  )
}
