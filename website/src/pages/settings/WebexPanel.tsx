import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { WebexIcon } from '../../components/WebexIcon'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'
import { api, type WebexConfigData, type WebexConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
const CREATE_BOT_URL = 'https://developer.webex.com/my-apps/new/bot'
const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/webex-integration.md'

/** Loose email shape check via linear string ops (mirrors the backend —
 *  avoids the polynomially-backtracking regex CodeQL flags). */
function isValidEmail(v: string): boolean {
  if (!v || v.length > 254 || /\s/.test(v)) return false
  const at = v.indexOf('@')
  if (at <= 0 || v.indexOf('@', at + 1) !== -1) return false
  const domain = v.slice(at + 1)
  return domain.slice(1, -1).includes('.')
}

type Draft = {
  enabled: boolean
  allowed_emails: string[]
}

function draftFrom(c: WebexConfigData): Draft {
  return { enabled: c.enabled, allowed_emails: [...c.allowed_emails] }
}

/** Status pill mirroring the Slack panel's connection states. */
function StatusBadge({ config }: { config: WebexConfigData }) {
  const [dot, text, cls] = config.connected
    ? ['var(--ok)', i18nT('pages.settings.webexPanel.active'), 'text-ok']
    : config.configured
      ? ['var(--warn)', i18nT('pages.settings.webexPanel.not_active'), 'text-warn']
      : ['var(--muted)', i18nT('pages.settings.webexPanel.needs_setup'), 'text-muted']
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/** One-line explanation of WHY Webex is not active, with the fix. */
function connectionHint(config: WebexConfigData): string {
  if (config.connected || !config.configured) return ''
  if (config.connect_error) {
    return i18nT('pages.settings.webexPanel.connection_failed', { error: config.connect_error })
  }
  return i18nT('pages.settings.webexPanel.settings_are_saved_but_the_channel_is_not_runnin')
}

/** Webex channel-integration settings. */
export function WebexPanel() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<WebexConfigData>({
    queryKey: ['webex-config'],
    queryFn: api.getWebexConfig,
    retry: false,
    // An ambient focus refetch mid-edit would hand back a fresh `data`
    // object and clobber unsaved edits via the sync effect below.
    refetchOnWindowFocus: false,
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [botToken, setBotToken] = useState('')
  const [botClear, setBotClear] = useState(false)
  const [formKey, setFormKey] = useState(0) // bump to remount the secret field after save
  const [saved, setSaved] = useState(false)
  const [restartHint, setRestartHint] = useState(false)
  const [verifyWarning, setVerifyWarning] = useState('')
  const [tokenVerified, setTokenVerified] = useState(false)
  const [error, setError] = useState('')

  // Sync the local draft when server config arrives. Guarded so only the
  // initial load and post-save invalidation reseed it — a background refetch
  // must not discard in-progress edits (including a just-pasted token).
  const syncArmed = useRef(true)
  useEffect(() => {
    if (data && syncArmed.current) {
      syncArmed.current = false
      setDraft(draftFrom(data))
      setBotToken(''); setBotClear(false)
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<WebexConfigSave>) => api.saveWebexConfig(body),
    onError: (e: unknown) => {
      let msg = i18nT('pages.settings.webexPanel.save_failed_is_the_gateway_running')
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
    onSuccess: (res, vars) => {
      setSaved(true)
      setRestartHint(!!res.restart_required)
      setVerifyWarning(res.verify_warning || '')
      setTokenVerified(!!vars.bot_token && !res.verify_warning)
      syncArmed.current = true
      setFormKey(k => k + 1)
      setTimeout(() => setSaved(false), 6000)
      qc.invalidateQueries({ queryKey: ['webex-config'] })
    },
  })

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    const payload: Partial<WebexConfigSave> = {
      enabled: draft.enabled,
      allowed_emails: draft.allowed_emails,
    }
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    saveMut.mutate(payload)
  }, [draft, botToken, botClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.webexPanel.loading_webex_config')}</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">{i18nT('pages.settings.webexPanel.cannot_load_webex_config_is_the_gateway_running')}</p>

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none text-text">
          <WebexIcon size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">{i18nT('pages.settings.webexPanel.webex')}</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            {i18nT('pages.settings.webexPanel.talk_to_your_agents_from_cisco_webex_no_public_u')}
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
            {i18nT('pages.settings.webexPanel.webex_settings_are_managed_on_the_machine_runnin')}
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title={i18nT('pages.settings.webexPanel.get_your_credentials')}>
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            {i18nT('pages.settings.webexPanel.create_a_bot_on_the_webex_developer_portal_name')}
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href={CREATE_BOT_URL}
              target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all bg-accent text-accent-fg border-accent hover:bg-accent-hover"
            >
              {i18nT('pages.settings.webexPanel.create_webex_bot')} <ExternalLink size={13} />
            </a>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.webexPanel.setup_guide')} <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required token ── */}
      <SettingsSection title={i18nT('pages.settings.webexPanel.required')}>
        <SettingsCard>
          <SecretField
            key={`bot-${formKey}`}
            label={i18nT('pages.settings.webexPanel.webex_bot_token')}
            description={i18nT('pages.settings.webexPanel.bot_access_token_from_developer_webex_com_my_web')}
            placeholder={i18nT('pages.settings.webexPanel.paste_webex_bot_access_token')}
            isSet={data.bot_token_set}
            preview={data.bot_token_preview}
            readOnly={ro}
            value={botToken}
            onChange={setBotToken}
            cleared={botClear}
            onClearedChange={setBotClear}
            setupLink={{ href: SETUP_GUIDE, label: i18nT('pages.settings.webexPanel.where_to_find_the_bot_token') }}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Access ── */}
      <SettingsSection title={i18nT('pages.settings.webexPanel.access')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.webexPanel.enable_webex_channel')}
            description={i18nT('pages.settings.webexPanel.start_the_channel_at_gateway_boot_when_a_token_i')}
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <TagListEditor
            label={i18nT('pages.settings.webexPanel.allowed_emails')}
            description={i18nT('pages.settings.webexPanel.webex_account_emails_permitted_to_dm_the_bot_emp')}
            values={draft.allowed_emails}
            placeholder={i18nT('pages.settings.webexPanel.you_example_com')}
            onChange={v => upd({ allowed_emails: v })}
            validate={isValidEmail}
            readOnly={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
          {saveMut.isPending ? i18nT('pages.settings.webexPanel.saving') : i18nT('pages.settings.webexPanel.save_webex_settings')}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {tokenVerified ? i18nT('pages.settings.webexPanel.verified_with_webex_and_saved_restart_the_gatewa') : restartHint ? i18nT('pages.settings.webexPanel.saved_restart_the_gateway_to_apply') : i18nT('pages.settings.webexPanel.saved')}
          </span>
        )}
        {saved && verifyWarning && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-warn">
            <AlertTriangle size={14} /> {verifyWarning}
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
