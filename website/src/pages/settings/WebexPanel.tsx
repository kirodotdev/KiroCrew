import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { WebexIcon } from '../../components/WebexIcon'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'
import { api, type WebexConfigData, type WebexConfigSave } from '../../api/client'

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

/** One-line explanation of WHY Webex is not active, with the fix. */
function connectionHint(config: WebexConfigData): string {
  if (config.connected || !config.configured) return ''
  if (config.connect_error) {
    return `Webex connection failed (${config.connect_error}). Check the bot token and network access to webexapis.com, then restart the gateway.`
  }
  return 'Settings are saved but the channel is not running. Restart the gateway to connect.'
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

  if (isLoading) return <p className="text-[13px] text-muted p-4">Loading Webex config…</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">Cannot load Webex config. Is the gateway running?</p>

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
            <h3 className="text-[15px] font-semibold text-text-strong">Webex</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            Talk to your agents from Cisco Webex. No public URL needed — KiroCrew receives
            messages over an outbound WebSocket, so it works behind a firewall.
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
            Webex settings are managed on the machine running KiroCrew and are read-only from remote sessions.
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title="Get your credentials">
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            Create a bot on the Webex developer portal (name, username, icon), then paste
            its access token here. The token is shown only once on the confirmation page —
            you can regenerate it later from the bot's edit page.
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href={CREATE_BOT_URL}
              target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all bg-accent text-accent-fg border-accent hover:bg-accent-hover"
            >
              Create Webex bot <ExternalLink size={13} />
            </a>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              Setup guide <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required token ── */}
      <SettingsSection title="Required">
        <SettingsCard>
          <SecretField
            key={`bot-${formKey}`}
            label="Webex bot token"
            description="Bot access token from developer.webex.com (My Webex Apps)."
            placeholder="Paste Webex bot access token"
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

      {/* ── Access ── */}
      <SettingsSection title="Access">
        <SettingsCard>
          <SettingsToggle
            label="Enable Webex channel"
            description="Start the channel at gateway boot when a token is set."
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <TagListEditor
            label="Allowed emails"
            description="Webex account emails permitted to DM the bot. Empty = nobody (fail closed) — anyone in an org can message a Webex bot, so add only your own."
            values={draft.allowed_emails}
            placeholder="you@example.com"
            onChange={v => upd({ allowed_emails: v })}
            validate={isValidEmail}
            readOnly={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
          {saveMut.isPending ? 'Saving…' : 'Save Webex settings'}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {tokenVerified ? 'Verified with Webex and saved. Restart the gateway to connect.' : restartHint ? 'Saved. Restart the gateway to apply.' : 'Saved.'}
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
