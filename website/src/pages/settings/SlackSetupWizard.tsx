/**
 * Settings ▸ Slack setup wizard: one screen per step, from "have a workspace"
 * to "the bot answered a message". It writes through the same local-only
 * `PUT /api/slack/config` and `POST /api/slack/reconnect` the form below it
 * uses, so it adds no new write path. The panel offers it to local sessions
 * only; if it is ever rendered read-only, the steps show as text only.
 *
 * Saved credentials and a first owner apply with Reconnect, not a restart.
 * The slash command saved at Create applies without a restart only on a
 * gateway that hot-applies `slack.command`; elsewhere the step says a restart
 * is needed. The owner step offers Claim in Slack first and the member-ID
 * field as the fallback; an owner other than the one the gateway runs with
 * asks for a restart, since Reconnect refuses an owner change.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ExternalLink, RefreshCw } from 'lucide-react'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { Input, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { api, type SlackConfigData, type SlackConfigSave } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useSlackManifestStatus, forgetSlackInstallReading, checkFailed } from './SlackManifestUpdate'
import { SlackWizardDiagram } from './SlackWizardDiagram'
import { APP_NAME_MAX, BOT_NAME_MAX, DEFAULT_SLACK_NAME, slackCommandOk, slackNameOk } from './slackNames'


/** The scope an app-level token needs for Socket Mode: a Slack identifier, never translated. */
export const APP_TOKEN_SCOPE = 'connections:write'

/** A Slack member ID, as `PUT /api/slack/config` validates it. */
const MEMBER_ID_RE = /^[UW][A-Z0-9]+$/

/** Step order. The index is the screen; the name keys its copy. */
export const WIZARD_STEPS = ['workspace', 'create', 'bot_token', 'app_token', 'owner', 'test'] as const
export type WizardStep = (typeof WIZARD_STEPS)[number]
type Step = WizardStep

/** Each step's heading. Spelled out, not built from the step name, so every
 *  key is a literal the catalog checks can see. */
function stepTitle(step: Step): string {
  switch (step) {
    case 'workspace': return i18nT('pages.settings.slackWizard.step_workspace')
    case 'create': return i18nT('pages.settings.slackWizard.step_create')
    case 'bot_token': return i18nT('pages.settings.slackWizard.step_bot_token')
    case 'app_token': return i18nT('pages.settings.slackWizard.step_app_token')
    case 'owner': return i18nT('pages.settings.slackWizard.step_owner')
    case 'test': return i18nT('pages.settings.slackWizard.step_test')
  }
}

/** The first step the saved config has not done yet. */
export function firstOpenStep(c: SlackConfigData): number {
  if (!c.bot_token_set) return 0
  if (!c.app_token_set) return WIZARD_STEPS.indexOf('app_token')
  if (!c.owner_id || !c.connected) return WIZARD_STEPS.indexOf('owner')
  return WIZARD_STEPS.indexOf('test')
}

/** Only an `https://` link to a real host is ever rendered as the org guide. */
export function safeGuideUrl(raw: string | undefined | null): string | null {
  const url = raw ? safeHttpUrl(raw) : null
  return url && url.startsWith('https://') ? url : null
}

function errorText(e: unknown): string {
  if (e instanceof Error && e.message) {
    try {
      return String(JSON.parse(e.message).error ?? e.message)
    } catch {
      return e.message
    }
  }
  return i18nT('pages.settings.slackWizard.save_failed')
}

