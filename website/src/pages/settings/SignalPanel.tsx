import { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { ExternalLink, Check, AlertTriangle, Lock, PackageX, Smartphone, Link2, Info } from 'lucide-react'
import { SignalLogo } from '../../components/SignalLogo'
import { SettingsSection, SettingsCard, SettingsInput, SettingsToggle } from '../../components/settings'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { TagListEditor } from './SlackPanel'
import { api, type SignalConfigData, type SignalConfigSave, type SignalOperatorMode } from '../../api/client'

import { i18nT } from '../../i18n/t'
import { ChannelFolderBackfill } from './ChannelFolderBackfill'
import { signalListRecipientsCommand, signalReceiveCommand } from './signalCliCommand'
/** Brand name — do-not-translate, so it lives here rather than in the catalog. */
const CHANNEL_NAME = 'Signal'
const CLI_URL = 'https://github.com/AsamK/signal-cli'
const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/signal-integration.md'
/** The operator's override for the signal-cli executable. An identifier the
 *  reader types into a shell, so it is interpolated rather than translated. */
const CLI_ENV = 'KIROCREW_SIGNAL_CLI'
/** Example numbers for the inputs, from the UK's range reserved for fiction:
 *  data, not copy. The catalog wraps each as an example ("e.g. …"), and the two
 *  differ so neither reads as the other field's saved value. */
const ACCOUNT_EXAMPLE = '+44 7700 900123'
const ALLOWED_EXAMPLE = '+44 7700 900456'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const E164_RE = /^\+[0-9]{6,15}$/

/**
 * The canonical form the backend stores and the transport compares: an ACI
 * lowercased, a number with its formatting stripped and a leading `00` read as
 * `+`. Mirrors `signal_cli.identifiers.normalize_identifier`; both patterns are
 * anchored and free of nested quantifiers, so they cannot backtrack.
 */
export function normalizeSignalId(raw: string): string {
  const v = raw.trim()
  if (UUID_RE.test(v)) return v.toLowerCase()
  const n = v.replace(/[\s().-]/g, '')
  return n.startsWith('00') && /^\d+$/.test(n.slice(2)) ? `+${n.slice(2)}` : n
}

/** A `+E.164` number. The account must be one: signal-cli runs as a number. */
export function isSignalNumber(raw: string): boolean {
  return E164_RE.test(normalizeSignalId(raw))
}

/** A `+E.164` number or a Signal ACI — what the allow-list accepts. */
export function isSignalId(raw: string): boolean {
  const n = normalizeSignalId(raw)
  return E164_RE.test(n) || UUID_RE.test(n)
}

/**
 * The allow-list as the transport reads it: entries that are neither a number
 * nor an ACI dropped (`allowed_numbers_from_config`), the rest canonical and
 * deduplicated.
 */
export function signalRoster(entries: readonly string[]): Set<string> {
  return new Set(entries.filter(isSignalId).map(normalizeSignalId))
}

/**
 * Who the operator is for this allow-list and account: a mirror of
 * `signal_cli.transport.operator_mode`, for the draft being edited. The account
 * itself when it is on the list (the linked setup), else the sole entry, else
 * nobody. Both sides are compared canonically, so `+44 7700 900123` and
 * `0044.7700.900123` are the same number.
 */
export function signalOperatorMode(allowed: readonly string[], account: string): SignalOperatorMode {
  const roster = signalRoster(allowed)
  if (roster.size === 0) return 'none'
  const self = account.trim() ? normalizeSignalId(account) : ''
  if (self && roster.has(self)) return 'account'
  return roster.size === 1 ? 'sole_entry' : 'none'
}

/** Whether the draft's account and allow-list are what the server holds. */
function draftIsSaved(draft: Draft, data: SignalConfigData): boolean {
  const canon = (v: string) => (v.trim() ? normalizeSignalId(v) : '')
  const a = draft.allowed_numbers.map(canon)
  const b = data.allowed_numbers.map(canon)
  return canon(draft.account) === canon(data.account ?? '')
    && a.length === b.length && a.every((v, i) => v === b[i])
}

type Draft = {
  enabled: boolean
  account: string
  allowed_numbers: string[]
  /** Whether this channel files its sessions in a folder at all (off = unfiled). */
  session_folder_on: boolean
  /** Folder name, kept while the toggle is off so turning it back on restores it. */
  session_folder: string
}

function draftFrom(c: SignalConfigData): Draft {
  return {
    enabled: c.enabled,
    account: c.account ?? '',
    allowed_numbers: [...c.allowed_numbers],
    // A configured name IS the on-state — the backend has one field, where ""
    // means off, so the toggle is derived rather than separately persisted.
    session_folder_on: !!c.session_folder,
    session_folder: c.session_folder ?? '',
  }
}

/** Status pill mirroring the other channel panels' connection states. */
function StatusBadge({ config }: { config: SignalConfigData }) {
  let tone: [string, string, string]
  if (config.connected) tone = ['var(--ok)', i18nT('pages.settings.signalPanel.active'), 'text-ok']
  else if (config.configured) tone = ['var(--warn)', i18nT('pages.settings.signalPanel.not_active'), 'text-warn']
  else tone = ['var(--muted)', i18nT('pages.settings.signalPanel.needs_setup'), 'text-muted']
  const [dot, text, cls] = tone
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/** Why the configured channel is not up: signal-cli's own failure first. */
function startupProblem(config: SignalConfigData): { error: string; hint: string } {
  if (config.connected || !config.configured) return { error: '', hint: '' }
  if (config.connect_error) {
    // signal-cli's reason loses its own full stop, which the sentence around it
    // supplies.
    const error = config.connect_error.trim().replace(/\.+$/, '')
    return { error: i18nT('pages.settings.signalPanel.connection_failed', { error }), hint: '' }
  }
  return { error: '', hint: i18nT('pages.settings.signalPanel.saved_but_not_running') }
}

/** A sentence from the catalog with one signal-cli command set in `<code>`, so
 *  the command reads as one unit in any language and is never translated. */
function CommandSentence({ i18nKey, command }: { i18nKey: string; command: string }) {
  return (
    <Trans
      i18nKey={i18nKey}
      values={{ command }}
      components={{ command: <code className="font-mono break-words" /> }}
    />
  )
}

/** A failed save's message: a known `code` reads in the UI language, anything
 *  else falls back to the server's own text. */
function saveErrorMessage(e: unknown): string {
  if (!(e instanceof Error) || !e.message) return i18nT('pages.settings.signalPanel.save_failed')
  let body: { error?: string; code?: string } | null = null
  try {
    body = JSON.parse(e.message)
  } catch {
    return e.message
  }
  if (body?.code === 'invalid_account') return i18nT('pages.settings.signalPanel.account_invalid')
  if (body?.code === 'invalid_number') return i18nT('pages.settings.signalPanel.number_invalid')
  return body?.error ?? e.message
}

/** One of the two supported setups, as an icon-led paragraph. */
function SetupOption({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return (
    <div className="flex items-start gap-2.5 mt-3">
      <span className="text-muted mt-0.5 flex-none">{icon}</span>
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-text">{title}</div>
        <p className="text-[12px] text-muted m-0 mt-0.5">{body}</p>
      </div>
    </div>
  )
}

/** A one-line warning under a field. */
function FieldWarning({ children }: { children: ReactNode }) {
  return (
    <p className="text-[12px] text-warn mt-2 mb-0 flex items-center gap-1.5">
      <AlertTriangle size={12} className="flex-none" />
      {children}
    </p>
  )
}

/** Signal channel-integration settings (a local signal-cli; no token). */
export function SignalPanel() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<SignalConfigData>({
    queryKey: ['signal-config'],
    queryFn: api.getSignalConfig,
    retry: false,
    // An ambient focus refetch mid-edit would hand back a fresh `data`
    // object and clobber unsaved edits via the sync effect below.
    refetchOnWindowFocus: false,
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [saved, setSaved] = useState(false)
  const [restartHint, setRestartHint] = useState(false)
  const [error, setError] = useState('')

  // Seed the local draft once, when server config first arrives. Nothing
  // re-arms it, so neither a background refetch nor the post-save invalidation
  // can discard edits made since (the IMessagePanel contract).
  const syncArmed = useRef(true)
  useEffect(() => {
    if (data && syncArmed.current) {
      syncArmed.current = false
      setDraft(draftFrom(data))
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<SignalConfigSave>) => api.saveSignalConfig(body),
    // Persist until the next save attempt clears it: the rejected draft is
    // still in the form.
    onError: (e: unknown) => setError(saveErrorMessage(e)),
    onSuccess: (res) => {
      setSaved(true)
      setRestartHint(!!res.restart_required)
      setTimeout(() => setSaved(false), 6000)
      qc.invalidateQueries({ queryKey: ['signal-config'] })
    },
  })

  const accountValid = !draft?.account.trim() || isSignalNumber(draft.account)

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    saveMut.mutate({
      enabled: draft.enabled,
      account: draft.account.trim(),
      allowed_numbers: draft.allowed_numbers,
      // Off sends "" (the field's off-state); on with a blank name falls back
      // to "Signal", which is what the toggle's description promises.
      session_folder: draft.session_folder_on ? (draft.session_folder.trim() || CHANNEL_NAME) : '',
    })
  }, [draft, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.signalPanel.loading')}</p>
  // Nothing to lose here: the form is not mounted in this branch.
  if (isError || !data || !draft) return <ErrorNotice className="m-4" message={i18nT('pages.settings.signalPanel.cannot_load')} askAgent />

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  const ro = data.read_only
  const { error: startupError, hint } = startupProblem(data)
  const account = draft.account.trim() ? normalizeSignalId(draft.account) : ''
  // One line under an EMPTY allow-list, never two: an enabled channel warns
  // that it answers nobody (which already says to add your own number); a
  // disabled one gets the linked-device tip instead. A non-empty list says
  // nothing, because a dedicated bot number correctly omits its own number.
  const allowListEmpty = draft.allowed_numbers.length === 0
  // Operator scope rides on the list's size: with a bot number, a second
  // allowed number leaves nobody able to approve tools or /stop. The backend's
  // own answer is used while it describes this form (connected, nothing
  // edited); the mirror answers for a draft.
  const operatorMode = data.connected && data.operator_mode && draftIsSaved(draft, data)
    ? data.operator_mode
    : signalOperatorMode(draft.allowed_numbers, draft.account)
  const noOperator = operatorMode === 'none' && signalRoster(draft.allowed_numbers).size > 1

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none text-text">
          <SignalLogo size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">{CHANNEL_NAME}</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            {i18nT('pages.settings.signalPanel.tagline')}
          </p>
          {/* No hand-off: the unsaved `draft` (account, allowed_numbers,
              session folder) lives in this panel's local state — navigating
              to the chat would discard it. */}
          <ErrorNotice
            message={startupError}
            className="mt-2"
            footer={startupError && (
              <CommandSentence
                i18nKey="pages.settings.signalPanel.connection_failed_check"
                command={signalReceiveCommand(data.account)}
              />
            )}
          />
          {hint && (
            <p className="text-[12px] text-warn mt-1 flex items-center gap-1.5">
              <AlertTriangle size={12} className="flex-none" />
              {hint}
            </p>
          )}
        </div>
      </div>

      {/* ── signal-cli missing (the binary is never a setting) ── */}
      {!data.cli_found && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mb-3">
          <PackageX size={13} className="text-warn flex-none" />
          <span className="text-[12px] text-muted">
            {i18nT('pages.settings.signalPanel.cli_missing', { env: CLI_ENV })}
          </span>
        </div>
      )}

      {/* ── Read-only notice (remote session) ── */}
      {ro && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mb-3">
          <Lock size={13} className="text-muted flex-none" />
          <span className="text-[12px] text-muted">
            {i18nT('pages.settings.signalPanel.managed_locally')}
          </span>
        </div>
      )}

      {/* ── Prerequisites: the two setups ── */}
      <SettingsSection title={i18nT('pages.settings.signalPanel.before_you_start')}>
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            {i18nT('pages.settings.signalPanel.prerequisites_body')}
          </p>
          <SetupOption
            icon={<Smartphone size={14} />}
            title={i18nT('pages.settings.signalPanel.setup_dedicated_title')}
            body={i18nT('pages.settings.signalPanel.setup_dedicated_body')}
          />
          <SetupOption
            icon={<Link2 size={14} />}
            title={i18nT('pages.settings.signalPanel.setup_linked_title')}
            body={i18nT('pages.settings.signalPanel.setup_linked_body')}
          />
          <div className="flex items-center gap-2 mt-3 flex-wrap">
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all bg-accent text-accent-fg border-accent hover:bg-accent-hover">
              {i18nT('pages.settings.signalPanel.setup_guide')} <ExternalLink size={13} />
            </a>
            <a href={CLI_URL} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.signalPanel.cli_project')} <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Account and access ── */}
      <SettingsSection title={i18nT('pages.settings.signalPanel.access')}>
        <SettingsCard index={1}>
          <SettingsToggle
            label={i18nT('pages.settings.signalPanel.enable')}
            description={i18nT('pages.settings.signalPanel.enable_desc')}
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
            configKey="signal.enabled"
          />
          <SettingsInput
            label={i18nT('pages.settings.signalPanel.account')}
            description={i18nT('pages.settings.signalPanel.account_desc')}
            value={draft.account}
            onChange={v => upd({ account: v })}
            placeholder={i18nT('pages.settings.signalPanel.example_placeholder', { example: ACCOUNT_EXAMPLE })}
            disabled={ro}
            configKey="signal.account"
          />
          {!accountValid && <FieldWarning>{i18nT('pages.settings.signalPanel.account_invalid')}</FieldWarning>}
          {draft.enabled && !account && <FieldWarning>{i18nT('pages.settings.signalPanel.no_account_hint')}</FieldWarning>}
          <TagListEditor
            label={i18nT('pages.settings.signalPanel.allowed_numbers')}
            description={i18nT('pages.settings.signalPanel.allowed_numbers_desc')}
            values={draft.allowed_numbers}
            placeholder={i18nT('pages.settings.signalPanel.example_placeholder', { example: ALLOWED_EXAMPLE })}
            onChange={v => upd({ allowed_numbers: v })}
            validate={isSignalId}
            readOnly={ro}
          />
          <p className="text-[12px] text-muted m-0 mt-1" data-testid="signal-aci-hint">
            <CommandSentence
              i18nKey="pages.settings.signalPanel.aci_hint"
              command={signalListRecipientsCommand(account)}
            />
          </p>
          {allowListEmpty && draft.enabled && (
            <FieldWarning>{i18nT('pages.settings.signalPanel.empty_allowlist_hint')}</FieldWarning>
          )}
          {noOperator && (
            <div role="status" data-testid="signal-operator-warning"
              className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mt-2">
              <AlertTriangle size={13} className="text-warn flex-none mt-0.5" />
              <span className="text-[12px] text-text">
                {i18nT('pages.settings.signalPanel.no_operator_warning')}
              </span>
            </div>
          )}
          {allowListEmpty && !draft.enabled && (
            <p className="text-[12px] text-muted mt-2 mb-0 flex items-center gap-1.5">
              <Info size={12} className="flex-none" />
              {i18nT('pages.settings.signalPanel.note_to_self_hint')}
            </p>
          )}
          {/* Optional per-channel session filing. Off by default: Signal
              conversations stay unfiled in the sidebar. */}
          <div className="border-t border-border mt-4 pt-4">
            <SettingsToggle
              label={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder')}
              description={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder_desc', { channel: CHANNEL_NAME })}
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
                  placeholder={CHANNEL_NAME}
                  disabled={ro}
                />
              </div>
            )}
            {!!data.session_folder && (
              <ChannelFolderBackfill
                namespace="signal"
                folderName={data.session_folder}
                disabled={ro}
                testId="session-folder-backfill"
              />
            )}
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only sessions) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending || !accountValid}>
          {saveMut.isPending ? i18nT('pages.settings.signalPanel.saving') : i18nT('pages.settings.signalPanel.save')}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {restartHint ? i18nT('pages.settings.signalPanel.saved_restart') : i18nT('pages.settings.signalPanel.saved')}
          </span>
        )}
        {/* No hand-off: the unsaved `draft` (account, allowed_numbers, session
            folder) is exactly what a failed save did not store — the hand-off
            unmounts this panel and would lose it. */}
        <ErrorNotice message={error} variant="inline" />
      </div>}
    </>
  )
}
