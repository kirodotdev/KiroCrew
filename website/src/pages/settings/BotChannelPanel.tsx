import { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
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
  /** Shared-channel config (optional; only Discord sends these). */
  allowed_channel_ids?: string[]
  auto_thread?: boolean
  /** Progress-display config (optional; only Discord sends these). */
  reactions_enabled?: boolean
  show_thinking?: boolean
  soft_threshold_pct: number
  /** Spoken answers (optional; only channels declaring `voiceReplies` send it). */
  voice_replies?: boolean
  /** Telegram forum per-topic config (optional; only Telegram sends these). */
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
  /**
   * Group-chat scope (optional; only Feishu sends these). Distinct from
   * ``allow_forum``: that gates per-TOPIC routing inside one Telegram
   * supergroup, whereas this gates whole group conversations. Sharing one field
   * would make the panel's copy lie for whichever channel borrowed the other's.
   */
  allow_group?: boolean
  allowed_group_ids?: string[]
  /** When to answer inside an allow-listed forum topic (optional; Telegram). */
  forum_activation?: string
  /** Sidebar folder this channel's sessions are filed into ("" = off). */
  session_folder?: string
  /**
   * Optional-SDK state (only a channel declaring ``sdkExtra`` sends these).
   * ``sdk_installed`` false means the client library is not importable by the
   * gateway process, so the channel is skipped at boot however complete the rest
   * of the config is; ``sdk_install_supported`` false marks the environments
   * where a pip install cannot work at all (bundled desktop interpreter, no pip
   * module, PEP 668 externally-managed); ``sdk_install_command`` names the
   * gateway's OWN interpreter and is empty whenever it would not help.
   */
  sdk_installed?: boolean
  sdk_install_supported?: boolean
  sdk_install_command?: string
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
  /** Shared-channel config (optional; only Discord sends these). */
  allowed_channel_ids?: string[]
  auto_thread?: boolean
  /** Progress-display config (optional; only Discord sends these). */
  reactions_enabled?: boolean
  show_thinking?: boolean
  soft_threshold_pct: number
  voice_replies?: boolean
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
  forum_activation?: string
  allow_group?: boolean
  allowed_group_ids?: string[]
  session_folder?: string
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
  /**
   * Optional reasoning toggle. Present only for a channel that can render a
   * collapsed quote after the answer; a channel that omits it never sends the
   * field, so its save payload is unchanged.
   */
  showThinking?: { label: string; description: string }
  /**
   * Optional spoken-answer toggle. Present only for a channel that can upload
   * synthesized audio; a channel that omits it never sends the field.
   */
  voiceReplies?: { label: string; description: string }
  /** Optional shared-thread allow-list rendered below user access controls. */
  threadAllowlist?: {
    label: string
    description: string
    placeholder: string
    help: ReactNode
    warning: ReactNode
  }
  /**
   * Optional shared-channel allow-list (Discord server channels). When present,
   * the panel renders a channel-id tag editor plus the auto-thread toggle;
   * channels that omit it (Telegram, Webex) are unaffected and never send
   * ``allowed_channel_ids`` or ``auto_thread``.
   */
  sharedChannels?: {
    label: string
    description: string
    placeholder: string
    help: ReactNode
    warning: ReactNode
    autoThreadLabel: string
    autoThreadDescription: string
    /** Config path the auto-thread toggle writes, for `<SettingRef>` deep-links. */
    autoThreadConfigKey: string
    /**
     * Hint shown while auto-thread is off and channels are listed. An allowed
     * channel is only ever answered in a thread promoted from the message, so an
     * off toggle makes every listed channel inert rather than answered in place.
     */
    autoThreadOffHint: string
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
    /**
     * Optional activation selector: when the bot answers inside an allow-listed
     * topic. Values are the backend's own modes, so the option list and the
     * labels stay positionally paired.
     */
    activation?: {
      label: string
      description: string
      hint: string
      options: string[]
      optionLabels: string[]
    }
  }
  /**
   * Optional group-chat scope (Feishu group conversations). When present, the
   * panel renders an allow_group toggle plus a chat-id tag editor. Separate from
   * ``forum`` on purpose: a Telegram forum topic lives INSIDE one supergroup and
   * carries its own activation mode, while this is "may this channel serve group
   * conversations at all, and which ones". Channels that omit it never send
   * ``allow_group`` or ``allowed_group_ids``.
   */
  groupChats?: {
    toggleLabel: string
    toggleDescription: ReactNode
    allowlistLabel: string
    allowlistDescription: string
    allowlistPlaceholder: string
    /** Entry validator; ids are opaque so each channel supplies its own shape. */
    allowlistValidate?: (v: string) => boolean
    /**
     * Hint shown while the toggle is ON and the list is empty. Both fail closed,
     * so that combination serves no group at all — without the hint the panel
     * would look configured while doing nothing.
     */
    emptyHint: string
  }
  /**
   * Optional progress-display toggles rendered in the Behavior card: how much of
   * a running turn the channel shows (the phase-reaction ladder, and whether the
   * model's reasoning is surfaced). Channels that omit it are unaffected and
   * never send ``reactions_enabled`` or ``show_thinking``.
   */
  progressDisplay?: {
    reactionsLabel: string
    reactionsDescription: string
    /** Config path the reactions toggle writes, for `<SettingRef>` deep-links. */
    reactionsConfigKey: string
    thinkingLabel: string
    thinkingDescription: string
    /** Config path the thinking toggle writes, for `<SettingRef>` deep-links. */
    thinkingConfigKey: string
  }
  /**
   * Optional SDK extra. Present only for a channel whose client library ships
   * outside core (Feishu: lark-oapi), which is the one case where a fully
   * configured channel still cannot start. All the COPY lives in this shared
   * namespace parameterised on the channel and package names, so a channel
   * adopting the card adds no translation keys of its own — an object rather
   * than a bare string so a later field is not a breaking change.
   */
  sdkExtra?: {
    /** Distribution name as the user would type it, e.g. "lark-oapi". */
    packageLabel: string
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
  allowed_channel_ids: string[]
  auto_thread: boolean
  reactions_enabled: boolean
  show_thinking: boolean
  soft_threshold_pct: string
  voice_replies: boolean
  allow_forum: boolean
  allowed_forum_chat_ids: string[]
  forum_activation: string
  allow_group: boolean
  allowed_group_ids: string[]
  /** Whether this channel files its sessions in a folder at all (off = unfiled). */
  session_folder_on: boolean
  /** Folder name, kept while the toggle is off so turning it back on restores it. */
  session_folder: string
}

function draftFrom(c: BotChannelConfigData): Draft {
  return {
    enabled: c.enabled,
    allowed_user_ids: [...c.allowed_user_ids],
    allowed_thread_ids: [...(c.allowed_thread_ids ?? [])],
    allow_all_users: !!c.allow_all_users,
    allowed_channel_ids: [...(c.allowed_channel_ids ?? [])],
    // Defaults ON, matching the backend default: `!!c.auto_thread` would read a
    // channel that never sends the field as an explicit opt-out and then save
    // that false back over a config the user never touched.
    auto_thread: c.auto_thread ?? true,
    // Same default-ON reasoning as `auto_thread` above: an absent field means
    // "this channel does not send it", never "the user opted out".
    reactions_enabled: c.reactions_enabled ?? true,
    // Default OFF, so `!!` is the faithful read here: reasoning stays private
    // until someone asks for it.
    show_thinking: !!c.show_thinking,
    soft_threshold_pct: String(c.soft_threshold_pct),
    voice_replies: !!c.voice_replies,
    allow_forum: !!c.allow_forum,
    allowed_forum_chat_ids: [...(c.allowed_forum_chat_ids ?? [])],
    // Default OFF like the backend: an absent field means "this channel does
    // not send it", and defaulting a GROUP-access switch on would widen reach
    // for a channel that never asked for it.
    allow_group: !!c.allow_group,
    allowed_group_ids: [...(c.allowed_group_ids ?? [])],
    // Falls back to the backend's own default rather than to "", which is not a
    // valid mode and would post a value the loader then has to reject.
    forum_activation: c.forum_activation || 'always',
    // A configured name IS the on-state — the backend has one field, where ""
    // means off, so the toggle is derived rather than separately persisted.
    session_folder_on: !!c.session_folder,
    session_folder: c.session_folder ?? '',
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
    if (spec.sharedChannels) {
      payload.allowed_channel_ids = draft.allowed_channel_ids
      payload.auto_thread = draft.auto_thread
    }
    if (spec.showThinking) payload.show_thinking = draft.show_thinking
    if (spec.voiceReplies) payload.voice_replies = draft.voice_replies
    if (spec.forum) {
      payload.allow_forum = draft.allow_forum
      payload.allowed_forum_chat_ids = draft.allowed_forum_chat_ids
      if (spec.forum.activation) payload.forum_activation = draft.forum_activation
    }
    if (spec.groupChats) {
      payload.allow_group = draft.allow_group
      payload.allowed_group_ids = draft.allowed_group_ids
    }
    if (spec.progressDisplay) {
      payload.reactions_enabled = draft.reactions_enabled
      payload.show_thinking = draft.show_thinking
    }
    // Off sends "" (the field's off-state); on with a blank name falls back to
    // the channel's own name, which is what the toggle's description promises.
    payload.session_folder = draft.session_folder_on
      ? (draft.session_folder.trim() || spec.name)
      : ''
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    if (spec.secondCredential) {
      if (botIdClear) payload.bot_id_clear = true
      else if (botId.trim()) payload.bot_id = botId.trim()
    }
    saveMut.mutate(payload)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `spec` is excluded on purpose: the owning panel rebuilds it on every render (a module const cannot hold localised copy), and the fields read here are the channel's CAPABILITY flags, which are the same shape on every rebuild for a mounted panel. Listing it would recreate this handler every render while changing nothing it computes, and the spec's contract is that it appears in no dependency array.
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

      {/* ── Missing optional SDK ── */}
      {/*
        Strictly `=== false`: an older gateway omits the field entirely, and
        treating undefined as "missing" would tell every user of one to install a
        package they may already have.
      */}
      {spec.sdkExtra && data.sdk_installed === false && (
        <SettingsSection title={i18nT('pages.settings.botChannelPanel.sdk_missing', { channel: spec.name })}>
          <SettingsCard>
            {data.sdk_install_supported && data.sdk_install_command ? (
              <>
                <p className="text-[13px] text-text m-0">
                  {i18nT('pages.settings.botChannelPanel.sdk_missing_body', { channel: spec.name, package: spec.sdkExtra.packageLabel })}
                </p>
                {/*
                  The command names the gateway's own interpreter rather than a
                  bare `pip`, because installing into a different environment is
                  the failure this card exists to prevent — so it must stay
                  copyable verbatim: break-all, never truncated.
                */}
                <div className="flex items-start gap-2 mt-2 rounded-md border border-border bg-bg-elevated px-3 py-2">
                  <code className="flex-1 text-[12px] font-mono text-text break-all">{data.sdk_install_command}</code>
                  <CopyCommandButton text={data.sdk_install_command} />
                </div>
                <p className="text-[12px] text-muted mt-2 mb-0">
                  {i18nT('pages.settings.botChannelPanel.sdk_restart_after_install', { channel: spec.name })}
                </p>
              </>
            ) : (
              <p className="text-[13px] text-warn m-0 flex items-start gap-1.5">
                <AlertTriangle size={13} className="flex-none mt-0.5" />
                {i18nT('pages.settings.botChannelPanel.sdk_install_unsupported', { package: spec.sdkExtra.packageLabel })}
              </p>
            )}
          </SettingsCard>
        </SettingsSection>
      )}

      {/* ── Required ── */}
      <SettingsSection title={i18nT('pages.settings.botChannelPanel.required')}>
        <SettingsCard index={1}>
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
        <SettingsCard index={2}>
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

      {/* ── Shared channels (optional; Discord server channels) ── */}
      {spec.sharedChannels && (
        <SettingsSection title={i18nT('pages.settings.botChannelPanel.shared_channels')}>
          <SettingsCard index={3}>
            <TagListEditor
              label={spec.sharedChannels.label}
              description={spec.sharedChannels.description}
              values={draft.allowed_channel_ids}
              placeholder={spec.sharedChannels.placeholder}
              onChange={v => upd({ allowed_channel_ids: v })}
              validate={v => /^\d+$/.test(v)}
              readOnly={ro}
            />
            <p className="text-[12px] text-muted mt-2 mb-0">
              {spec.sharedChannels.help}
            </p>
            <p className="text-[12px] text-warn mt-2 mb-0 flex items-start gap-1.5">
              <AlertTriangle size={13} className="flex-none mt-0.5" />
              <span>{spec.sharedChannels.warning}</span>
            </p>
            <div className="border-t border-border mt-4 pt-4">
              <SettingsToggle
                label={spec.sharedChannels.autoThreadLabel}
                description={spec.sharedChannels.autoThreadDescription}
                configKey={spec.sharedChannels.autoThreadConfigKey}
                checked={draft.auto_thread}
                onChange={v => upd({ auto_thread: v })}
                disabled={ro}
              />
              {!draft.auto_thread && draft.allowed_channel_ids.length > 0 && (
                <p className="text-[12px] text-warn mt-2 mb-0 flex items-start gap-1.5">
                  <AlertTriangle size={13} className="flex-none mt-0.5" />
                  <span>{spec.sharedChannels.autoThreadOffHint}</span>
                </p>
              )}
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {/* ── Forum topics (optional; Telegram supergroups) ── */}
      {spec.forum && (
        <SettingsSection title={i18nT('pages.settings.botChannelPanel.forum_topics')}>
          <SettingsCard index={3}>
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
            {spec.forum.activation && (
              <div className="border-t border-border mt-4 pt-4">
                <SettingsSelect
                  label={spec.forum.activation.label}
                  description={spec.forum.activation.description}
                  hint={spec.forum.activation.hint}
                  value={draft.forum_activation}
                  options={spec.forum.activation.options}
                  optionLabels={spec.forum.activation.optionLabels}
                  onChange={v => upd({ forum_activation: v })}
                  disabled={ro}
                />
              </div>
            )}
          </SettingsCard>
        </SettingsSection>
      )}

      {/* ── Group chats (optional; Feishu group conversations) ── */}
      {spec.groupChats && (
        <SettingsSection title={i18nT('pages.settings.botChannelPanel.group_chats')}>
          <SettingsCard index={3}>
            <SettingsToggle
              label={spec.groupChats.toggleLabel}
              description={spec.groupChats.toggleDescription}
              checked={draft.allow_group}
              onChange={v => upd({ allow_group: v })}
              disabled={ro}
            />
            <div className="border-t border-border mt-4 pt-4">
              <TagListEditor
                label={spec.groupChats.allowlistLabel}
                description={spec.groupChats.allowlistDescription}
                values={draft.allowed_group_ids}
                placeholder={spec.groupChats.allowlistPlaceholder}
                onChange={v => upd({ allowed_group_ids: v })}
                validate={spec.groupChats.allowlistValidate}
                readOnly={ro}
              />
              {draft.allow_group && draft.allowed_group_ids.length === 0 && (
                <p className="text-[12px] text-warn mt-2 mb-0 flex items-start gap-1.5">
                  <AlertTriangle size={13} className="flex-none mt-0.5" />
                  <span>{spec.groupChats.emptyHint}</span>
                </p>
              )}
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {/* ── Behavior ── */}
      <SettingsSection title={i18nT('pages.settings.botChannelPanel.behavior')}>
        <SettingsCard index={4}>
          <SettingsInput
            label={i18nT('pages.settings.botChannelPanel.soft_context_threshold')}
            description={spec.thresholdDescription}
            value={draft.soft_threshold_pct}
            onChange={v => upd({ soft_threshold_pct: v })}
            placeholder="80"
            disabled={ro}
          />
          {spec.progressDisplay && (
            <>
              <SettingsToggle
                label={spec.progressDisplay.reactionsLabel}
                description={spec.progressDisplay.reactionsDescription}
                configKey={spec.progressDisplay.reactionsConfigKey}
                checked={draft.reactions_enabled}
                onChange={v => upd({ reactions_enabled: v })}
                disabled={ro}
              />
              <SettingsToggle
                label={spec.progressDisplay.thinkingLabel}
                description={spec.progressDisplay.thinkingDescription}
                configKey={spec.progressDisplay.thinkingConfigKey}
                checked={draft.show_thinking}
                onChange={v => upd({ show_thinking: v })}
                disabled={ro}
              />
            </>
          )}
          {spec.showThinking && (
            <div className="border-t border-border mt-4 pt-4">
              <SettingsToggle
                label={spec.showThinking.label}
                description={spec.showThinking.description}
                checked={draft.show_thinking}
                onChange={v => upd({ show_thinking: v })}
                disabled={ro}
              />
            </div>
          )}
          {spec.voiceReplies && (
            <div className="border-t border-border mt-4 pt-4">
              <SettingsToggle
                label={spec.voiceReplies.label}
                description={spec.voiceReplies.description}
                checked={draft.voice_replies}
                onChange={v => upd({ voice_replies: v })}
                disabled={ro}
              />
            </div>
          )}
          {/* Optional per-channel session filing. Off by default: sessions from
              this channel stay unfiled in the sidebar, as before. */}
          <div className="border-t border-border mt-4 pt-4">
            <SettingsToggle
              label={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder')}
              description={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder_desc', { channel: spec.name })}
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
                  placeholder={spec.name}
                  disabled={ro}
                />
              </div>
            )}
          </div>
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