function LinkButton({ href, children, testId, onClick }: { href: string | null | undefined; children: ReactNode; testId?: string; onClick?: () => void }) {
  const ok = !!href
  return (
    <a
      data-testid={testId}
      onClick={ok ? onClick : undefined}
      href={href ?? '#'}
      target="_blank" rel="noopener noreferrer"
      aria-disabled={!ok}
      tabIndex={ok ? undefined : -1}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all ${ok ? 'bg-accent text-accent-fg border-accent hover:bg-accent-hover' : 'border-border text-muted pointer-events-none'}`}
    >
      {children} <ExternalLink size={13} />
    </a>
  )
}

function Done({ text }: { text: string }) {
  return (
    <p className="text-[12px] text-ok mt-2 flex items-center gap-1.5">
      <Check size={12} className="flex-none" /> {text}
    </p>
  )
}

export function SlackSetupWizard({ data, onSaved, orgGuideUrl, describeConnectError, onClose, renderOwnerClaim }: {
  data: SlackConfigData
  /** The panel's "Claim in Slack" control (main's owner claim), drawn on the
   *  owner step above the manual member-ID field. It is handed the callback
   *  to run when a claim confirms an owner, so the wizard can move on. */
  renderOwnerClaim?: (onOwnerChange: (ownerId: string) => void) => ReactNode
  /** Hides the wizard; offered on every step and again once setup is done. */
  onClose?: () => void
  /** The panel's sentence for a recorded connect failure code. */
  describeConnectError: (code: string) => string
  /** Called after every wizard save with the non-secret fields it stored, so
   *  the form below can take just those and keep the user's other edits. */
  onSaved: (saved: { owner_id?: string; command?: string; app_name?: string; bot_name?: string }) => void
  /** The optional organization setup guide (`slack.setup_guide_url`). */
  orgGuideUrl?: string | null
}) {
  const qc = useQueryClient()
  const ro = data.read_only
  const [step, setStep] = useState(() => firstOpenStep(data))
  // The names the manifest gives the new app, starting from this install's own.
  const [appName, setAppName] = useState(() => data.app_name ?? DEFAULT_SLACK_NAME)
  const [botName, setBotName] = useState(() => data.bot_name ?? DEFAULT_SLACK_NAME)
  const [command, setCommand] = useState(() => data.manifest_command || data.command || DEFAULT_SLACK_NAME)
  const [botToken, setBotToken] = useState('')
  const [appToken, setAppToken] = useState('')
  const [ownerId, setOwnerId] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [gotReply, setGotReply] = useState(false)
  const name: Step = WIZARD_STEPS[step]
  const guide = safeGuideUrl(orgGuideUrl)

  const names = { app_name: appName.trim(), bot_name: botName.trim(), command: command.trim() }
  const appNameOk = slackNameOk(appName, APP_NAME_MAX)
  const botNameOk = slackNameOk(botName, BOT_NAME_MAX)
  const commandOk = slackCommandOk(command)
  const namesOk = appNameOk && botNameOk && commandOk
  const createQ = useQuery({
    queryKey: ['slack-manifest', 'wizard', names.app_name, names.bot_name, names.command],
    queryFn: () => api.getSlackManifest(names),
    enabled: !ro && name === 'create' && namesOk,
    staleTime: Infinity,
    retry: false,
  })
  const statusQ = useSlackManifestStatus()
  const status = statusQ.isError ? undefined : statusQ.data
  // Only the steps that use its links say it failed: the request itself, or a
  // 200 reading whose Slack call failed (timeout, network), which also leaves
  // the links empty.
  const statusFailed = data.bot_token_set && (statusQ.isError || checkFailed(status))

  const saveMut = useMutation({
    mutationFn: (body: Partial<SlackConfigSave>) => api.saveSlackConfig(body),
    onMutate: () => { setError(''); setNotice('') },
    onError: (e: unknown) => setError(errorText(e)),
    onSuccess: (res, body) => {
      onSaved({
        ...(body.owner_id !== undefined ? { owner_id: body.owner_id } : {}),
        ...(body.command !== undefined ? { command: body.command } : {}),
        ...(body.app_name !== undefined ? { app_name: body.app_name } : {}),
        ...(body.bot_name !== undefined ? { bot_name: body.bot_name } : {}),
      })
      void qc.invalidateQueries({ queryKey: ['slack-config'] })
      void qc.invalidateQueries({ queryKey: ['slack-manifest'] })
      if (body.bot_token) void forgetSlackInstallReading(qc)
      if (body.command) {
        setStep(WIZARD_STEPS.indexOf('bot_token'))
        setNotice(res.restart_required ? i18nT('pages.settings.slackWizard.command_saved_restart') : '')
        return
      }
      if (res.verify_warning) setNotice(res.verify_warning)
      if (body.bot_token) { setBotToken(''); setStep(WIZARD_STEPS.indexOf('app_token')) }
      if (body.app_token) { setAppToken(''); setStep(WIZARD_STEPS.indexOf('owner')) }
      if (body.owner_id) {
        setOwnerId('')
        // A different owner than the one the gateway runs with applies only on
        // a restart, and Reconnect refuses it.
        if (res.restart_required) setNotice(i18nT('pages.settings.slackPanel.connect_error_owner_change_needs_restart'))
        else reconnectMut.mutate()
      }
    },
  })

  const reconnectMut = useMutation({
    mutationFn: () => api.reconnectSlack(),
    onMutate: () => setError(''),
    onSuccess: (res) => {
      void qc.invalidateQueries({ queryKey: ['slack-config'] })
      if (res.connected && !res.connect_error) {
        void qc.invalidateQueries({ queryKey: ['slack-manifest-status'] })
        setStep(WIZARD_STEPS.indexOf('test'))
      } else if (res.connect_error === 'invalid_auth') {
        // The panel's sentence points at its own token fields below; here the
        // tokens are pasted on earlier steps.
        setError(i18nT('pages.settings.slackWizard.reconnect_invalid_auth', { step: WIZARD_STEPS.indexOf('bot_token') + 1 }))
      } else {
        setError(i18nT('pages.settings.slackWizard.reconnect_failed', { reason: describeConnectError(res.connect_error) }))
      }
    },
    onError: (e: unknown) => setError(errorText(e)),
  })

  const busy = saveMut.isPending || reconnectMut.isPending

  // A claim confirmed here writes the owner and runs Reconnect itself; move on
  // once the refetched config shows both. Only a claim made on this step does:
  // going Back to the owner step on a connected install stays put.
  const claimedHere = useRef(false)
  useEffect(() => {
    if (claimedHere.current && name === 'owner' && data.owner_id && data.connected) {
      claimedHere.current = false
      setStep(WIZARD_STEPS.indexOf('test'))
    }
  }, [name, data.owner_id, data.connected])
  const go = (i: number) => { setError(''); setNotice(''); setStep(i) }

  let body: ReactNode = null
  if (name === 'workspace') {
    body = (
      <>
        <p className="text-[13px] text-text m-0">{i18nT('pages.settings.slackWizard.workspace_body')}</p>
        {guide && (
          <a data-testid="slack-wizard-org-guide" href={guide} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 mt-2 text-[13px] font-medium text-accent hover:underline">
            {i18nT('pages.settings.slackWizard.org_guide')} <ExternalLink size={13} />
          </a>
        )}
        {!ro && <div className="mt-3"><Btn primary onClick={() => go(1)}>{i18nT('pages.settings.slackWizard.have_workspace')}</Btn></div>}
      </>
    )
  } else if (name === 'create') {
    const rendered = namesOk ? createQ.data : undefined
    const field = (label: string, value: string, set: (v: string) => void, testId: string, error: string, prefix?: string) => (
      <>
        <label className="flex items-center gap-2 mt-2">
          <span className="text-[12px] text-muted w-28 flex-none">{label}</span>
          {/* A fixed slot, empty for the app name, so the three inputs line up. */}
          <span className="text-[13px] text-muted font-mono w-3 flex-none text-center" aria-hidden="true">{prefix}</span>
          <Input value={value} onChange={e => set(e.target.value)} placeholder={DEFAULT_SLACK_NAME}
            aria-label={label} aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${testId}-error` : undefined}
            className="min-w-0 flex-1 font-mono" data-testid={testId} />
        </label>
        {error && <p id={`${testId}-error`} className="text-[12px] text-warn mt-1 ml-[8.25rem]" data-testid={`${testId}-error`}>{error}</p>}
      </>
    )
    body = (
      <>
        <p className="text-[13px] text-text m-0">{i18nT('pages.settings.slackWizard.names_body')}</p>
        {!ro && (
          <>
            {field(i18nT('pages.settings.slackWizard.app_name_label'), appName, setAppName, 'slack-wizard-app-name',
              appNameOk ? '' : i18nT('pages.settings.slackWizard.app_name_invalid'))}
            {field(i18nT('pages.settings.slackWizard.bot_name_label'), botName, setBotName, 'slack-wizard-bot-name',
              botNameOk ? '' : i18nT('pages.settings.slackWizard.bot_name_invalid'), '@')}
            {/* Lowercased as typed, as the alias field was (#41310): Slack takes only
                lowercase commands, so the one saved is the one the user saw. A
                longer one is flagged rather than cut. */}
            {field(i18nT('pages.settings.slackWizard.command_label'), command, v => setCommand(v.toLowerCase()), 'slack-wizard-command',
              commandOk ? '' : i18nT('pages.settings.slackWizard.command_invalid'), '/')}
            <p className="text-[12px] text-muted mt-1">{i18nT('pages.settings.slackWizard.command_collision_hint')}</p>
            {/* No hand-off: the typed names and the token and owner inputs of the
                later steps live in this wizard's local state; the chat hand-off
                unmounts it and would lose them. */}
            {namesOk && createQ.isError && <ErrorNotice variant="inline" className="mt-2" message={i18nT('pages.settings.slackWizard.manifest_unavailable')} />}
            <div className="mt-3">
              <LinkButton href={rendered?.create_url} testId="slack-wizard-create">
                {i18nT('pages.settings.slackWizard.create_app')}
              </LinkButton>
            </div>
            <p className="text-[12px] text-muted mt-1">{i18nT('pages.settings.slackWizard.pick_workspace')}</p>
            {/* Saved here, not on the link's click: "Open in new tab" and a
                middle-click never fire onClick. Advances only once it is stored.
                The names saved are the ones the server rendered (an empty field
                is kirocrew there), so the gateway answers the app's command and
                later manifest updates keep its names. */}
            <div className="mt-3">
              <Btn primary disabled={busy || !rendered}
                onClick={() => {
                  if (rendered) saveMut.mutate({ command: rendered.command, app_name: rendered.app_name, bot_name: rendered.bot_name })
                }}>
                {i18nT('pages.settings.slackWizard.app_created')}
              </Btn>
            </div>
          </>
        )}
      </>
    )
  } else if (name === 'bot_token') {
    body = (
      <>
        <p className="text-[13px] text-text m-0">{i18nT('pages.settings.slackWizard.bot_token_body')}</p>
        <p className="text-[12px] text-muted mt-1">{i18nT('pages.settings.slackWizard.admin_approval')}</p>
        {!ro && (
          <div className="flex items-center gap-2 mt-2">
            <Input type="password" autoComplete="off" value={botToken} onChange={e => setBotToken(e.target.value)}
              placeholder={i18nT('pages.settings.slackWizard.bot_token_placeholder')} aria-label={i18nT('pages.settings.slackWizard.bot_token_label')}
              className="min-w-0 flex-1 font-mono" data-testid="slack-wizard-bot-token" />
            <Btn primary disabled={busy || !botToken.trim().startsWith('xoxb-')}
              onClick={() => saveMut.mutate({ bot_token: botToken.trim() })}>{i18nT('pages.settings.slackWizard.verify_and_save')}</Btn>
          </div>
        )}
        {data.bot_token_set && <Done text={i18nT('pages.settings.slackWizard.bot_token_saved')} />}
      </>
    )
  } else if (name === 'app_token') {
    body = (
      <>
        <p className="text-[13px] text-text m-0">{i18nT('pages.settings.slackWizard.app_token_body', { scope: APP_TOKEN_SCOPE })}</p>
        <div className="mt-2"><LinkButton href={status?.app_basic_info_url} testId="slack-wizard-basic-info">{i18nT('pages.settings.slackWizard.open_basic_info')}</LinkButton></div>
        {!status?.app_basic_info_url && <p className="text-[12px] text-muted mt-1">{i18nT('pages.settings.slackWizard.basic_info_fallback')}</p>}
        {!ro && (
          <div className="flex items-center gap-2 mt-2">
            <Input type="password" autoComplete="off" value={appToken} onChange={e => setAppToken(e.target.value)}
              placeholder={i18nT('pages.settings.slackWizard.app_token_placeholder')} aria-label={i18nT('pages.settings.slackWizard.app_token_label')}
              className="min-w-0 flex-1 font-mono" data-testid="slack-wizard-app-token" />
            <Btn primary disabled={busy || !appToken.trim().startsWith('xapp-')}
              onClick={() => saveMut.mutate({ app_token: appToken.trim() })}>{i18nT('pages.settings.slackWizard.verify_and_save')}</Btn>
          </div>
        )}
      </>
    )
  } else if (name === 'owner') {
    body = (
      <>
        {data.bot_token_set && data.app_token_set && <Done text={i18nT('pages.settings.slackWizard.tokens_verified_next_owner')} />}
        {renderOwnerClaim && (
          <div className="mt-2" data-testid="slack-wizard-owner-claim">
            {renderOwnerClaim(id => { if (id) claimedHere.current = true })}
          </div>
        )}
        <p className="text-[13px] text-text mt-2 mb-0">
          {renderOwnerClaim ? i18nT('pages.settings.slackWizard.owner_manual') : i18nT('pages.settings.slackWizard.owner_body')}
        </p>
        {!ro && (
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <Input value={ownerId} onChange={e => setOwnerId(e.target.value.trim())}
              placeholder={i18nT('pages.settings.slackWizard.owner_placeholder')} aria-label={i18nT('pages.settings.slackWizard.owner_label')}
              className="min-w-0 flex-1 font-mono" data-testid="slack-wizard-owner" />
            <Btn primary disabled={busy || !MEMBER_ID_RE.test(ownerId)}
              onClick={() => saveMut.mutate({ owner_id: ownerId })}>{i18nT('pages.settings.slackWizard.save_and_connect')}</Btn>
            {data.owner_id && (
              <Btn disabled={busy} onClick={() => reconnectMut.mutate()} data-testid="slack-wizard-reconnect">
                <RefreshCw size={13} /> {i18nT('pages.settings.slackWizard.connect')}
              </Btn>
            )}
          </div>
        )}
        {reconnectMut.isPending && <p className="text-[12px] text-muted mt-2">{i18nT('pages.settings.slackWizard.connecting')}</p>}
      </>
    )
  } else {
    body = (
      <>
        <p className="text-[13px] text-text m-0">{i18nT('pages.settings.slackWizard.test_body')}</p>
        <div className="mt-2"><LinkButton href={status?.app_dm_url} testId="slack-wizard-dm">{i18nT('pages.settings.slackWizard.open_dm')}</LinkButton></div>
        <p className="text-[12px] text-muted mt-2">{i18nT('pages.settings.slackWizard.clients_hint')}</p>
        {!ro && !gotReply && <div className="mt-3"><Btn primary onClick={() => setGotReply(true)}>{i18nT('pages.settings.slackWizard.got_reply')}</Btn></div>}
        {gotReply && <Done text={i18nT('pages.settings.slackWizard.all_set')} />}
        {!gotReply && (
          <div className="mt-3 flex items-center gap-2 flex-wrap" data-testid="slack-wizard-test-status">
            <span className={`text-[12px] ${data.connected ? 'text-ok' : 'text-warn'}`}>
              {data.connected ? i18nT('pages.settings.slackPanel.connected') : i18nT('pages.settings.slackPanel.not_connected')}
            </span>
            <span className="text-[12px] text-muted">{i18nT('pages.settings.slackWizard.no_reply_hint')}</span>
            {!ro && (
              <Btn disabled={busy} onClick={() => reconnectMut.mutate()} data-testid="slack-wizard-test-reconnect">
                <RefreshCw size={13} />
                {reconnectMut.isPending ? i18nT('pages.settings.slackPanel.reconnecting') : i18nT('pages.settings.slackPanel.reconnect')}
              </Btn>
            )}
          </div>
        )}
        {gotReply && onClose && <div className="mt-3"><Btn onClick={onClose} data-testid="slack-wizard-close">{i18nT('pages.settings.slackWizard.close')}</Btn></div>}
      </>
    )
  }

  return (
    <SettingsSection title={i18nT('pages.settings.slackWizard.title')}>
      <SettingsCard>
        <div data-testid="slack-wizard" data-step={name}>
          <div className="flex items-center justify-between gap-2 mb-1">
            <span className="text-[12px] text-muted">
              {i18nT('pages.settings.slackWizard.step_of', { step: step + 1, total: WIZARD_STEPS.length })}
            </span>
            {/* The way out on every step, for every viewer, apart from the
                Back / Next row: the panel's own form stays below. */}
            {onClose && (
              <button type="button" onClick={onClose} data-testid="slack-wizard-close-header"
                className="text-[12px] text-muted hover:text-text hover:underline">
                {i18nT('pages.settings.slackWizard.close')}
              </button>
            )}
          </div>
          <h4 className="text-[14px] font-semibold text-text-strong mt-0 mb-2">{stepTitle(name)}</h4>
          {body}
          <SlackWizardDiagram step={name} />
          {notice && <p className="text-[12px] text-warn mt-2">{notice}</p>}
          {/* No hand-off: a refused save or Reconnect leaves the alias, bot
              token, app-level token or member ID the user just typed in this
              wizard's local state; the chat hand-off unmounts it and would lose
              them. The step stays put so the user can correct and retry. */}
          <ErrorNotice variant="inline" className="mt-2" message={error} />
          {/* No hand-off, for the same unsaved wizard inputs as above. Without
              the install reading the Basic Information and DM links stay off. */}
          {statusFailed && (name === 'app_token' || name === 'test') && (
            <ErrorNotice variant="inline" className="mt-2" message={i18nT('pages.settings.slackPanel.slackCode.status_load_failed')} />
          )}
          <div className="flex items-center gap-2 mt-3">
            {step > 0 && <Btn onClick={() => go(step - 1)}>{i18nT('pages.settings.slackWizard.back')}</Btn>}
            {/* A read-only viewer has none of the steps' own controls; Next
                only changes the screen and writes nothing. */}
            {ro && step + 1 < WIZARD_STEPS.length && (
              <Btn onClick={() => go(step + 1)} data-testid="slack-wizard-next">{i18nT('pages.settings.slackWizard.next_step')}</Btn>
            )}
          </div>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
