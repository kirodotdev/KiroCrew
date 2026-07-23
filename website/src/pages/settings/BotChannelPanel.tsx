import { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'

/** Config shape shared by every bot-token channel (Discord, Telegram, …). */
export interface BotChannelConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  bot_token_set: boolean
  bot_token_preview: string
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: number
}

/** Writable fields shared by every bot-token channel save endpoint. */
export interface BotChannelConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: number
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
  /** Primary guide-card button: label + href. */
  guideLink: { label: string; href: string }
  /** Secret field labels. */
  tokenDescription: string
  tokenPlaceholder: string
  /** Allowlist copy. */
  allowlistDescription: string
  allowlistPlaceholder: string
  /** Soft-threshold copy (command prefixes differ per channel). */
  thresholdDescription: string
  /** Fail-closed hint shown when enabled + token set but allowlist empty. */
  emptyAllowlistHint: string
  /** API calls. */
  getConfig: () => Promise<BotChannelConfigData>
  saveConfig: (body: Partial<BotChannelConfigSave>) => Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>
  /** Refresh cadence for the live status badge (ms); omit to disable. */
  refetchInterval?: number
}

type Draft = {
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: string
}

function draftFrom(c: BotChannelConfigData): Draft {
  return {
    enabled: c.enabled,
    allowed_user_ids: [...c.allowed_user_ids],
    soft_threshold_pct: String(c.soft_threshold_pct),
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
    return `${spec.name} channel failed to start (${config.connect_error}). Check the bot token and network access to ${spec.host}, then restart the gateway.`
  }
  if (config.configured) {
    return 'Configuration is saved but the channel is not running. Restart the gateway to connect.'
  }
  if (config.bot_token_set && config.enabled && config.allowed_user_ids.length === 0) {
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
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<BotChannelConfigSave>) => spec.saveConfig(body),
    onError: (e: unknown) => {
      // The API client throws with the raw response body; extract the
      // server's error field for clean display.
      let msg = 'Save failed. Is the gateway running?'
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
      setError('Soft context threshold must be a number between 1 and 100')
      setTimeout(() => setError(''), 8000)
      return
    }
    const payload: Partial<BotChannelConfigSave> = {
      enabled: draft.enabled,
      allowed_user_ids: draft.allowed_user_ids,
      soft_threshold_pct: pct,
    }
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    saveMut.mutate(payload)
  }, [draft, botToken, botClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">Loading {spec.name} config…</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">Cannot load {spec.name} config. Is the gateway running?</p>

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
            {spec.name} settings are managed on the machine running KiroCrew and are read-only from remote sessions.
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title="Get your bot token">
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
              Setup guide <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required ── */}
      <SettingsSection title="Required">
        <SettingsCard>
          <SettingsToggle
            label={`Enable ${spec.name}`}
            description={`Start the ${spec.name} channel at gateway startup (requires a bot token).`}
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <SecretField
            key={`bot-${formKey}`}
            label={`${spec.name} bot token`}
            description={spec.tokenDescription}
            placeholder={spec.tokenPlaceholder}
            isSet={data.bot_token_set}
            preview={data.bot_token_preview}
            readOnly={ro}
            value={botToken}
            onChange={setBotToken}
            cleared={botClear}
            onClearedChange={setBotClear}
            setupLink={{ href: spec.setupGuide, label: 'Where to find the bot token' }}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Identity & access ── */}
      <SettingsSection title="Identity & access">
        <SettingsCard>
          <TagListEditor
            label="Allowed user IDs"
            description={spec.allowlistDescription}
            values={draft.allowed_user_ids}
            placeholder={spec.allowlistPlaceholder}
            onChange={v => upd({ allowed_user_ids: v })}
            validate={v => /^\d+$/.test(v)}
            readOnly={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Behavior ── */}
      <SettingsSection title="Behavior">
        <SettingsCard>
          <SettingsInput
            label="Soft context threshold %"
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
          {saveMut.isPending ? 'Saving…' : `Save ${spec.name} settings`}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {tokenVerified ? `Verified with ${spec.name} and saved. Restart the gateway to connect.` : restartHint ? 'Saved. Restart the gateway to apply.' : 'Saved.'}
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
