import { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'

import { i18nT } from '../../i18n/t'
/** Config shape shared by every bot-token channel (Discord, Telegram, …). */
export interface BotChannelConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  bot_token_set: boolean
  bot_token_preview: string
  /** Second credential slot (optional; only WeCom sends these). */
  bot_id_set?: boolean
  bot_id_preview?: string
  enabled: boolean
  allowed_user_ids: string[]
  allowed_thread_ids?: string[]
  /** Explicit allow-everyone opt-in (optional; only WeCom sends this). */
  allow_all_users?: boolean
  soft_threshold_pct: number
  /** Telegram forum per-topic config (optional; only Telegram sends these). */
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
}

/** Writable fields shared by every bot-token channel save endpoint. */
export interface BotChannelConfigSave {
  bot_token: string
  bot_token_clear: boolean
  /** Second credential slot (optional; only WeCom sends these). */
  bot_id?: string
  bot_id_clear?: boolean
  enabled: boolean
  allowed_user_ids: string[]
  allowed_thread_ids?: string[]
  /** Explicit allow-everyone opt-in (optional; only WeCom sends this). */
  allow_all_users?: boolean
  soft_threshold_pct: number
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
}

/** Everything channel-specific: names, copy, endpoints, and guide content. */
export interface BotChannelSpec {
  /** Display name, e.g. "Discord". */
  name: string
  /** react-query cache key, e.g. "discord-config". */
  queryKey: string
  /** Brand logo element for the header (20px) — a *Logo.tsx component. */
  logo: ReactNode
  /** One-line panel description under the title. */
  description: string
  /** Host to check network access to in the failed-to-start hint. */
  host: string
  /** Setup guide URL (docs page). */
  setupGuide: string
  /** Guide-card body content (how to create the bot / find your ID). */
  guideBody: ReactNode
  /** Optional guide section title (default "Get your bot token"). */
  guideTitle?: string
  /** Primary guide-card button: label + href. */
  guideLink: { label: string; href: string }
  /** Secret field labels. */
  tokenDescription: string
  tokenPlaceholder: string
  /** Optional label override for the primary secret (default "<name> bot token"). */
  tokenLabel?: string
  /**
   * Optional second credential rendered above the primary secret (WeCom's
   * bot ID + secret pair). Channels that omit it are unaffected and never
   * send ``bot_id`` fields.
   */
  secondCredential?: {
    label: string
    description: string
    placeholder: string
  }
  /**
   * Optional allow-everyone toggle rendered above the user allow-list
   * (WeCom: every org member may DM the bot). Channels that omit it never
   * send ``allow_all_users``.
   */
  allowAll?: {
    label: string
    description: ReactNode
    /** Note shown under the allow-list while the toggle is on. */
    bypassNote: string
  }
  /** Allowlist copy. */
  allowlistDescription: string
  allowlistPlaceholder: string
  /**
   * Optional allow-list entry validator (default: digits only). WeCom userids
   * are alphanumeric with ``.-_@``, so the numeric default would reject them.
   */
  allowlistValidate?: (v: string) => boolean
  /** Soft-threshold copy (command prefixes differ per channel). */
  thresholdDescription: string
  /** Fail-closed hint shown when enabled + token set but allowlist empty. */
  emptyAllowlistHint: string
  /** Optional shared-thread allow-list rendered below user access controls. */
  threadAllowlist?: {
    label: string
    description: string
    placeholder: string
    help: ReactNode
    warning: ReactNode
  }
  /**
   * Optional forum/per-topic config (Telegram supergroups). When present, the
   * panel renders an allow_forum toggle plus a chat-id tag editor; channels
   * that omit it (Discord, Webex) are unaffected and never send forum fields.
   */
  forum?: {
    toggleLabel: string
    toggleDescription: ReactNode
    allowlistLabel: string
    allowlistDescription: string
    allowlistPlaceholder: string
    /** Fail-closed hint shown when the toggle is on but the list is empty. */
    emptyHint: string
  }
  /** API calls. */
  getConfig: () => Promise<BotChannelConfigData>
  saveConfig: (body: Partial<BotChannelConfigSave>) => Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>
  /** Refresh cadence for the live status badge (ms); omit to disable. */
  refetchInterval?: number
}

