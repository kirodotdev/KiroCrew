import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock } from 'lucide-react'
import { TeamsIcon } from '../../components/TeamsIcon'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'
import { api, type TeamsConfigData, type TeamsConfigSave } from '../../api/client'

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
    return `Teams credential check failed (${config.connect_error}). Verify the App ID / password / tenant, then restart the gateway.`
  }
  return 'Settings are saved but the channel is not running. Restart the gateway to connect.'
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

  if (isLoading) return <p className="text-[13px] text-muted p-4">Loading Teams config…</p>
  if (isError || !data || !draft)
    return <p className="text-[13px] text-danger p-4">Cannot load Teams config. Is the gateway running?</p>

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
            <h3 className="text-[15px] font-semibold text-text-strong">Microsoft Teams</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            Talk to your agents from a Teams 1:1 chat. Self-hosted via the Bot Framework:
            Teams posts to your gateway's HTTPS webhook, so it needs a public endpoint
            (reverse proxy, App Service, or a dev tunnel).
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
            Teams settings are managed on the machine running KiroCrew and are read-only from remote sessions.
          </span>
        </div>
      )}

      {/* ── Credentials guide ── */}
      <SettingsSection title="Get your credentials">
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            Register an Azure Bot, add the <strong>Microsoft Teams</strong> channel, and set
            its messaging endpoint to your public URL + <code>{WEBHOOK_PATH}</code>. Copy the
            App (Client) ID, create a client secret, and note the tenant id for a
            single-tenant bot.
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a
              href={AZURE_BOT_URL}
              target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all bg-accent text-accent-fg border-accent hover:bg-accent-hover"
            >
              Create Azure Bot <ExternalLink size={13} />
            </a>
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              Setup guide <ExternalLink size={13} />
            </a>
          </div>
          <p className="text-[12px] text-muted mt-2 mb-0">
            Messaging endpoint: <code>https://&lt;your-host&gt;{WEBHOOK_PATH}</code>
          </p>
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 mt-3">
            <AlertTriangle size={13} className="lucide-inline text-warning flex-none mt-0.5" />
            <span className="text-[12px] text-text">
              <strong>Requires a public HTTPS URL.</strong> Teams delivers messages by
              calling this endpoint from the internet, so a <code>localhost</code> or
              SSH-tunneled address won&apos;t work. Expose the gateway with a tunnel
              (ngrok / cloudflared) or a reverse proxy, then use that public host as
              <code>&lt;your-host&gt;</code> above. Unlike Slack, the Bot Framework has no
              outbound-only mode.
            </span>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Bot setup steps ── */}
      <SettingsSection title="Connect the Azure Bot">
        <SettingsCard>
          <ol className="text-[13px] text-text m-0 pl-5 space-y-1.5 list-decimal">
            <li>
              Expose this gateway over <strong>public HTTPS</strong> (tunnel or reverse
              proxy) — see the note above. Note the resulting host.
            </li>
            <li>
              <strong>Create an Azure Bot</strong> (button above) as{' '}
              <strong>Multi-tenant</strong>, or single-tenant if you&apos;ll pin a tenant id.
            </li>
            <li>
              In the bot&apos;s <strong>Configuration</strong>, set the{' '}
              <strong>Messaging endpoint</strong> to{' '}
              <code>https://&lt;your-host&gt;{WEBHOOK_PATH}</code>.
            </li>
            <li>
              Under <strong>Certificates &amp; secrets</strong>, create a client secret and
              paste it as the <strong>App password</strong> below. Copy the{' '}
              <strong>App (Client) ID</strong> from the bot&apos;s overview (and the{' '}
              <strong>Tenant ID</strong> if single-tenant).
            </li>
            <li>
              Under <strong>Channels</strong>, add the <strong>Microsoft Teams</strong>{' '}
              channel.
            </li>
            <li>
              Fill in the credentials below, add yourself to the allow-list (email or AAD
              object id), toggle <strong>Enable</strong>, and Save.
            </li>
            <li>
              Side-load a Teams app whose <code>botId</code> is your App ID, then DM the
              bot — full manifest steps in the setup guide above.
            </li>
          </ol>
        </SettingsCard>
      </SettingsSection>

      {/* ── Required credentials ── */}
      <SettingsSection title="Required">
        <SettingsCard>
          <label htmlFor="teams-app-id" className="flex flex-col gap-1.5 py-1.5 text-[13px] font-semibold text-text">
            App (Client) ID
            <input
              id="teams-app-id"
              aria-label="App (Client) ID"
              className={inputCls}
              type="text"
              placeholder={data.app_id_set ? '•••••• (set — paste to replace)' : 'Microsoft App ID'}
              value={draft.app_id}
              disabled={ro}
              onChange={e => upd({ app_id: e.target.value })}
            />
          </label>
          <SecretField
            key={`pw-${formKey}`}
            label="App password (client secret)"
            description="Azure Bot client secret. Stored only in .env (never config.json)."
            placeholder="Paste Azure Bot client secret"
            isSet={data.app_password_set}
            preview=""
            readOnly={ro}
            value={appPassword}
            onChange={setAppPassword}
            cleared={pwClear}
            onClearedChange={setPwClear}
            setupLink={{ href: SETUP_GUIDE, label: 'Where to find the client secret' }}
          />
          <label htmlFor="teams-tenant-id" className="flex flex-col gap-1.5 py-1.5 text-[13px] font-semibold text-text">
            Tenant ID
            <span className="text-[12px] font-normal text-muted -mt-0.5">Optional — only for single-tenant bots.</span>
            <input
              id="teams-tenant-id"
              aria-label="Tenant ID"
              className={inputCls}
              type="text"
              placeholder="Leave empty for a multi-tenant bot"
              value={draft.tenant_id}
              disabled={ro}
              onChange={e => upd({ tenant_id: e.target.value })}
            />
          </label>
        </SettingsCard>
      </SettingsSection>

      {/* ── Access ── */}
      <SettingsSection title="Access">
        <SettingsCard>
          <SettingsToggle
            label="Enable Teams channel"
            description="Start the channel at gateway boot when the App ID + password are set."
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <TagListEditor
            label="Allowed users (email or AAD object id)"
            description="Azure AD UPNs/emails OR object ids permitted to DM the bot. Teams activities reliably carry the object id (email is often absent), so object ids work out of the box. Empty = nobody (fail closed)."
            values={draft.allowed_emails}
            placeholder="you@example.com or 00000000-0000-0000-0000-000000000000"
            onChange={v => upd({ allowed_emails: v })}
            validate={isValidPrincipal}
            readOnly={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only remote sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
          {saveMut.isPending ? 'Saving…' : 'Save Teams settings'}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {restartHint ? 'Saved. Restart the gateway to apply.' : 'Saved.'}
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
