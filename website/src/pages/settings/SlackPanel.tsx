import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Plus, X, Lock } from 'lucide-react'
import { SlackIcon } from '../../components/SlackIcon'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Input, Btn } from '../../components/ui'
import { api, type SlackConfigData, type SlackConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/slack-integration.md'

type Draft = {
  owner_id: string
  command: string
  allowed_enterprise_ids: string[]
  reactions_enabled: boolean
  show_thinking: boolean
}

function draftFrom(c: SlackConfigData): Draft {
  return {
    owner_id: c.owner_id,
    command: c.command,
    allowed_enterprise_ids: [...c.allowed_enterprise_ids],
    reactions_enabled: c.reactions_enabled,
    show_thinking: c.show_thinking,
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

/** One-line explanation of WHY Slack is not connected, with the fix. */
function connectionHint(config: SlackConfigData): string {
  if (config.connected || !config.configured) return ''
  if (config.connect_error === 'invalid_auth') {
    return i18nT('pages.settings.slackPanel.slack_rejected_the_stored_tokens_invalid_auth_re')
  }
  if (config.connect_error) {
    return i18nT('pages.settings.slackPanel.slack_connection_failed_at_startup', { error: config.connect_error })
  }
  return i18nT('pages.settings.slackPanel.tokens_are_saved_but_not_yet_active_restart_the')
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
    <div className="flex flex-col gap-1.5 py-1.5">
      <span className="text-[13px] font-semibold text-text">{label}</span>
      {description && <div className="text-[12px] text-muted">{description}</div>}
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {values.map(v => (
            <span key={v} className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[12px] font-mono text-text">
              {v}
              {!readOnly && (
                <button type="button" onClick={() => onChange(values.filter(x => x !== v))}
                  className="text-muted hover:text-danger transition-colors" aria-label={i18nT('pages.settings.slackPanel.remove', { name: v })}>
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
          <Input value={draft} placeholder={placeholder} className="flex-none font-mono"
            onChange={e => { setDraft(e.target.value); setErr('') }}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add() } }} />
          <Btn onClick={add} disabled={!draft.trim()}><Plus size={13} /> {i18nT('pages.settings.slackPanel.add')}</Btn>
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
  const [verifyWarning, setVerifyWarning] = useState('')
  const [tokensVerified, setTokensVerified] = useState(false)
  const [manifestCopied, setManifestCopied] = useState(false)

  // Public manifest template + one-click Slack create URL (no secrets).
  const manifestQ = useQuery({
    queryKey: ['slack-manifest'],
    queryFn: api.getSlackManifest,
    staleTime: Infinity,
    retry: false,
  })

  const copyManifest = useCallback(() => {
    if (!manifestQ.data) return
    navigator.clipboard.writeText(manifestQ.data.manifest).then(() => {
      setManifestCopied(true)
      setTimeout(() => setManifestCopied(false), 1500)
    }).catch(() => {})
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
      setError(msg)
      setTimeout(() => setError(''), 8000)
    },
    onSuccess: (res, vars) => {
      setSaved(true)
      setRestartHint(!!res.restart_required)
      setVerifyWarning(res.verify_warning || '')
      setTokensVerified(!!(vars.bot_token || vars.app_token) && !res.verify_warning)
      syncArmed.current = true
      setFormKey(k => k + 1)
      setTimeout(() => setSaved(false), 6000)
      qc.invalidateQueries({ queryKey: ['slack-config'] })
    },
  })

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    const payload: Partial<SlackConfigSave> = {
      owner_id: draft.owner_id.trim(),
      command: draft.command.trim(),
      allowed_enterprise_ids: draft.allowed_enterprise_ids,
      reactions_enabled: draft.reactions_enabled,
      show_thinking: draft.show_thinking,
    }
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    if (appClear) payload.app_token_clear = true
    else if (appToken.trim()) payload.app_token = appToken.trim()
    saveMut.mutate(payload)
  }, [draft, botToken, appToken, botClear, appClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.slackPanel.loading_slack_config')}</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">{i18nT('pages.settings.slackPanel.cannot_load_slack_config_is_the_gateway_running')}</p>

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only

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
          </div>
          <p className="text-[12px] text-muted mt-1">
            {i18nT('pages.settings.slackPanel.talk_to_your_agents_from_slack_over_socket_mode')}
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
            <Btn onClick={copyManifest} disabled={!manifestQ.data}>
              {manifestCopied ? <><Check size={13} /> {i18nT('pages.settings.slackPanel.copied')}</> : i18nT('pages.settings.slackPanel.copy_manifest_yaml')}
            </Btn>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.slackPanel.setup_guide')} <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required tokens ── */}
      <SettingsSection title={i18nT('pages.settings.slackPanel.required')}>
        <SettingsCard>
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
        <SettingsCard>
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
        <SettingsCard>
          <SettingsInput
            label={i18nT('pages.settings.slackPanel.slash_command')}
            description={i18nT('pages.settings.slackPanel.trigger_word_for_the_slack_slash_command_without')}
            value={draft.command}
            onChange={v => upd({ command: v })}
            placeholder={i18nT('pages.settings.slackPanel.kirocrew')}
            disabled={ro}
          />
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
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
          {saveMut.isPending ? i18nT('pages.settings.slackPanel.saving') : i18nT('pages.settings.slackPanel.save_slack_settings')}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {tokensVerified ? i18nT('pages.settings.slackPanel.verified_with_slack_and_saved_restart_the_gatewa') : restartHint ? i18nT('pages.settings.slackPanel.saved_restart_the_gateway_to_apply') : i18nT('pages.settings.slackPanel.saved')}
          </span>
        )}
        {saved && verifyWarning && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-warn">
            <AlertTriangle size={14} /> {verifyWarning}
          </span>
        )}
        {/*
          Deliberately NOT opted in, same as `BrowserPanel`: `botToken`/`appToken`
          live in local state (never persisted — `formKey` even remounts them after
          a successful save), and both come from the Slack app admin.
        */}
        <ErrorNotice message={error} variant="inline" />
      </div>}
    </>
  )
}