type Draft = {
  enabled: boolean
  allowed_user_ids: string[]
  allowed_thread_ids: string[]
  allow_all_users: boolean
  soft_threshold_pct: string
  allow_forum: boolean
  allowed_forum_chat_ids: string[]
}

function draftFrom(c: BotChannelConfigData): Draft {
  return {
    enabled: c.enabled,
    allowed_user_ids: [...c.allowed_user_ids],
    allowed_thread_ids: [...(c.allowed_thread_ids ?? [])],
    allow_all_users: !!c.allow_all_users,
    soft_threshold_pct: String(c.soft_threshold_pct),
    allow_forum: !!c.allow_forum,
    allowed_forum_chat_ids: [...(c.allowed_forum_chat_ids ?? [])],
  }
}

/** Status pill mirroring the run state of the channel. */
function StatusBadge({ config }: { config: BotChannelConfigData }) {
  const [dot, text, cls] = config.connected
    ? ['var(--ok)', 'Connected', 'text-ok']
    : config.configured
      ? ['var(--warn)', 'Not connected', 'text-warn']
      : ['var(--muted)', 'Needs setup', 'text-muted']
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/** One-line explanation of WHY the channel is not running, with the fix. */
function connectionHint(spec: BotChannelSpec, config: BotChannelConfigData): string {
  if (config.connected) return ''
  if (config.connect_error) {
    return i18nT('pages.settings.botChannelPanel.channel_failed_to_start', { channel: spec.name, error: config.connect_error, host: spec.host })
  }
  if (config.configured) {
    return i18nT('pages.settings.botChannelPanel.configuration_is_saved_but_the_channel_is_not_ru')
  }
  if (config.bot_token_set && (config.bot_id_set ?? true) && config.enabled && config.allowed_user_ids.length === 0 && !config.allow_all_users) {
    return spec.emptyAllowlistHint
  }
  return ''
}

/**
 * Shared settings panel for bot-token messaging channels (Discord, Telegram).
 * Each channel supplies a {@link BotChannelSpec} with its copy and endpoints;
 * the draft/save/status plumbing lives here exactly once.
 */
export function BotChannelPanel({ spec }: { spec: BotChannelSpec }) {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<BotChannelConfigData>({
    queryKey: [spec.queryKey],
    queryFn: spec.getConfig,
    retry: false,
    // Keeps the status badge tracking live backend state (polling health).
    // Draft edits are safe: the sync effect reseeds only when re-armed.
    refetchInterval: spec.refetchInterval,
    // An ambient focus refetch mid-edit would hand back a fresh `data`
    // object and clobber unsaved edits via the sync effect below.
    refetchOnWindowFocus: false,
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [botToken, setBotToken] = useState('')
  const [botClear, setBotClear] = useState(false)
  const [botId, setBotId] = useState('')
  const [botIdClear, setBotIdClear] = useState(false)
  const [formKey, setFormKey] = useState(0)  // bump to remount secret field after save
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
      setBotId(''); setBotIdClear(false)
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<BotChannelConfigSave>) => spec.saveConfig(body),
    onError: (e: unknown) => {
      // The API client throws with the raw response body; extract the
      // server's error field for clean display.
      let msg = i18nT('pages.settings.botChannelPanel.save_failed_is_the_gateway_running')
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
      qc.invalidateQueries({ queryKey: [spec.queryKey] })
    },
  })

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    const pct = parseInt(draft.soft_threshold_pct, 10)
    if (!Number.isInteger(pct) || pct < 1 || pct > 100) {
      setError(i18nT('pages.settings.botChannelPanel.soft_context_threshold_must_be_a_number_between'))
      setTimeout(() => setError(''), 8000)
      return
    }
    const payload: Partial<BotChannelConfigSave> = {
      enabled: draft.enabled,
      allowed_user_ids: draft.allowed_user_ids,
      soft_threshold_pct: pct,
    }
    if (spec.threadAllowlist) payload.allowed_thread_ids = draft.allowed_thread_ids
    if (spec.allowAll) payload.allow_all_users = draft.allow_all_users
    if (spec.forum) {
      payload.allow_forum = draft.allow_forum
      payload.allowed_forum_chat_ids = draft.allowed_forum_chat_ids
    }
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    if (spec.secondCredential) {
      if (botIdClear) payload.bot_id_clear = true
      else if (botId.trim()) payload.bot_id = botId.trim()
    }
    saveMut.mutate(payload)
  }, [draft, botToken, botClear, botId, botIdClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.botChannelPanel.loading')} {spec.name} {i18nT('pages.settings.botChannelPanel.config')}</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">{i18nT('pages.settings.botChannelPanel.cannot_load')} {spec.name} {i18nT('pages.settings.botChannelPanel.config_is_the_gateway_running')}</p>

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only
  const hint = connectionHint(spec, data)

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none">
          {spec.logo}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">{spec.name}</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">{spec.description}</p>
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
            {spec.name} {i18nT('pages.settings.botChannelPanel.settings_are_managed_on_the_machine_running_kiro')}
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title={spec.guideTitle ?? i18nT('pages.settings.botChannelPanel.get_your_bot_token')}>
        <SettingsCard>
          <p className="text-[13px] text-text m-0">{spec.guideBody}</p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href={spec.guideLink.href}
              target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border bg-accent text-accent-fg border-accent hover:bg-accent-hover transition-all"
            >
              {spec.guideLink.label} <ExternalLink size={13} />
            </a>
            <a href={spec.setupGuide} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.botChannelPanel.setup_guide')} <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required ── */}
      <SettingsSection title={i18nT('pages.settings.botChannelPanel.required')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.botChannelPanel.enable', { channel: spec.name })}
            description={i18nT('pages.settings.botChannelPanel.start_the_channel_at_gateway_startup', { channel: spec.name })}
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          {spec.secondCredential && (
            <SecretField
              key={`botid-${formKey}`}
              label={spec.secondCredential.label}
              description={spec.secondCredential.description}
              placeholder={spec.secondCredential.placeholder}
              isSet={!!data.bot_id_set}
              preview={data.bot_id_preview ?? ''}
              readOnly={ro}
              value={botId}
              onChange={setBotId}
              cleared={botIdClear}
              onClearedChange={setBotIdClear}
              setupLink={{ href: spec.setupGuide, label: i18nT('pages.settings.botChannelPanel.where_to_find_the_credential', { label: spec.secondCredential.label.toLowerCase() }) }}
            />
          )}
          <SecretField
            key={`bot-${formKey}`}
            label={spec.tokenLabel ?? i18nT('pages.settings.botChannelPanel.bot_token', { channel: spec.name })}
            description={spec.tokenDescription}
            placeholder={spec.tokenPlaceholder}
            isSet={data.bot_token_set}
            preview={data.bot_token_preview}
            readOnly={ro}
            value={botToken}
            onChange={setBotToken}
            cleared={botClear}
            onClearedChange={setBotClear}
            setupLink={{ href: spec.setupGuide, label: i18nT('pages.settings.botChannelPanel.where_to_find_the_bot_token') }}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Identity & access ── */}
      <SettingsSection title={i18nT('pages.settings.botChannelPanel.identity_access')}>
        <SettingsCard>
          {spec.allowAll && (
            <>
              <SettingsToggle
                label={spec.allowAll.label}
                description={spec.allowAll.description}
                checked={draft.allow_all_users}
                onChange={v => upd({ allow_all_users: v })}
                disabled={ro}
              />
              <div className="border-t border-border mt-4 pt-4" />
            </>
          )}
          <TagListEditor
            label={i18nT('pages.settings.botChannelPanel.allowed_user_ids')}
            description={spec.allowlistDescription}
            values={draft.allowed_user_ids}
            placeholder={spec.allowlistPlaceholder}
            onChange={v => upd({ allowed_user_ids: v })}
            validate={spec.allowlistValidate ?? (v => /^\d+$/.test(v))}
            readOnly={ro}
          />
          {spec.allowAll && draft.allow_all_users && (
            <p className="text-[12px] text-muted mt-2 mb-0">{spec.allowAll.bypassNote}</p>
          )}
          {spec.threadAllowlist && (
            <div className="border-t border-border mt-4 pt-4">
              <TagListEditor
                label={spec.threadAllowlist.label}
                description={spec.threadAllowlist.description}
                values={draft.allowed_thread_ids}
                placeholder={spec.threadAllowlist.placeholder}
                onChange={v => upd({ allowed_thread_ids: v })}
                validate={v => /^\d+$/.test(v)}
                readOnly={ro}
              />
              <p className="text-[12px] text-muted mt-2 mb-0">
                {spec.threadAllowlist.help}
              </p>
              <p className="text-[12px] text-warn mt-2 mb-0 flex items-start gap-1.5">
                <AlertTriangle size={13} className="flex-none mt-0.5" />
                <span>{spec.threadAllowlist.warning}</span>
              </p>
            </div>
          )}
        </SettingsCard>
      </SettingsSection>

      {/* ── Forum topics (optional; Telegram supergroups) ── */}
      {spec.forum && (
        <SettingsSection title={i18nT('pages.settings.botChannelPanel.forum_topics')}>
          <SettingsCard>
            <SettingsToggle
              label={spec.forum.toggleLabel}
              description={spec.forum.toggleDescription}
              checked={draft.allow_forum}
              onChange={v => upd({ allow_forum: v })}
              disabled={ro}
            />
            <div className="border-t border-border mt-4 pt-4">
              <TagListEditor
                label={spec.forum.allowlistLabel}
                description={spec.forum.allowlistDescription}
                values={draft.allowed_forum_chat_ids}
                placeholder={spec.forum.allowlistPlaceholder}
                onChange={v => upd({ allowed_forum_chat_ids: v })}
                // Supergroup chat_ids are negative — allow an optional leading
                // minus (a digits-only check would reject every valid id).
                validate={v => /^-?\d+$/.test(v)}
                readOnly={ro}
              />
              {draft.allow_forum && draft.allowed_forum_chat_ids.length === 0 && (
                <p className="text-[12px] text-warn mt-2 mb-0 flex items-start gap-1.5">
                  <AlertTriangle size={13} className="flex-none mt-0.5" />
                  <span>{spec.forum.emptyHint}</span>
                </p>
              )}
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {/* ── Behavior ── */}
      <SettingsSection title={i18nT('pages.settings.botChannelPanel.behavior')}>
        <SettingsCard>
          <SettingsInput
            label={i18nT('pages.settings.botChannelPanel.soft_context_threshold')}
            description={spec.thresholdDescription}
            value={draft.soft_threshold_pct}
            onChange={v => upd({ soft_threshold_pct: v })}
            placeholder="80"
            disabled={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
          {saveMut.isPending ? i18nT('pages.settings.botChannelPanel.saving') : i18nT('pages.settings.botChannelPanel.save_channel_settings', { channel: spec.name })}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {tokenVerified ? i18nT('pages.settings.botChannelPanel.verified_with_channel_and_saved', { channel: spec.name }) : restartHint ? i18nT('pages.settings.botChannelPanel.saved_restart_the_gateway_to_apply') : i18nT('pages.settings.botChannelPanel.saved')}
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
