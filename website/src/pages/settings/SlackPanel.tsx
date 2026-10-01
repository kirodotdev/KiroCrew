import { useState, useEffect, useCallback, useRef } from 'react'
import { useImeGuard } from '../../hooks/useImeGuard'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Plus, X, Lock, RefreshCw, Loader2 } from 'lucide-react'
import { SlackIcon } from '../../components/SlackIcon'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Input, Btn } from '../../components/ui'
import { api, type SlackConfigData, type SlackConfigSave, type SlackReconnectResult } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { copyToClipboard } from '../../utils/clipboard'

import { i18nT } from '../../i18n/t'
import { ChannelFolderBackfill } from './ChannelFolderBackfill'
import ErrorNotice from '../../components/ErrorNotice'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
/** Brand name — do-not-translate, so it lives here rather than in the catalog. */
const CHANNEL_NAME = "Slack"
const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/slack-integration.md'

type Draft = {
  owner_id: string
  command: string
  allowed_enterprise_ids: string[]
  reactions_enabled: boolean
  show_thinking: boolean
  /** Whether Slack files its sessions in a folder at all (off = unfiled). */
  session_folder_on: boolean
  /** Folder name, kept while the toggle is off so turning it back on restores it. */
  session_folder: string
}

function draftFrom(c: SlackConfigData): Draft {
  return {
    owner_id: c.owner_id,
    command: c.command,
    allowed_enterprise_ids: [...c.allowed_enterprise_ids],
    reactions_enabled: c.reactions_enabled,
    show_thinking: c.show_thinking,
    // A configured name IS the on-state — the backend has one field, where ""
    // means off, so the toggle is derived rather than separately persisted.
    session_folder_on: !!c.session_folder,
    session_folder: c.session_folder ?? '',
  }
}

