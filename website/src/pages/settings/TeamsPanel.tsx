import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { TeamsIcon } from '../../components/TeamsIcon'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { TagListEditor } from './SlackPanel'
import { api, type TeamsConfigData, type TeamsConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
/** Brand name — do-not-translate, so it lives here rather than in the catalog. */
const CHANNEL_NAME = "Teams"
const AZURE_BOT_URL = 'https://portal.azure.com/#create/Microsoft.AzureBot'
const SETUP_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/teams-integration.md'
const WEBHOOK_PATH = '/api/messaging/teams'
/** In-chat commands the soft-threshold nudge names. Protocol tokens the channel
 *  matches by value, so they stay code constants rather than catalog values. */
const TEAMS_COMMANDS = { compact: '/compact', new: '/new' } as const
/** Percentage bounds the backend enforces on both context thresholds. */
const PCT_MIN = 1
const PCT_MAX = 100
/** Stand-in for a stored secret on the read-only path: the Teams config payload
 *  carries no server-rendered preview, and an empty box reads as "not set". */
const SECRET_MASK = '••••••'

/** Accept an allow-list entry that is an email/UPN OR an AAD object id
 *  (Teams activities carry the object id, not always the email). Shape-only,
 *  no regex: non-empty, whitespace-free, length-bounded. */
function isValidPrincipal(v: string): boolean {
  return !!v && v.length <= 254 && !/\s/.test(v)
}

type Draft = {
  enabled: boolean
  app_id: string
  tenant_id: string
  allowed_emails: string[]
  /** Soft context threshold, as typed. Held as text so a half-typed value is the
   *  user's own and not silently coerced to a number the backend would store. */
  soft_threshold_pct: string
  /** Hard context threshold, as typed. */
  hard_threshold_pct: string
  /** Whether this channel files its sessions in a folder at all (off = unfiled). */
  session_folder_on: boolean
  /** Folder name, kept while the toggle is off so turning it back on restores it. */
  session_folder: string
}

/** A server percentage as input text. `String()` and not `fmtNumber()` on
 *  purpose: this is a machine value that has to round-trip through `parseInt`,
 *  so a locale-grouped `1 000` would break the field it seeds. */
function pctText(v: number | undefined): string {
  return isPct(v) ? String(v) : ''
}

/** Whether the server actually reported a percentage for a field. */
function isPct(v: number | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** A threshold field as a whole percentage in range, or null when it is neither. */
function parsePct(text: string): number | null {
  const t = text.trim()
  if (!/^\d+$/.test(t)) return null
  const n = Number(t)
  return n >= PCT_MIN && n <= PCT_MAX ? n : null
}

/**
 * The one mapping from a threshold rule to its copy, keyed by the SAME
 * machine-readable code the backend returns on a rejected save.
 *
 * Both the client-side check and the server-rejection path read this table, so
 * the two cannot drift: a renamed catalog key is one edit, and the client
 * provably recognises exactly the codes the server can send. Two parallel
 * if-ladders is how the second one silently falls through to English prose.
 */
const THRESHOLD_MSG_KEY = {
  soft_threshold_pct_invalid:
    'pages.settings.botChannelPanel.soft_context_threshold_must_be_a_number_between',
  hard_threshold_pct_invalid: 'pages.settings.channels.hard_threshold_range_error',
  threshold_pct_inverted: 'pages.settings.channels.hard_below_soft_error',
} as const

type ThresholdCode = keyof typeof THRESHOLD_MSG_KEY

/**
 * Client-side mirror of the rule the backend enforces on the pair: whole
 * percentages in 1..100, hard at or above soft. Returns the offending CODE (not
 * prose) so the message comes from the same table the server path uses.
 * Checked as the user types so a typo is a message under the field instead of a
 * round-trip 400.
 *
 * **Blank is invalid for a field the server sent a value for.** "Leave the stored value
 * alone" is a promise a text box cannot make legibly here: the box was showing the live
 * number a moment ago, so a user who clears it to stop the nudge reads "Saved." and
 * watches the old value come back, and a placeholder reads as an example rather than as
 * the setting. Refusing blank says so at the moment they clear it, using the range
 * message the field already has. (The App ID's "blank = keep" IS legible, because the
 * server never sends a secret back for the box to contradict.)
 *
 * **Blank stays valid for a field the server sent NOTHING for** — a gateway older than
 * these fields — because then "keep what is stored" is the only honest option: we cannot
 * display a value we were never told, and sending the client's default would overwrite
 * whatever that gateway actually holds. Those saves omit the key, exactly as before.
 */
function thresholdCode(draft: Draft, stored: TeamsConfigData): ThresholdCode | null {
  const softKept = !draft.soft_threshold_pct.trim() && !isPct(stored.soft_threshold_pct)
  const hardKept = !draft.hard_threshold_pct.trim() && !isPct(stored.hard_threshold_pct)
  const soft = softKept ? null : parsePct(draft.soft_threshold_pct)
  const hard = hardKept ? null : parsePct(draft.hard_threshold_pct)
  if (!softKept && soft === null) return 'soft_threshold_pct_invalid'
  if (!hardKept && hard === null) return 'hard_threshold_pct_invalid'
  if (soft !== null && hard !== null && hard < soft) return 'threshold_pct_inverted'
  return null
}

/** The localized copy for a threshold code, or `''` for an unknown code. */
function thresholdMessage(code: string): string {
  const key = THRESHOLD_MSG_KEY[code as ThresholdCode]
  return key ? i18nT(key) : ''
}

/**
 * Client-side error text for the current draft, or `''` when it is valid.
 *
 * A rejection from the server carries both a `code` and advisory English prose;
 * the code is what makes it translatable, since rendering the prose would put
 * English inside a localized panel.
 */
function thresholdError(draft: Draft, stored: TeamsConfigData): string {
  const code = thresholdCode(draft, stored)
  return code ? thresholdMessage(code) : ''
}

function draftFrom(c: TeamsConfigData): Draft {
  return {
    enabled: c.enabled,
    app_id: '',
    tenant_id: c.tenant_id,
    allowed_emails: [...c.allowed_emails],
    soft_threshold_pct: pctText(c.soft_threshold_pct),
    hard_threshold_pct: pctText(c.hard_threshold_pct),
    // A configured name IS the on-state — the backend has one field, where ""
    // means off, so the toggle is derived rather than separately persisted.
    session_folder_on: !!c.session_folder,
    session_folder: c.session_folder ?? '',
  }
}

/** Status pill mirroring the other channel panels. */
function StatusBadge({ config }: { config: TeamsConfigData }) {
  const [dot, text, cls] = config.connected
    ? ['var(--ok)', i18nT('pages.settings.teamsPanel.active'), 'text-ok']
    : config.configured
      ? ['var(--warn)', i18nT('pages.settings.teamsPanel.not_active'), 'text-warn']
      : ['var(--muted)', i18nT('pages.settings.teamsPanel.needs_setup'), 'text-muted']
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/** One-line explanation of WHY Teams is not active, with the fix. */
function connectionHint(config: TeamsConfigData): string {
  // A missing PyJWT is the one reason the channel cannot start at all, and the
  // notice below already carries it plus the install command. Blaming a restart
  // on top of that would send the operator to fix the wrong thing.
  if (config.jwt_available === false) return ''
  if (config.connected || !config.configured) return ''
  if (config.connect_error) {
    return i18nT('pages.settings.teamsPanel.teams_credential_check_failed', { error: config.connect_error })
  }
  return i18nT('pages.settings.teamsPanel.settings_are_saved_but_the_channel_is_not_runnin')
}

/** Microsoft Teams channel-integration settings. */
export function TeamsPanel() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<TeamsConfigData>({
    queryKey: ['teams-config'],
    queryFn: api.getTeamsConfig,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [appPassword, setAppPassword] = useState('')
  const [pwClear, setPwClear] = useState(false)
  const [formKey, setFormKey] = useState(0)
  const [saved, setSaved] = useState(false)
  const [restartHint, setRestartHint] = useState(false)
  const [error, setError] = useState('')

  const syncArmed = useRef(true)
  useEffect(() => {
    if (data && syncArmed.current) {
      syncArmed.current = false
      setDraft(draftFrom(data))
      setAppPassword('')
      setPwClear(false)
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<TeamsConfigSave>) => api.saveTeamsConfig(body),
    onError: (e: unknown) => {
      // The API client throws with the raw response body. A rejected save (a
      // threshold outside 1..100, hard below soft) answers 400 with a
      // machine-readable `code`, so a known one is said in the user's language
      // and anything else degrades to the server's prose, then to the code.
      let msg = i18nT('pages.settings.teamsPanel.save_failed_is_the_gateway_running')
      if (e instanceof Error && e.message) {
        try {
          const body = JSON.parse(e.message)
          const code = typeof body.code === 'string' ? body.code : ''
          msg = thresholdMessage(code)
            || body.error
            || (code ? i18nT('pages.settings.teamsPanel.save_rejected', { code }) : e.message)
        } catch {
          msg = e.message
        }
      }
      setError(msg)
      setTimeout(() => setError(''), 8000)
    },
    onSuccess: res => {
      setSaved(true)
      setRestartHint(!!res.restart_required)
      syncArmed.current = true
      setFormKey(k => k + 1)
      setTimeout(() => setSaved(false), 6000)
      qc.invalidateQueries({ queryKey: ['teams-config'] })
    },
  })

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    const payload: Partial<TeamsConfigSave> = {
      enabled: draft.enabled,
      tenant_id: draft.tenant_id.trim(),
      allowed_emails: draft.allowed_emails,
      // Off sends "" (the field's off-state); on with a blank name falls back
      // to "Teams", which is what the toggle's description promises.
      session_folder: draft.session_folder_on ? (draft.session_folder.trim() || CHANNEL_NAME) : '',
    }
    // Only a percentage that PARSED is sent, so an out-of-range or half-typed field
    // cannot reach the wire at all. Save is disabled while one is invalid, so the only
    // blank that survives to here is the "server sent nothing" case above, where
    // omitting the key IS the correct save.
    const soft = parsePct(draft.soft_threshold_pct)
    const hard = parsePct(draft.hard_threshold_pct)
    if (soft !== null) payload.soft_threshold_pct = soft
    if (hard !== null) payload.hard_threshold_pct = hard
    // App ID is masked as "set — paste to replace" once stored, so draft.app_id
    // loads blank. Only send it when the user actually (re)entered a value —
    // otherwise a save that only edits the allow-list or toggle would overwrite
    // the stored App ID with "" and silently disable the channel at next boot.
    const appId = draft.app_id.trim()
    if (appId) payload.app_id = appId
    if (pwClear) payload.app_password_clear = true
    else if (appPassword.trim()) payload.app_password = appPassword.trim()
    saveMut.mutate(payload)
  }, [draft, appPassword, pwClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.teamsPanel.loading_teams_config')}</p>
  if (isError || !data || !draft)
    return <p className="text-[13px] text-danger p-4">{i18nT('pages.settings.teamsPanel.cannot_load_teams_config_is_the_gateway_running')}</p>

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only
  const pctErr = thresholdError(draft, data)

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none text-text">
          <TeamsIcon size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">{i18nT('pages.settings.teamsPanel.microsoft_teams')}</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            {i18nT('pages.settings.teamsPanel.talk_to_your_agents_from_a_teams_1_1_chat_self_h')}
          </p>
          {connectionHint(data) && (
            <p className="text-[12px] text-warn mt-1 flex items-center gap-1.5">
              <AlertTriangle size={12} className="flex-none" />
              {connectionHint(data)}
            </p>
          )}
        </div>
      </div>

      {/* ── Missing optional dependency ──
          The inbound webhook validates a signed Bot Framework JWT, so the
          channel refuses to start without PyJWT: enabling Teams without the
          extra installed does nothing and reports nothing. Compared against
          `false` rather than falsy — a gateway that predates the field sends
          none, and absence must not be read as a missing dependency. */}
      {data.jwt_available === false && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 mb-3 mt-2">
          <AlertTriangle className="lucide-inline text-warn flex-none mt-0.5" aria-hidden />
          <span className="text-[12px] text-text">
            {/* One key for the whole notice: the install command has to stay
                verbatim, and splitting the sentence around it would pin every
                locale to English word order. */}
            <Trans
              i18nKey="pages.settings.teamsPanel.jwt_required"
              components={{ mono: <code className="font-mono" /> }}
            />
          </span>
        </div>
      )}

      {/* ── Read-only notice (remote session) ── */}
      {ro && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mb-3">
          <Lock size={13} className="text-muted flex-none" />
          <span className="text-[12px] text-muted">
            {i18nT('pages.settings.teamsPanel.teams_settings_are_managed_on_the_machine_runnin')}
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title={i18nT('pages.settings.teamsPanel.get_your_credentials')}>
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            {i18nT('pages.settings.teamsPanel.register_an_azure_bot_add_the')} <strong>{i18nT('pages.settings.teamsPanel.microsoft_teams')}</strong> {i18nT('pages.settings.teamsPanel.channel_and_set_its_messaging_endpoint_to_your_p')} <code>{WEBHOOK_PATH}</code>{i18nT('pages.settings.teamsPanel.copy_the_app_client_id_create_a_client_secret_an')}
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href={AZURE_BOT_URL}
              target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all bg-accent text-accent-fg border-accent hover:bg-accent-hover"
            >
              {i18nT('pages.settings.teamsPanel.create_azure_bot')} <ExternalLink size={13} />
            </a>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.teamsPanel.setup_guide')} <ExternalLink size={13} />
            </a>
          </div>
          <p className="text-[12px] text-muted mt-2 mb-0">
            {i18nT('pages.settings.teamsPanel.messaging_endpoint')} <code>{i18nT('pages.settings.teamsPanel.https_your_host')}{WEBHOOK_PATH}</code>
          </p>
          <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 mt-3">
            <AlertTriangle size={13} className="lucide-inline text-warn flex-none mt-0.5" />
            <span className="text-[12px] text-text">
              <strong>{i18nT('pages.settings.teamsPanel.requires_a_public_https_url')}</strong> {i18nT('pages.settings.teamsPanel.teams_delivers_messages_by_calling_this_endpoint')} <code>{i18nT('pages.settings.teamsPanel.localhost')}</code> {i18nT('pages.settings.teamsPanel.or_ssh_tunneled_address_won_t_work_expose_the_ga')}
              <code>{i18nT('pages.settings.teamsPanel.your_host')}</code> {i18nT('pages.settings.teamsPanel.above_unlike_slack_the_bot_framework_has_no_outb')}
            </span>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Bot setup steps ── */}
      <SettingsSection title={i18nT('pages.settings.teamsPanel.connect_the_azure_bot')}>
        <SettingsCard index={1}>
          <ol className="text-[13px] text-text m-0 pl-5 space-y-1.5 list-decimal">
            <li>
              {i18nT('pages.settings.teamsPanel.expose_this_gateway_over')} <strong>{i18nT('pages.settings.teamsPanel.public_https')}</strong> {i18nT('pages.settings.teamsPanel.tunnel_or_reverse_proxy_see_the_note_above_note')}
            </li>
            <li>
              <strong>{i18nT('pages.settings.teamsPanel.create_an_azure_bot')}</strong> {i18nT('pages.settings.teamsPanel.button_above_as')}{' '}
              <strong>{i18nT('pages.settings.teamsPanel.multi_tenant')}</strong>{i18nT('pages.settings.teamsPanel.or_single_tenant_if_you_ll_pin_a_tenant_id')}
            </li>
            <li>
              {i18nT('pages.settings.teamsPanel.in_the_bot_s')} <strong>{i18nT('pages.settings.teamsPanel.configuration')}</strong>{i18nT('pages.settings.teamsPanel.set_the')}{' '}
              <strong>{i18nT('pages.settings.teamsPanel.messaging_endpoint_2')}</strong> {i18nT('pages.settings.teamsPanel.to')}{' '}
              <code>{i18nT('pages.settings.teamsPanel.https_your_host')}{WEBHOOK_PATH}</code>.
            </li>
            <li>
              {i18nT('pages.settings.teamsPanel.under')} <strong>{i18nT('pages.settings.teamsPanel.certificates_secrets')}</strong>{i18nT('pages.settings.teamsPanel.create_a_client_secret_and_paste_it_as_the')} <strong>{i18nT('pages.settings.teamsPanel.app_password')}</strong> {i18nT('pages.settings.teamsPanel.below_copy_the')}{' '}
              <strong>{i18nT('pages.settings.teamsPanel.app_client_id')}</strong> {i18nT('pages.settings.teamsPanel.from_the_bot_s_overview_and_the')}{' '}
              <strong>{i18nT('pages.settings.teamsPanel.tenant_id')}</strong> {i18nT('pages.settings.teamsPanel.if_single_tenant')}
            </li>
            <li>
              {i18nT('pages.settings.teamsPanel.under')} <strong>{i18nT('pages.settings.teamsPanel.channels')}</strong>{i18nT('pages.settings.teamsPanel.add_the')} <strong>{i18nT('pages.settings.teamsPanel.microsoft_teams')}</strong>{' '}
              {i18nT('pages.settings.teamsPanel.channel')}
            </li>
            <li>
              {i18nT('pages.settings.teamsPanel.fill_in_the_credentials_below_add_yourself_to_th')} <strong>{i18nT('pages.settings.teamsPanel.enable')}</strong>{i18nT('pages.settings.teamsPanel.and_save')}
            </li>
            <li>
              {i18nT('pages.settings.teamsPanel.side_load_a_teams_app_whose')} <code>{i18nT('pages.settings.teamsPanel.botid')}</code> {i18nT('pages.settings.teamsPanel.is_your_app_id_then_dm_the_bot_full_manifest_ste')}
            </li>
          </ol>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required credentials ── */}
      <SettingsSection title={i18nT('pages.settings.teamsPanel.required')}>
        <SettingsCard index={2}>
          <SettingsInput
            label={i18nT('pages.settings.teamsPanel.app_client_id')}
            placeholder={data.app_id_set ? i18nT('pages.settings.teamsPanel.set_paste_to_replace') : i18nT('pages.settings.teamsPanel.microsoft_app_id')}
            value={draft.app_id}
            onChange={v => upd({ app_id: v })}
            disabled={ro}
            configKey="teams.app_id"
          />
          <SecretField
            key={`pw-${formKey}`}
            label={i18nT('pages.settings.teamsPanel.app_password_client_secret')}
            description={i18nT('pages.settings.teamsPanel.azure_bot_client_secret_stored_only_in_env_never')}
            placeholder={i18nT('pages.settings.teamsPanel.paste_azure_bot_client_secret')}
            isSet={data.app_password_set}
            preview={SECRET_MASK}
            readOnly={ro}
            value={appPassword}
            onChange={setAppPassword}
            cleared={pwClear}
            onClearedChange={setPwClear}
            setupLink={{ href: SETUP_GUIDE, label: i18nT('pages.settings.teamsPanel.where_to_find_the_client_secret') }}
          />
          <SettingsInput
            label={i18nT('pages.settings.teamsPanel.tenant_id')}
            description={i18nT('pages.settings.teamsPanel.optional_only_for_single_tenant_bots')}
            placeholder={i18nT('pages.settings.teamsPanel.leave_empty_for_a_multi_tenant_bot')}
            value={draft.tenant_id}
            onChange={v => upd({ tenant_id: v })}
            disabled={ro}
            configKey="teams.tenant_id"
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Access ── */}
      <SettingsSection title={i18nT('pages.settings.teamsPanel.access')}>
        <SettingsCard index={3}>
          <SettingsToggle
            label={i18nT('pages.settings.teamsPanel.enable_teams_channel')}
            description={i18nT('pages.settings.teamsPanel.start_the_channel_at_gateway_boot_when_the_app_i')}
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <TagListEditor
            label={i18nT('pages.settings.teamsPanel.allowed_users_email_or_aad_object_id')}
            description={i18nT('pages.settings.teamsPanel.azure_ad_upns_emails_or_object_ids_permitted_to')}
            values={draft.allowed_emails}
            placeholder={i18nT('pages.settings.teamsPanel.you_example_com_or_00000000_0000_0000_0000_00000')}
            onChange={v => upd({ allowed_emails: v })}
            validate={isValidPrincipal}
            readOnly={ro}
          />
          {/* Optional per-channel session filing. Off by default: Teams
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
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Behavior ── */}
      <SettingsSection title={i18nT('pages.settings.botChannelPanel.behavior')}>
        <SettingsCard index={4}>
          <SettingsInput
            label={i18nT('pages.settings.botChannelPanel.soft_context_threshold')}
            description={i18nT('pages.settings.channels.threshold_description', {
              compact: TEAMS_COMMANDS.compact,
              new: TEAMS_COMMANDS.new,
            })}
            type="number"
            min={PCT_MIN}
            max={PCT_MAX}
            value={draft.soft_threshold_pct}
            onChange={v => upd({ soft_threshold_pct: v })}
            // The stored value, so an emptied box shows what it is refusing to lose
            // alongside the error that says the field cannot be blank.
            placeholder={pctText(data.soft_threshold_pct)}
            disabled={ro}
            configKey="teams.soft_threshold_pct"
          />
          <SettingsInput
            label={i18nT('pages.settings.channels.hard_threshold_label')}
            description={i18nT('pages.settings.channels.hard_threshold_description')}
            type="number"
            min={PCT_MIN}
            max={PCT_MAX}
            value={draft.hard_threshold_pct}
            onChange={v => upd({ hard_threshold_pct: v })}
            placeholder={pctText(data.hard_threshold_pct)}
            disabled={ro}
            configKey="teams.hard_threshold_pct"
          />
          {/* Client-side only, and it names the field rather than the endpoint:
              there is nothing for the agent to diagnose, so no hand-off. */}
          <ErrorNotice message={pctErr} variant="inline" />
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending || !!pctErr}>
          {saveMut.isPending ? i18nT('pages.settings.teamsPanel.saving') : i18nT('pages.settings.teamsPanel.save_teams_settings')}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {restartHint ? i18nT('pages.settings.teamsPanel.saved_restart_the_gateway_to_apply') : i18nT('pages.settings.teamsPanel.saved')}
          </span>
        )}
        {error && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-danger">
            <AlertTriangle size={14} /> {error}
          </span>
        )}
      </div>}
    </>
  )
}
