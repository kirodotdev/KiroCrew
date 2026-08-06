import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { TeamsIcon } from '../../components/TeamsIcon'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'
import { api, type TeamsConfigData, type TeamsConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
const AZURE_BOT_URL = 'https://portal.azure.com/#create/Microsoft.AzureBot'
const SETUP_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/teams-integration.md'
const WEBHOOK_PATH = '/api/messaging/teams'

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
}

function draftFrom(c: TeamsConfigData): Draft {
  return {
    enabled: c.enabled,
    app_id: '',
    tenant_id: c.tenant_id,
    allowed_emails: [...c.allowed_emails],
  }
}

/** Status pill mirroring the other channel panels. */
function StatusBadge({ config }: { config: TeamsConfigData }) {
  const [dot, text, cls] = config.connected
    ? ['var(--ok)', 'Active', 'text-ok']
    : config.configured
      ? ['var(--warn)', 'Not active', 'text-warn']
      : ['var(--muted)', 'Needs setup', 'text-muted']
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/** One-line explanation of WHY Teams is not active, with the fix. */
function connectionHint(config: TeamsConfigData): string {
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
      let msg = i18nT('pages.settings.teamsPanel.save_failed_is_the_gateway_running')
      if (e instanceof Error && e.message) {
        try {
          msg = JSON.parse(e.message).error ?? e.message
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
    }
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
  // Matches the shared <Input>/<SecretField> look so all fields render consistently.
  const inputCls =
    'w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body font-normal outline-none transition-colors focus-ring disabled:opacity-60'

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
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 mt-3">
            <AlertTriangle size={13} className="lucide-inline text-warning flex-none mt-0.5" />
            <span className="text-[12px] text-text">
              <strong>{i18nT('pages.settings.teamsPanel.requires_a_public_https_url')}</strong> {i18nT('pages.settings.teamsPanel.teams_delivers_messages_by_calling_this_endpoint')} <code>{i18nT('pages.settings.teamsPanel.localhost')}</code> {i18nT('pages.settings.teamsPanel.or_ssh_tunneled_address_won_t_work_expose_the_ga')}
              <code>{i18nT('pages.settings.teamsPanel.your_host')}</code> {i18nT('pages.settings.teamsPanel.above_unlike_slack_the_bot_framework_has_no_outb')}
            </span>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Bot setup steps ── */}
      <SettingsSection title={i18nT('pages.settings.teamsPanel.connect_the_azure_bot')}>
        <SettingsCard>
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
        <SettingsCard>
          <label htmlFor="teams-app-id" className="flex flex-col gap-1.5 py-1.5 text-[13px] font-semibold text-text">
            {i18nT('pages.settings.teamsPanel.app_client_id')}
            <input
              id="teams-app-id"
              aria-label={i18nT('pages.settings.teamsPanel.app_client_id')}
              className={inputCls}
              type="text"
              placeholder={data.app_id_set ? i18nT('pages.settings.teamsPanel.set_paste_to_replace') : i18nT('pages.settings.teamsPanel.microsoft_app_id')}
              value={draft.app_id}
              disabled={ro}
              onChange={e => upd({ app_id: e.target.value })}
            />
          </label>
          <SecretField
            key={`pw-${formKey}`}
            label={i18nT('pages.settings.teamsPanel.app_password_client_secret')}
            description={i18nT('pages.settings.teamsPanel.azure_bot_client_secret_stored_only_in_env_never')}
            placeholder={i18nT('pages.settings.teamsPanel.paste_azure_bot_client_secret')}
            isSet={data.app_password_set}
            preview=""
            readOnly={ro}
            value={appPassword}
            onChange={setAppPassword}
            cleared={pwClear}
            onClearedChange={setPwClear}
            setupLink={{ href: SETUP_GUIDE, label: i18nT('pages.settings.teamsPanel.where_to_find_the_client_secret') }}
          />
          <label htmlFor="teams-tenant-id" className="flex flex-col gap-1.5 py-1.5 text-[13px] font-semibold text-text">
            {i18nT('pages.settings.teamsPanel.tenant_id')}
            <span className="text-[12px] font-normal text-muted -mt-0.5">{i18nT('pages.settings.teamsPanel.optional_only_for_single_tenant_bots')}</span>
            <input
              id="teams-tenant-id"
              aria-label={i18nT('pages.settings.teamsPanel.tenant_id')}
              className={inputCls}
              type="text"
              placeholder={i18nT('pages.settings.teamsPanel.leave_empty_for_a_multi_tenant_bot')}
              value={draft.tenant_id}
              disabled={ro}
              onChange={e => upd({ tenant_id: e.target.value })}
            />
          </label>
        </SettingsCard>
      </SettingsSection>

      {/* ── Access ── */}
      <SettingsSection title={i18nT('pages.settings.teamsPanel.access')}>
        <SettingsCard>
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
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
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
