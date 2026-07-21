import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { DiscordIcon } from '../../components/DiscordIcon'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'
import { api, type DiscordConfigData, type DiscordConfigSave } from '../../api/client'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/discord-integration.md'

type Draft = {
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: string
}

function draftFrom(c: DiscordConfigData): Draft {
  return {
    enabled: c.enabled,
    allowed_user_ids: [...c.allowed_user_ids],
    soft_threshold_pct: String(c.soft_threshold_pct),
  }
}

/** Status pill mirroring the run state of the Discord channel. */
function StatusBadge({ config }: { config: DiscordConfigData }) {
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

/** One-line explanation of WHY Discord is not running, with the fix. */
function connectionHint(config: DiscordConfigData): string {
  if (config.connected) return ''
  if (config.connect_error) {
    return `Discord channel failed to start (${config.connect_error}). Check the bot token and network access to discord.com, then restart the gateway.`
  }
  if (config.configured) {
    return 'Configuration is saved but the channel is not running. Restart the gateway to connect.'
  }
  if (config.bot_token_set && config.enabled && config.allowed_user_ids.length === 0) {
    return 'No allowed user IDs: the bot rejects every message (fail closed). Add your Discord user ID below.'
  }
  return ''
}

/** Discord channel-integration settings. */
export function DiscordPanel() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<DiscordConfigData>({
    queryKey: ['discord-config'],
    queryFn: api.getDiscordConfig,
    retry: false,
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
    mutationFn: (body: Partial<DiscordConfigSave>) => api.saveDiscordConfig(body),
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
      qc.invalidateQueries({ queryKey: ['discord-config'] })
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
    const payload: Partial<DiscordConfigSave> = {
      enabled: draft.enabled,
      allowed_user_ids: draft.allowed_user_ids,
      soft_threshold_pct: pct,
    }
    if (botClear) payload.bot_token_clear = true
    else if (botToken.trim()) payload.bot_token = botToken.trim()
    saveMut.mutate(payload)
  }, [draft, botToken, botClear, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">Loading Discord config…</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">Cannot load Discord config. Is the gateway running?</p>

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none">
          <DiscordIcon size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">Discord</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            Talk to your agent from Discord DMs over the Gateway WebSocket (no webhook or
            public address needed). Add allowed user IDs so the bot can respond.
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
            Discord settings are managed on the machine running KiroCrew and are read-only from remote sessions.
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title="Get your bot token">
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            Create an app in the Discord Developer Portal, open the{' '}
            <span className="font-mono">Bot</span> page, and click{' '}
            <span className="font-mono">Reset Token</span> — paste the token below.
            No privileged intents are needed (the bot is DM-only). Invite the bot to a
            server you share, or use its install link. To find your user ID, enable
            Developer Mode in Discord settings, then right-click your name and choose{' '}
            <span className="font-mono">Copy User ID</span>.
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href="https://discord.com/developers/applications"
              target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border bg-accent text-accent-fg border-accent hover:bg-accent-hover transition-all"
            >
              Open Developer Portal <ExternalLink size={13} />
            </a>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
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
            label="Enable Discord"
            description="Start the Discord channel at gateway startup (requires a bot token)."
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <SecretField
            key={`bot-${formKey}`}
            label="Discord bot token"
            description="From the Developer Portal's Bot page (Reset Token to view it once)."
            placeholder="Paste Discord bot token"
            isSet={data.bot_token_set}
            preview={data.bot_token_preview}
            readOnly={ro}
            value={botToken}
            onChange={setBotToken}
            cleared={botClear}
            onClearedChange={setBotClear}
            setupLink={{ href: SETUP_GUIDE, label: 'Where to find the bot token' }}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Identity & access ── */}
      <SettingsSection title="Identity & access">
        <SettingsCard>
          <TagListEditor
            label="Allowed user IDs"
            description="Discord user IDs permitted to DM the bot. Empty = deny all (fail closed): anyone sharing a server with the bot can DM it."
            values={draft.allowed_user_ids}
            placeholder="123456789012345678"
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
            description="Prompt to !compact or !new when the session context passes this percentage."
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
          {saveMut.isPending ? 'Saving…' : 'Save Discord settings'}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {tokenVerified ? 'Verified with Discord and saved. Restart the gateway to connect.' : restartHint ? 'Saved. Restart the gateway to apply.' : 'Saved.'}
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