/** Status pill mirroring the connection state of the messaging gateway. */
function StatusBadge({ config }: { config: SlackConfigData }) {
  const [dot, text, cls] = config.connected
    ? ['var(--ok)', i18nT('pages.settings.slackPanel.connected'), 'text-ok']
    : config.configured
      ? ['var(--warn)', i18nT('pages.settings.slackPanel.not_connected'), 'text-warn']
      : ['var(--muted)', i18nT('pages.settings.slackPanel.needs_setup'), 'text-muted']
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/**
 * Plain-language reading of a `connect_error` code. One mapper for both the
 * connect the gateway recorded at startup (GET) and the outcome of a Reconnect
 * click (POST), so the two never describe the same failure in different words.
 * The named codes are the ones the gateway's reconnect emits itself; anything
 * else is a Slack API error or a network error class name and is quoted.
 */
function describeConnectError(code: string): string {
  switch (code) {
    case 'invalid_auth':
      return i18nT('pages.settings.slackPanel.slack_rejected_the_stored_tokens_invalid_auth_re')
    case 'tokens_missing':
      return i18nT('pages.settings.slackPanel.reconnect_tokens_missing')
    case 'owner_id_missing':
      return i18nT('pages.settings.slackPanel.reconnect_owner_id_missing')
    case 'enterprise_validation_failed':
      return i18nT('pages.settings.slackPanel.reconnect_enterprise_validation_failed')
    case 'denied_by_policy':
      return i18nT('pages.settings.slackPanel.reconnect_denied_by_policy')
    case 'previous_client_close_failed':
      return i18nT('pages.settings.slackPanel.reconnect_previous_client_close_failed')
    case 'workspace_identity_unverified':
      return i18nT('pages.settings.slackPanel.reconnect_workspace_identity_unverified')
    case 'workspace_identity_unrecorded':
      return i18nT('pages.settings.slackPanel.reconnect_workspace_identity_unrecorded')
    case 'workspace_record_unreadable':
      return i18nT('pages.settings.slackPanel.reconnect_workspace_record_unreadable')
    case 'workspace_switch_too_large':
      return i18nT('pages.settings.slackPanel.reconnect_workspace_switch_too_large')
    case 'workspace_switch_unrecordable':
      return i18nT('pages.settings.slackPanel.reconnect_workspace_switch_unrecordable')
    default:
      return i18nT('pages.settings.slackPanel.slack_connection_failed_at_startup', { error: code })
  }
}

/**
 * Did the gateway itself answer this failed request? Its refusals -- 403 from a
 * remote session, 500 when the credential store could not be read, 503 with no
 * Slack socket owner -- are JSON with an `error` (and a `code`), already
 * unwrapped into `message`. A proxy that could not reach the gateway, or a
 * gateway that fell over mid-request, answers with text or HTML instead; the
 * status alone cannot tell the two 5xx apart, the body's shape can.
 */
function gatewayAnswered(e: unknown): e is ApiError {
  if (!(e instanceof ApiError) || !e.message) return false
  try {
    const parsed: unknown = JSON.parse(e.body)
    return typeof parsed === 'object' && parsed !== null && typeof (parsed as { error?: unknown }).error === 'string'
  } catch {
    return false
  }
}

/**
 * The gateway's recorded connect failure (`connect_error`), kept apart from
 * {@link connectionHint}: it is the outcome of something that FAILED, so it
 * renders through `ErrorNotice`, while the hint describes a state that has not
 * gone wrong yet.
 */
function connectError(config: SlackConfigData): string {
  if (config.connected || !config.configured || !config.connect_error) return ''
  return describeConnectError(config.connect_error)
}

/** One-line explanation of WHY Slack is not connected, with the fix. */
function connectionHint(config: SlackConfigData): string {
  // A connect failure is shown by `connectError`; "saved but not yet active"
  // would only repeat it underneath.
  if (config.connected || !config.configured || config.connect_error) return ''
  return i18nT('pages.settings.slackPanel.tokens_are_saved_but_not_yet_active_restart_the')
}

/**
 * The confirmation after a save, naming the step that makes it take effect.
 * A credential/owner write is applied by Reconnect; the slash command is the
 * one field that still needs a gateway restart. Verified-with-Slack saves say
 * so, because the tokens were checked before they were stored.
 */
function savedText(verified: boolean, reconnect: boolean, restart: boolean): string {
  // One save can change a token AND the slash command: both steps are named,
  // or the restart the command still needs would be dropped.
  if (reconnect && restart) return i18nT('pages.settings.slackPanel.saved_reconnect_and_restart')
  if (reconnect) {
    return verified
      ? i18nT('pages.settings.slackPanel.verified_with_slack_and_saved_restart_the_gatewa')
      : i18nT('pages.settings.slackPanel.saved_reconnect_to_apply')
  }
  if (restart) return i18nT('pages.settings.slackPanel.saved_restart_the_gateway_to_apply')
  return verified
    ? i18nT('pages.settings.slackPanel.verified_and_saved')
    : i18nT('pages.settings.slackPanel.saved')
}

/** Editor for a list of plain string IDs (channels, enterprise orgs, user IDs, emails). */
export function TagListEditor({ label, description, values, placeholder, onChange, validate, readOnly }: {
  label: string
  description?: string
  values: string[]
  placeholder: string
  onChange: (next: string[]) => void
  validate?: (v: string) => boolean
  readOnly?: boolean
}) {
  const [draft, setDraft] = useState('')
  const [err, setErr] = useState('')
  const ime = useImeGuard()
  const add = () => {
    const v = draft.trim()
    if (!v) return
    if (validate && !validate(v)) { setErr(i18nT('pages.settings.slackPanel.not_a_valid_id', { name: v })); return }
    if (values.includes(v)) { setDraft(''); return }
    onChange([...values, v])
    setDraft('')
    setErr('')
  }
  return (
    <div data-setting-label={label} className="flex flex-col gap-1.5 py-1.5">
      <span className="text-[13px] font-semibold text-text">{label}</span>
      {description && <div className="text-[12px] text-muted">{description}</div>}
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {values.map(v => (
            <span key={v} className="inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[12px] font-mono text-text">
              {/* break-all, not truncate: these ids are opaque and a user checking
                  one against their console needs every character. A Webex space id
                  is a single unbreakable base64 token, so without this it runs off
                  the card at narrow widths. */}
              <span className="min-w-0 break-all">{v}</span>
              {!readOnly && (
                <button type="button" onClick={() => onChange(values.filter(x => x !== v))}
                  className="shrink-0 text-muted hover:text-danger transition-colors" aria-label={i18nT('pages.settings.slackPanel.remove', { name: v })}>
                  <X size={12} />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      {values.length === 0 && readOnly && <div className="text-[12px] text-muted">{i18nT('pages.settings.slackPanel.none')}</div>}
      {!readOnly && (
        <div className="flex items-center gap-2">
          {/* min-w-0 + flex-1: a `flex-none` input keeps its intrinsic width, which
              pushes the Add button off the card at 320px. Letting the input shrink
              keeps both on one row at every width the repo supports. */}
          <Input value={draft} placeholder={placeholder} className="min-w-0 flex-1 font-mono"
            onChange={e => { setDraft(e.target.value); setErr('') }}
            {...ime.bindEnter({ onEnter: add })} />
          <Btn onClick={add} disabled={!draft.trim()} className="shrink-0"><Plus size={13} /> {i18nT('pages.settings.slackPanel.add')}</Btn>
        </div>
      )}
      {/* Client-side validation only ("X is not a valid ID") — there is nothing
          for the agent to diagnose, so no hand-off. */}
      <ErrorNotice message={err} variant="inline" />
    </div>
  )
}

/** Slack channel-integration settings. */
export function SlackPanel() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<SlackConfigData>({
    queryKey: ['slack-config'],
    queryFn: api.getSlackConfig,
    retry: false,
    // An ambient focus refetch mid-edit would hand back a fresh `data`
    // object and clobber unsaved edits via the sync effect below.
    refetchOnWindowFocus: false,
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [botToken, setBotToken] = useState('')
  const [appToken, setAppToken] = useState('')
  const [botClear, setBotClear] = useState(false)
  const [appClear, setAppClear] = useState(false)
  const [formKey, setFormKey] = useState(0)  // bump to remount secret fields after save
  const [saved, setSaved] = useState(false)
  const [restartHint, setRestartHint] = useState(false)
  // A saved token/owner change is applied by Reconnect, not a restart.
  const [reconnectHint, setReconnectHint] = useState(false)
  // Outcome of the last Reconnect click. Replaces the GET-recorded connect
  // error in the header once set: both describe the same socket, and the click
  // is the newer reading. Cleared when a new save changes what would be applied.
  const [reconnectResult, setReconnectResult] = useState<SlackReconnectResult | null>(null)
  // The request itself failed (gateway unreachable) -- distinct from a
  // reconnect that ran and reported `connected: false`.
  const [reconnectError, setReconnectError] = useState('')
  const [verifyWarning, setVerifyWarning] = useState('')
  const [tokensVerified, setTokensVerified] = useState(false)
  const [manifestCopied, setManifestCopied] = useState(false)
  // Separate from `!manifestCopied`: idle and failed both read as not-copied,
  // but only the failure needs a notice (same split as MobileLoginCard).
  const [manifestCopyFailed, setManifestCopyFailed] = useState(false)

  // Public manifest template + one-click Slack create URL (no secrets).
  const manifestQ = useQuery({
    queryKey: ['slack-manifest'],
    queryFn: api.getSlackManifest,
    staleTime: Infinity,
    retry: false,
  })

  const copyManifest = useCallback(async () => {
    if (!manifestQ.data) return
    setManifestCopyFailed(false)
    // The shared helper, not `navigator.clipboard.writeText` directly: on a
    // plain-HTTP remote dashboard `navigator.clipboard` is undefined, and a
    // direct call throws synchronously before any `.catch()` could attach — so
    // the copy failed AND nothing reported it. The helper guards the API, falls
    // back to `execCommand`, and resolves `false` (or rejects) when both fail.
    let ok = false
    try {
      ok = await copyToClipboard(manifestQ.data.manifest)
    } catch {
      ok = false
    }
    if (!ok) {
      setManifestCopyFailed(true)
      return
    }
    setManifestCopied(true)
    setTimeout(() => setManifestCopied(false), 1500)
  }, [manifestQ.data])
  const [error, setError] = useState('')

  // Sync the local draft when server config arrives. Guarded so only the
  // initial load and post-save invalidation reseed it — a background refetch
  // must not discard in-progress edits (including a just-pasted token).
  const syncArmed = useRef(true)
  useEffect(() => {
    if (data && syncArmed.current) {
      syncArmed.current = false
      setDraft(draftFrom(data))
      setBotToken(''); setAppToken(''); setBotClear(false); setAppClear(false)
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<SlackConfigSave>) => api.saveSlackConfig(body),
    onError: (e: unknown) => {
      // The API client throws with the raw response body; extract the
      // server's error field (e.g. "bot_token rejected by Slack
      // (invalid_auth)") for clean display.
      let msg = i18nT('pages.settings.slackPanel.save_failed_is_the_gateway_running')
      if (e instanceof Error && e.message) {
        try {
          msg = JSON.parse(e.message).error ?? e.message
        } catch {
          msg = e.message
        }
      }
      // Persist until the next save attempt clears it: the rejected draft is
      // still in the form, and a notice that erases itself after a few seconds
      // leaves a quiet form that reads as saved.
      setError(msg)
    },
    onSuccess: (res, vars) => {
      setSaved(true)
      setRestartHint(!!res.restart_required)
      // Accumulates: a later save that touched only config fields must not
      // hide the reminder that credentials saved earlier are still not in
      // effect. Only a successful Reconnect clears it (reconnectMut).
      setReconnectHint(prev => prev || !!res.reconnect_required)
      // The header's reconnect reading described the OLD credentials.
      if (res.reconnect_required) setReconnectResult(null)
      setVerifyWarning(res.verify_warning || '')
      setTokensVerified(!!(vars.bot_token || vars.app_token) && !res.verify_warning)
      syncArmed.current = true
      setFormKey(k => k + 1)
      setTimeout(() => setSaved(false), 6000)
      qc.invalidateQueries({ queryKey: ['slack-config'] })
    },
  })

  const reconnectMut = useMutation({
    mutationFn: () => api.reconnectSlack(),
    onError: (e: unknown) => {
      // A refusal the gateway itself answered carries its own reason and its
      // own remedy (a writable data folder, a gateway that owns a Slack
      // socket); asking whether a gateway that just answered is running would
      // hide both. Only a request that never got the gateway's answer -- a
      // proxy or the gateway fell over -- is the one plain question.
      setReconnectError(gatewayAnswered(e) ? e.message : i18nT('pages.settings.slackPanel.reconnect_failed_is_the_gateway_running'))
    },
    onSuccess: (res) => {
      setReconnectResult(res)
      // The saved credentials are applied; the save row's Reconnect can go.
      if (res.connected) setReconnectHint(false)
      // The badge and the recorded connect_error read from GET: refresh them so
      // the pill flips with the outcome instead of one focus later.
      qc.invalidateQueries({ queryKey: ['slack-config'] })
    },
  })

  const handleReconnect = useCallback(() => {
    setReconnectError('')
    setReconnectResult(null)
    reconnectMut.mutate()
  }, [reconnectMut])

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    const payload: Partial<SlackConfigSave> = {
      owner_id: draft.owner_id.trim(),
      command: draft.command.trim(),
      allowed_enterprise_ids: draft.allowed_enterprise_ids,
      reactions_enabled: draft.reactions_enabled,
      show_thinking: draft.show_thinking,
      // Off sends "" (the field's off-state); on with a blank name falls back
      // to "Slack", which is what the toggle's description promises.
      session_folder: draft.session_folder_on ? (draft.session_folder.trim() || CHANNEL_NAME) : '',
    }
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    if (appClear) payload.app_token_clear = true
    else if (appToken.trim()) payload.app_token = appToken.trim()
    saveMut.mutate(payload)
  }, [draft, botToken, appToken, botClear, appClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.slackPanel.loading_slack_config')}</p>
  // Nothing to lose here: the form is not mounted in this branch.
  if (isError || !data || !draft) return <ErrorNotice className="m-4" message={i18nT('pages.settings.slackPanel.cannot_load_slack_config_is_the_gateway_running')} askAgent />

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only
  // The newest reading wins: a Reconnect outcome supersedes what the gateway
  // recorded at startup (the GET refetch will say the same once it lands).
  // A failed click is labelled as the click's outcome: the reason is often the
  // very text that was on screen before (the same tokens, still rejected), and
  // without the prefix nothing shows that the attempt ran at all.
  // One problem at a time: while a request-level failure is on screen (the
  // gateway never answered), the connect error the gateway recorded earlier
  // is not the thing to fix first, and two notices stacked read as two
  // problems. It comes back when the next click gets an answer. And while a
  // click is in flight the previous failure steps aside too: "Reconnecting…"
  // beside a red "failed" reads as the retry already having failed.
  const startupError = reconnectError || reconnectMut.isPending
    ? ''
    : reconnectResult
      ? (reconnectResult.connected
        ? ''
        : i18nT('pages.settings.slackPanel.reconnect_failed_reason', { reason: describeConnectError(reconnectResult.connect_error) }))
      : connectError(data)
  const hint = reconnectResult ? '' : connectionHint(data)
  const reconnected = !!reconnectResult?.connected
  // One button, rendered twice: beside the status pill, and beside the save
  // confirmation that tells the user to click it (the header may be scrolled
  // off above a long form). Both drive the same in-flight request.
  const reconnectButton = (testId: string) => (
    <Btn
      onClick={handleReconnect}
      disabled={reconnectMut.isPending}
      aria-busy={reconnectMut.isPending || undefined}
      title={i18nT('pages.settings.slackPanel.reconnect_desc')}
      data-testid={testId}
      // Never wraps: beside a long save confirmation the label would otherwise
      // break into a column; the confirmation text is what wraps.
      className="whitespace-nowrap flex-none"
    >
      {/* The two-arrow refresh glyph reads as "try again" at 13px, which is
          what this control does. (A plug was tried and read as a padlock --
          a locked, switched-off control -- to a cold reader.) SecretField's
          restore control beside the token fields uses a single reverse arrow
          with no label; this one is labelled, so the two do not collide. */}
      {reconnectMut.isPending ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
      {reconnectMut.isPending ? i18nT('pages.settings.slackPanel.reconnecting') : i18nT('pages.settings.slackPanel.reconnect')}
    </Btn>
  )

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none">
          <SlackIcon size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">{i18nT('pages.settings.slackPanel.slack')}</h3>
            <StatusBadge config={data} />
            {/* Re-runs the Socket Mode handshake on the saved credentials, in
                place: the way out of "saved but not connected" without a
                gateway restart. Hidden on read-only remote sessions, which the
                backend refuses with 403 for the same reason it refuses the save. */}
            {!ro && reconnectButton('slack-reconnect')}
            {reconnected && (
              <span className="inline-flex items-center gap-1.5 text-[12px] text-ok" role="status">
                <Check size={14} /> {i18nT('pages.settings.slackPanel.connected_to_slack')}
              </span>
            )}
          </div>
          <p className="text-[12px] text-muted mt-1">
            {i18nT('pages.settings.slackPanel.talk_to_your_agents_from_slack_over_socket_mode')}
          </p>
          {/* No hand-off: `botToken` / `appToken` (SecretField drafts, never
              persisted) and the unsaved `draft` (owner id, enterprise allow-list,
              command, session folder) live in this panel's local state —
              navigating to the chat would discard them. */}
          {/* Neither notice is dismissible, like the save's own error below:
              both clear when the next Reconnect click gets an answer, and one
              closable notice next to one that is not reads as two kinds of
              warning. */}
          <ErrorNotice message={startupError} className="mt-2" />
          <ErrorNotice message={reconnectError} className="mt-2" />
          {hint && (
            <p className="text-[12px] text-warn mt-1 flex items-center gap-1.5">
              <AlertTriangle size={12} className="flex-none" />
              {hint}
            </p>
          )}
        </div>
      </div>

      {/* ── Read-only notice (remote session) ── */}
      {ro && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mb-3">
          <Lock size={13} className="text-muted flex-none" />
          <span className="text-[12px] text-muted">
            {i18nT('pages.settings.slackPanel.slack_settings_are_managed_on_the_machine_runnin')}
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title={i18nT('pages.settings.slackPanel.get_your_credentials')}>
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            {i18nT('pages.settings.slackPanel.create_the_slack_app_from_the_manifest', { alias: manifestQ.data?.alias ?? 'you' })}
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href={manifestQ.data?.create_url ?? '#'}
              target="_blank" rel="noopener noreferrer"
              aria-disabled={!manifestQ.data}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all ${manifestQ.data ? 'bg-accent text-accent-fg border-accent hover:bg-accent-hover' : 'border-border text-muted pointer-events-none'}`}
            >
              {i18nT('pages.settings.slackPanel.create_slack_app')} <ExternalLink size={13} />
            </a>
            <Btn onClick={() => { void copyManifest() }} disabled={!manifestQ.data}>
              {manifestCopied ? <><Check size={13} /> {i18nT('pages.settings.slackPanel.copied')}</> : i18nT('pages.settings.slackPanel.copy_manifest_yaml')}
            </Btn>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.slackPanel.setup_guide')} <ExternalLink size={13} />
            </a>
          </div>
          {/* No hand-off for either: the token fields and the unsaved `draft`
              share this panel, and navigating to the chat would discard them. The
              setup-guide link beside them is the recovery path. */}
          {manifestQ.isError && (
            <ErrorNotice variant="inline" className="mt-2" message={i18nT('pages.settings.slackPanel.manifest_unavailable')} />
          )}
          {manifestCopyFailed && (
            <ErrorNotice variant="inline" className="mt-2" message={i18nT('pages.settings.slackPanel.copy_manifest_failed')} onDismiss={() => setManifestCopyFailed(false)} />
          )}
        </SettingsCard>
      </SettingsSection>

      {/* ── Required tokens ── */}
      <SettingsSection title={i18nT('pages.settings.slackPanel.required')}>
        <SettingsCard index={1}>
          <SecretField
            key={`bot-${formKey}`}
            label={i18nT('pages.settings.slackPanel.slack_bot_token')}
            description={i18nT('pages.settings.slackPanel.from_oauth_permissions_after_installing_your_sla')}
            placeholder={i18nT('pages.settings.slackPanel.paste_slack_bot_token_xoxb')}
            isSet={data.bot_token_set}
            preview={data.bot_token_preview}
            readOnly={ro}
            value={botToken}
            onChange={setBotToken}
            cleared={botClear}
            onClearedChange={setBotClear}
            setupLink={{ href: SETUP_GUIDE, label: i18nT('pages.settings.slackPanel.where_to_find_the_bot_token') }}
          />
          <SecretField
            key={`app-${formKey}`}
            label={i18nT('pages.settings.slackPanel.slack_app_token')}
            description={i18nT('pages.settings.slackPanel.app_level_token_required_for_socket_mode_starts')}
            placeholder={i18nT('pages.settings.slackPanel.paste_slack_app_token_xapp')}
            isSet={data.app_token_set}
            preview={data.app_token_preview}
            readOnly={ro}
            value={appToken}
            onChange={setAppToken}
            cleared={appClear}
            onClearedChange={setAppClear}
            setupLink={{ href: SETUP_GUIDE, label: i18nT('pages.settings.slackPanel.where_to_find_the_app_token') }}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Identity & access ── */}
      <SettingsSection title={i18nT('pages.settings.slackPanel.identity_access')}>
        <SettingsCard index={2}>
          <SettingsInput
            label={i18nT('pages.settings.slackPanel.owner_slack_member_id')}
            description={i18nT('pages.settings.slackPanel.the_one_member_who_can_always_interact_with_the')}
            value={draft.owner_id}
            onChange={v => upd({ owner_id: v })}
            placeholder={i18nT('pages.settings.slackPanel.u0123abc456')}
            disabled={ro}
          />
          <TagListEditor
            label={i18nT('pages.settings.slackPanel.allowed_enterprise_orgs')}
            description={i18nT('pages.settings.slackPanel.enterprise_grid_org_ids_to_allow_starts_with_e_o')}
            values={draft.allowed_enterprise_ids}
            placeholder={i18nT('pages.settings.slackPanel.e0123abc456')}
            onChange={v => upd({ allowed_enterprise_ids: v })}
            validate={v => /^[ET][A-Z0-9]+$/.test(v)}
            readOnly={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Behavior ── */}
      <SettingsSection title={i18nT('pages.settings.slackPanel.behavior')}>
        <SettingsCard index={3}>
          <SettingsInput
            label={i18nT('pages.settings.slackPanel.slash_command')}
            description={i18nT('pages.settings.slackPanel.trigger_word_for_the_slack_slash_command_without')}
            value={draft.command}
            onChange={v => upd({ command: v })}
            placeholder={i18nT('pages.settings.slackPanel.kirocrew')}
            disabled={ro}
          />
          {/* The one slack.* field that cannot hot-apply: the slash command is
              registered in the Slack app manifest. The badge answers from the
              schema, so it disappears by itself if that ever changes. */}
          <div className="-mt-1 pb-1"><SchemaRestartBadge configKey="slack.command" /></div>
          <SettingsToggle
            label={i18nT('pages.settings.slackPanel.phase_reactions')}
            description={i18nT('pages.settings.slackPanel.show_phase_aware_emoji_reactions_queued_thinking')}
            checked={draft.reactions_enabled}
            onChange={v => upd({ reactions_enabled: v })}
            disabled={ro}
          />
          <SettingsToggle
            label={i18nT('pages.settings.slackPanel.show_thinking')}
            description={i18nT('pages.settings.slackPanel.post_the_model_s_reasoning_as_a_thread_reply_dis')}
            checked={draft.show_thinking}
            onChange={v => upd({ show_thinking: v })}
            disabled={ro}
          />
          {/* Optional per-channel session filing. Off by default: Slack
              conversations stay unfiled in the sidebar, as before. */}
          <div className="border-t border-border mt-4 pt-4">
            <SettingsToggle
              label={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder')}
              description={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder_desc', { channel: CHANNEL_NAME })}
              checked={draft.session_folder_on}
              onChange={v => upd({ session_folder_on: v })}
              disabled={ro}
            />
            {draft.session_folder_on && (
              <div className="mt-4">
                <SettingsInput
                  label={i18nT('pages.settings.botChannelPanel.session_folder_name')}
                  description={i18nT('pages.settings.botChannelPanel.session_folder_name_desc')}
                  value={draft.session_folder}
                  onChange={v => upd({ session_folder: v })}
                  placeholder={CHANNEL_NAME}
                  disabled={ro}
                />
              </div>
            )}
            {!!data.session_folder && (
              <ChannelFolderBackfill
                namespace="slack"
                folderName={data.session_folder}
                disabled={ro}
                testId="session-folder-backfill"
              />
            )}
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        {/* The row's buttons never wrap; the confirmation text beside them does. */}
        <Btn primary onClick={handleSave} disabled={saveMut.isPending} className="whitespace-nowrap flex-none">
          {saveMut.isPending ? i18nT('pages.settings.slackPanel.saving') : i18nT('pages.settings.slackPanel.save_slack_settings')}
        </Btn>
        {saved && (
          <span className="inline-flex items-start gap-1.5 text-[12px] text-ok min-w-0">
            <Check size={14} className="flex-none mt-px" /> <span>{savedText(tokensVerified, reconnectHint, restartHint)}</span>
          </span>
        )}
        {/* Stays after the confirmation fades, until a reconnect succeeds: the
            saved credentials are not in effect until then. */}
        {reconnectHint && reconnectButton('slack-reconnect-after-save')}
        {saved && verifyWarning && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-warn">
            <AlertTriangle size={14} /> {verifyWarning}
          </span>
        )}
        {/* No hand-off: `botToken` / `appToken` (SecretField drafts, never
            persisted — `formKey` even remounts them after a successful save) and
            the unsaved `draft` are exactly what a failed save did not store; the
            hand-off unmounts this panel and would lose them. */}
        <ErrorNotice message={error} variant="inline" />
      </div>}
    </>
  )
}
