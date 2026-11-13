import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Check, Copy, ExternalLink, Globe, KeyRound, LogIn, Pencil, Plus, ShieldCheck, StickyNote, Trash2 } from 'lucide-react'
import { useQuery, useMutation, useMutationState, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { SettingsSubNav, type SubNavItem } from '../../components/SettingsSubNav'
import { SUBNAV_PARAM, parsePathSegments, toPathSegment } from '../../components/subNavParams'
import ErrorNotice from '../../components/ErrorNotice'
import { SecretField } from '../../components/SecretField'
import { Btn, Card, CardTitle, IconButton, IconButtonGroup, Input } from '../../components/ui'
import {
  api,
  isAuthExpiredError,
  type ManagedSecret,
  type SecretItemBody,
  type SecretItemType,
  type SecretListItem,
  type SecretLoginPublicFields,
  type SecretsListResponse,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import { settingsPath } from '../../components/settingsPath'
import { connectionsOAuthClientEntryId } from '../../components/commandPalette/settingsManual'
import { whenPreviewFlagSaved } from '../../hooks/usePreviewFlagServerSync'
import { PREVIEW_SECRETS_VAULT } from '../../utils/previewFlags'
import { SECRETS_GUIDE_URL as SECRETS_SETUP_GUIDE } from '../../utils/docsLink'
import { copyToClipboard } from '../../utils/clipboard'
import { SECRETS_AUTHORIZE_COMMAND, SECRETS_SERVER_GRANT } from './secretsAuthorizeCommand'

const UNUSED_REASON_KEYS = {
  wakatime_disabled: 'settings.secrets.wakatime_unused_hint',
  jira_multi_host: 'settings.secrets.jira_global_unused_multi_host',
  jira_host_precedence: 'settings.secrets.jira_global_unused_host_token',
  gerrit_multi_host: 'settings.secrets.gerrit_global_unused_multi_host',
  gerrit_host_precedence: 'settings.secrets.gerrit_global_unused_host_credential',
} as const
/** The sub-nav key of the add pane. Every other row's key is a vault name, and
 *  `POST /api/secrets` refuses a name holding `#` (it separates the field in
 *  `secret://NAME#FIELD`), so a name saved here can never collide with it. */
const ADD_KEY = '#'
/** The settingsManual ids of managed slots, by the slot kind whose pane they open. */
const SEARCH_PANE_KINDS: Readonly<Record<string, string>> = {
  'secrets.wakatime-api-key': 'wakatime_api_key',
  'secrets.jira-api-token': 'jira_api_token',
}
/** Every vault write is keyed `[WRITE_KEY, name]`, so the panel can read which
 *  names have a request in flight from the mutation cache itself. The cache
 *  outlives the pane that started the request: a user can leave a managed pane
 *  while its delete is running, and an Add that targets that name must still
 *  wait, or the delayed request could reorder after the Add's save and erase
 *  the just-saved credential. A mount-bound flag would clear on that unmount. */
const WRITE_KEY = 'secrets-write'
const MASK = '••••••••'

/**
 * The three secrets requests go through `api/client.ts`, not a local `fetch`.
 *
 * That transport rejects a non-2xx, which is what keeps react-query's
 * `onSuccess` from firing on a 403 and clearing the form -- the data-loss
 * regression this file's own guard was written for. It also surfaces the
 * backend's error prose the same way, and adds the recovery a local guard could
 * never reach: `X-Session-Key`, one silent cookie refresh, the re-auth banner,
 * and the localized sign-in sentence in place of the gateway's cryptographic
 * reason (#12240).
 *
 * The panel's own `settings.secrets.*_error` sentences stay as the FRAME around
 * whatever message arrives. They name which action failed and prescribe nothing,
 * so an expired session reads "Could not save secret: Session expired. Run ..."
 * -- the recovery instruction is carried, not hidden.
 */

/**
 * Drop a stale AUTH failure card once auth works again.
 *
 * Keyed on `mc-auth-recovered`, NOT on `mc-auth-cleared`. The distinction is
 * the whole guard: `mc-auth-cleared` means only that the banner is gone, and
 * the banner's own X emits it while the session is still broken, so resetting
 * on it erases a live "could not save" card the reader has not acted on and
 * nothing has fixed. `mc-auth-recovered` is emitted from `removeAuthBanner`
 * alone, every caller of which is gated on a 2xx or an accepted token
 * exchange, so it is the only one of the two that actually means authentication
 * succeeded.
 *
 * `isAuthExpiredError` is the second half of the guard. The event says auth
 * recovered, NOT that every request since has become valid: a disk-full save
 * failure sitting unread on screen is still true after an auth recovery, so
 * resetting it would delete a message the user has not acted on. Only a
 * failure the event actually resolves is cleared.
 */
function useResetOnAuthRecovered(onRecovered: () => void): void {
  const latest = useRef(onRecovered)
  useEffect(() => {
    latest.current = onRecovered
  }, [onRecovered])
  useEffect(() => {
    const handle = () => latest.current()
    window.addEventListener('mc-auth-recovered', handle)
    return () => window.removeEventListener('mc-auth-recovered', handle)
  }, [])
}

/** Whether the calling component is still mounted, read at call time. A
 *  mutation's `onSuccess` outlives the pane that started it; one that then
 *  navigates must not move a user who has since opened, and may be editing,
 *  another pane. */
function useStillMounted(): () => boolean {
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  return useCallback(() => mounted.current, [])
}

function resetIfAuthExpired(mutation: { error: unknown; reset: () => void }): void {
  if (isAuthExpiredError(mutation.error)) mutation.reset()
}

function managedCopy(kind: string): { label: string } | null {
  if (kind === 'wakatime_api_key') return { label: i18nT('settings.secrets.wakatime_api_key_label') }
  if (kind === 'jira_host_token') return { label: i18nT('settings.secrets.jira_host_token_label') }
  if (kind === 'jira_api_token') return { label: i18nT('settings.secrets.jira_api_token_label') }
  if (kind === 'gerrit_http_credential') return { label: i18nT('settings.secrets.gerrit_http_credential_label') }
  return null
}

/** The WakaTime sign-in tokens entry, the one `wakatime_oauth` row the vault
 *  routes refuse (main's legacy panel draws it read-only the same way). */
function isWakatimeOAuthTokens(secret: ManagedSecret): boolean {
  return secret.kind === 'wakatime_oauth' && secret.name === 'WAKATIME_OAUTH_TOKENS'
}

/** Managed kinds whose row names one host, shown as "<label> — <host>". */
const HOST_LABELLED_KINDS: ReadonlySet<string> = new Set(['jira_host_token', 'gerrit_http_credential'])

function managedLabel(secret: ManagedSecret): string {
  const copy = managedCopy(secret.kind)
  return HOST_LABELLED_KINDS.has(secret.kind) && secret.host
    ? `${copy?.label ?? secret.name} — ${secret.host}`
    : copy?.label ?? secret.name
}

function typeLabel(type: SecretItemType): string {
  if (type === 'login') return i18nT('settings.secrets.type_login')
  if (type === 'note') return i18nT('settings.secrets.type_note')
  return i18nT('settings.secrets.type_secret')
}

function TypeIcon({ type, className }: { type: SecretItemType; className?: string }) {
  const Icon = type === 'login' ? LogIn : type === 'note' ? StickyNote : KeyRound
  return <Icon className={className ?? 'lucide-inline shrink-0 text-muted'} aria-hidden />
}

/** The factual second line of a rail row: a status dot and a short state. */
function StatusLine({ tone, text }: { tone: 'ok' | 'warn' | 'muted'; text: string }) {
  const color = tone === 'ok' ? 'var(--ok)' : tone === 'warn' ? 'var(--warn)' : 'var(--muted)'
  return (
    <span className="flex items-center gap-1.5 text-[11.5px]" style={{ color }}>
      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: color }} />
      {text}
    </span>
  )
}

function loginPublic(item: SecretListItem | undefined): SecretLoginPublicFields | null {
  const fields = item?.fields
  if (item?.type !== 'login' || !fields || !('username' in fields)) return null
  return fields as SecretLoginPublicFields
}


function DetailHeader({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle?: ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-subtle text-accent">{icon}</span>
      <div className="min-w-0">
        <h4 className="truncate font-mono text-[15px] font-semibold text-text-strong">{title}</h4>
        {subtitle && <div className="mt-0.5 text-[12px] text-muted">{subtitle}</div>}
      </div>
    </div>
  )
}


// ── Forms ──

interface LoginDraft {
  username: string
  password: string
  urls: string[]
  notes: string
  /** The website typed into the add field but not yet added. Never sent; it
   *  only counts toward the draft. */
  newUrl: string
}

const emptyLogin = (): LoginDraft => ({ username: '', password: '', urls: [], notes: '', newUrl: '' })

/** Whether *draft* holds anything the user typed since the form opened on *initial*. */
function loginChanged(draft: LoginDraft, initial: LoginDraft): boolean {
  return draft.username !== initial.username
    || draft.urls.join('\n') !== initial.urls.join('\n')
    || Boolean(draft.password || draft.notes || draft.newUrl.trim())
}

/** `keepHint` is set when editing a stored login: the password and notes
 *  are never sent to the page, so each starts blank and a
 *  blank one keeps its stored value (the server's `KEPT_WHEN_ABSENT`). */
function LoginFields({ draft, onChange, disabled, keepHint }: { draft: LoginDraft; onChange: (next: LoginDraft) => void; disabled: boolean; keepHint?: string }) {
  const newUrl = draft.newUrl
  const setNewUrl = (next: string) => onChange({ ...draft, newUrl: next })
  const addUrl = () => {
    const href = newUrl.trim()
    if (!href) return
    onChange({ ...draft, urls: [...draft.urls, href], newUrl: '' })
  }
  return (
    <fieldset disabled={disabled} className="space-y-3">
      <label className="block">
        <span className="mb-1 block text-[13px] font-medium text-muted">{i18nT('settings.secrets.username_label')}</span>
        <Input value={draft.username} onChange={e => onChange({ ...draft, username: e.target.value })} autoComplete="off" />
      </label>
      {/* One line for the three hidden fields below it; a placeholder is cut
          off in an input this narrow. */}
      {keepHint && <p className="text-[12.5px] text-muted">{keepHint}</p>}
      <label className="block">
        <span className="mb-1 block text-[13px] font-medium text-muted">{i18nT('settings.secrets.password_label')}</span>
        <Input type="password" value={draft.password} onChange={e => onChange({ ...draft, password: e.target.value })} className="font-mono" autoComplete="new-password" />
      </label>
      <div>
        <span className="mb-1 block text-[13px] font-medium text-muted">{i18nT('settings.secrets.websites_label')}</span>
        <ul className="space-y-2">
          {draft.urls.map((href, i) => (
            <li key={`${href}-${i}`} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-bg-elevated px-2 py-1.5">
              <Globe className="lucide-inline shrink-0 text-muted" aria-hidden />
              <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{href}</span>
              <IconButton variant="danger" aria-label={i18nT('settings.secrets.remove_website', { href })} onClick={() => onChange({ ...draft, urls: draft.urls.filter((_, j) => j !== i) })}>
                <Trash2 className="lucide-inline" aria-hidden />
              </IconButton>
            </li>
          ))}
        </ul>
        <div className="mt-2 flex gap-2">
          <Input
            value={newUrl}
            onChange={e => setNewUrl(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addUrl()
              }
            }}
            placeholder={i18nT('settings.secrets.url_placeholder')}
            aria-label={i18nT('settings.secrets.add_website')}
            className="font-mono"
          />
          <Btn onClick={addUrl} disabled={!newUrl.trim()}>
            <Plus className="lucide-inline mr-1" aria-hidden />
            {i18nT('settings.secrets.add_website')}
          </Btn>
        </div>
      </div>
      <label className="block">
        <span className="mb-1 block text-[13px] font-medium text-muted">{i18nT('settings.secrets.notes_label')}</span>
        <Input value={draft.notes} onChange={e => onChange({ ...draft, notes: e.target.value })} autoComplete="off" />
      </label>
    </fieldset>
  )
}

/** The typed write, shared by Add and Replace. Owns the draft and the mutation;
 *  the host decides what happens on success. */
function ItemForm({
  name,
  type,
  initialLogin,
  onSaved,
  onPendingChange,
  onCancel,
  onDraftChange,
  submitLabel,
  disabled = false,
  editing = false,
}: {
  name: string
  type: SecretItemType
  initialLogin?: Partial<LoginDraft>
  /** Editing a stored item of this type: a hidden field left blank is not
   *  sent, so the server keeps its stored value instead of erasing it. */
  editing?: boolean
  /** *stillMounted* answers whether this form is still on screen, so a host
   *  that navigates after a late save does not pull the user off another pane. */
  onSaved: (stillMounted: () => boolean) => void | Promise<void>
  /** Hears whether a save is in flight, including the host's own work after
   *  it (`onSaved` is awaited before the mutation settles). */
  onPendingChange?: (pending: boolean) => void
  onCancel?: () => void
  onDraftChange: (hasDraft: boolean) => void
  submitLabel: string
  /** The host is not ready for a write (no name yet, or the name has another
   *  request in flight). Typing stays possible; only Save waits. */
  disabled?: boolean
}) {
  const stillMounted = useStillMounted()
  const [value, setValue] = useState('')
  const [text, setText] = useState('')
  const [initial] = useState<LoginDraft>(() => ({ ...emptyLogin(), ...initialLogin }))
  const [login, setLogin] = useState<LoginDraft>(initial)
  // Every editable field counts, so the agent hand-off is withheld for any
  // unsaved edit (it navigates away and unmounts this form).
  const hasDraft = Boolean(value || text) || loginChanged(login, initial)
  useEffect(() => {
    onDraftChange(hasDraft)
  }, [hasDraft, onDraftChange])
  useEffect(() => () => onDraftChange(false), [onDraftChange])

  // Editing, a blank hidden field is left out of the body: absent keeps.
  const kept = (field: string): string | undefined => (editing && !field ? undefined : field)
  // A website typed but not yet added is saved too, as Add website would.
  const pendingUrl = login.newUrl.trim()
  const urls = pendingUrl ? [...login.urls, pendingUrl] : login.urls
  const body = (): SecretItemBody =>
    type === 'login'
      ? { type, fields: { username: login.username, password: kept(login.password), urls, notes: kept(login.notes) } }
      : type === 'note'
        ? { type, fields: { text: kept(text) } }
        : { type, fields: { value } }
  const complete = type === 'login' ? editing || Boolean(login.password) : type === 'note' ? editing || Boolean(text.trim()) : Boolean(value.trim())
  const keepHint = editing ? i18nT('settings.secrets.keep_blank_hint') : undefined

  const save = useMutation({
    mutationKey: [WRITE_KEY, name],
    mutationFn: () => api.secretsSave(name, body()),
    // Returned, so the mutation stays pending until the host's refetch and
    // navigation are done.
    onSuccess: () => onSaved(stillMounted),
  })
  useResetOnAuthRecovered(() => resetIfAuthExpired(save))
  const frozen = save.isPending
  useEffect(() => {
    onPendingChange?.(frozen)
  }, [frozen, onPendingChange])

  return (
    <div className="space-y-3">
      {type === 'login' && <LoginFields draft={login} onChange={next => { save.reset(); setLogin(next) }} disabled={frozen} keepHint={keepHint} />}
      {type === 'note' && (
        <label className="block">
          <span className="mb-1 block text-[13px] font-medium text-muted">{i18nT('settings.secrets.text_label')}</span>
          <textarea
            aria-label={i18nT('settings.secrets.text_label')}
            value={text}
            onChange={e => { save.reset(); setText(e.target.value) }}
            disabled={frozen}
            placeholder={keepHint}
            rows={6}
            className="w-full rounded-md border border-border bg-bg px-3 py-2 font-mono text-[13px] text-text"
          />
        </label>
      )}
      {type === 'secret' && (
        <label className="block">
          <span className="mb-1 block text-[13px] font-medium text-muted">{i18nT('settings.secrets.value_label')}</span>
          <Input
            type="password"
            value={value}
            onChange={e => { save.reset(); setValue(e.target.value) }}
            disabled={frozen}
            placeholder={i18nT('settings.secrets.value_placeholder')}
            className="font-mono"
            aria-label={i18nT('settings.secrets.secret_value_aria')}
            autoComplete="off"
          />
        </label>
      )}
      <div className="flex gap-2">
        <Btn primary onClick={() => save.mutate()} disabled={!complete || frozen || disabled}>
          {save.isPending ? i18nT('settings.secrets.saving') : submitLabel}
        </Btn>
        {onCancel && (
          <Btn onClick={onCancel} disabled={save.isPending}>
            {i18nT('settings.secrets.cancel')}
          </Btn>
        )}
      </div>
      {/* No hand-off: navigation would discard the unsaved value. */}
      {save.isError && (
        <ErrorNotice variant="inline" message={i18nT('settings.secrets.save_error', { error: (save.error as Error).message })} />
      )}
    </div>
  )
}

// ── Panes ──

function AddPane({
  managed,
  lockedNames,
  onSaved,
  onDraftChange,
}: {
  managed: ManagedSecret[]
  /** Names with a vault write in flight (see WRITE_KEY): an Add targeting one
   *  waits until it settles. */
  lockedNames: Set<string>
  onSaved: (name: string, stillMounted: () => boolean) => void | Promise<void>
  onDraftChange: (hasDraft: boolean) => void
}) {
  const [type, setType] = useState<SecretItemType>('secret')
  const [name, setName] = useState('')
  // The name is what a save in flight writes to and then opens: held while
  // it runs, so an edit made meanwhile is not dropped when it navigates.
  const [saving, setSaving] = useState(false)
  // A typed name alone is unsaved work too, so it joins the form's draft.
  const [formDraft, setFormDraft] = useState(false)
  const hasDraft = name !== '' || formDraft
  useEffect(() => {
    onDraftChange(hasDraft)
  }, [hasDraft, onDraftChange])
  useEffect(() => () => onDraftChange(false), [onDraftChange])
  const trimmed = name.trim()
  const folded = trimmed.toUpperCase()
  const managedTarget = managed.find(secret => secret.name.toUpperCase() === folded)
  const target = managedTarget?.name ?? trimmed
  const nameOk = Boolean(trimmed) && !trimmed.includes('#')
  const locked = !nameOk || lockedNames.has(target)
  const types: SecretItemType[] = ['secret', 'login', 'note']
  return (
    <div className="flex h-full flex-col gap-4">
      <DetailHeader icon={<Plus aria-hidden />} title={i18nT('settings.secrets.add_secret')} />
      <div role="radiogroup" aria-label={i18nT('settings.secrets.type_label')} className="inline-flex gap-1 self-start rounded-lg border border-border bg-bg-elevated p-1">
        {types.map(option => (
          <Btn
            key={option}
            role="radio"
            aria-checked={type === option}
            onClick={() => setType(option)}
            disabled={saving}
            className={`flex items-center gap-1.5 rounded-md border-transparent px-3 py-1.5 text-[13px] ${type === option ? 'bg-accent-subtle text-accent' : 'bg-transparent text-muted'}`}
          >
            <TypeIcon type={option} className="lucide-inline" />
            {typeLabel(option)}
          </Btn>
        ))}
      </div>
      <div>
        <label className="mb-1 block text-[13px] font-medium text-muted" htmlFor="secret-name-input">
          {i18nT('settings.secrets.name_label')}
        </label>
        <Input
          id="secret-name-input"
          value={name}
          onChange={e => setName(e.target.value)}
          disabled={saving}
          placeholder={i18nT('settings.secrets.name_placeholder')}
          className="font-mono"
          aria-label={i18nT('settings.secrets.secret_name_aria')}
          autoFocus
        />
        {trimmed.includes('#') && <p className="mt-1 text-[12px] text-warn">{i18nT('settings.secrets.name_no_hash')}</p>}
        {managedTarget && (
          <p className="mt-1 text-[12px] text-warn">{i18nT('settings.secrets.managed_name_hint', { name: managedTarget.name })}</p>
        )}
        {nameOk && <code className="mt-1 block text-[12px] text-muted">secret://{target}</code>}
      </div>
      <ItemForm
        key={type}
        name={target}
        type={type}
        disabled={locked}
        onSaved={stillMounted => onSaved(target, stillMounted)}
        onPendingChange={setSaving}
        onDraftChange={setFormDraft}
        submitLabel={i18nT('settings.secrets.save')}
      />
    </div>
  )
}

function ManagedPane({
  secret,
  configured,
  unusedReason,
  allowAgentHandoff,
  onDraftChange,
  lockedNames,
}: {
  secret: ManagedSecret
  configured: boolean
  unusedReason: keyof typeof UNUSED_REASON_KEYS | undefined
  allowAgentHandoff: boolean
  onDraftChange: (name: string, hasDraft: boolean) => void
  /** Names with a vault write in flight (see WRITE_KEY). This pane freezes
   *  while its own request OR an Add targeting this canonical name runs. */
  lockedNames: Set<string>
}) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [editing, setEditing] = useState(!configured)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const deleteButtonRef = useRef<HTMLButtonElement>(null)
  const deleteCancelRef = useRef<HTMLButtonElement>(null)
  const label = managedLabel(secret)

  useEffect(() => {
    if (deleteConfirm) deleteCancelRef.current?.focus()
  }, [deleteConfirm])
  const cancelDelete = () => {
    setDeleteConfirm(false)
    deleteMutation.reset()
    requestAnimationFrame(() => deleteButtonRef.current?.focus())
  }

  useEffect(() => {
    onDraftChange(secret.name, Boolean(value))
  }, [onDraftChange, secret.name, value])
  useEffect(() => () => onDraftChange(secret.name, false), [onDraftChange, secret.name])

  const finish = async () => {
    setValue('')
    setDeleteConfirm(false)
    await queryClient.invalidateQueries({ queryKey: ['secrets'] })
    setEditing(false)
  }
  const setMutation = useMutation({
    mutationKey: [WRITE_KEY, secret.name],
    mutationFn: () => api.secretsSave(secret.name, value),
    onSuccess: finish,
  })
  const deleteMutation = useMutation({
    mutationKey: [WRITE_KEY, secret.name],
    mutationFn: () => api.secretsDelete(secret.name),
    onMutate: () => setMutation.reset(),
    onSuccess: finish,
  })
  const isPending = setMutation.isPending || deleteMutation.isPending
  useResetOnAuthRecovered(() => {
    resetIfAuthExpired(setMutation)
    resetIfAuthExpired(deleteMutation)
  })
  const locked = isPending || lockedNames.has(secret.name)

  return (
    <div className="flex h-full flex-col gap-4">
      <DetailHeader icon={<ShieldCheck aria-hidden />} title={secret.name} subtitle={label} />
      {unusedReason && <p className="text-[12px] text-warn">{i18nT(UNUSED_REASON_KEYS[unusedReason])}</p>}
      <fieldset disabled={locked}>
        {/* The slot's own label, as in the default panel: it is the anchor
            the manual registry entries (secrets.wakatime-api-key, ...) highlight. */}
        <SecretField
          label={label}
          isSet={configured}
          preview={MASK}
          placeholder={i18nT('settings.secrets.managed_value_placeholder')}
          setupLink={secret.kind === 'wakatime_api_key' ? { href: 'https://wakatime.com/settings/api-key', label: i18nT('settings.secrets.wakatime_setup_link') } : undefined}
          value={value}
          onChange={next => {
            setMutation.reset()
            setDeleteConfirm(false)
            setValue(next)
          }}
          editing={editing}
          onEditingChange={setEditing}
          cleared={false}
          onClearedChange={next => { if (next) setDeleteConfirm(true) }}
          permanentRemoval={{
            label: i18nT('settings.secrets.delete_managed_name', { name: label }),
            text: i18nT('settings.secrets.delete'),
            buttonRef: deleteButtonRef,
            confirmation: deleteConfirm ? (
              <>
                <span className="text-[13px] text-warn">{i18nT('settings.secrets.delete_managed_confirm', { name: label })}</span>
                <Btn danger onClick={() => deleteMutation.mutate()} disabled={locked}>
                  {i18nT('settings.secrets.delete')}
                </Btn>
                <Btn ref={deleteCancelRef} disabled={locked} onClick={cancelDelete}>
                  {i18nT('settings.secrets.cancel')}
                </Btn>
              </>
            ) : undefined,
          }}
          replaceLabel={i18nT('settings.secrets.replace')}
        />
      </fieldset>
      {(!configured || editing) && (
        <Btn primary aria-label={i18nT('settings.secrets.save_managed_name', { name: secret.name })} onClick={() => setMutation.mutate()} disabled={!value || locked} className="self-start">
          {isPending ? i18nT('settings.secrets.saving') : i18nT('settings.secrets.save')}
        </Btn>
      )}
      {setMutation.isSuccess && (
        <div role="status" className="flex items-center gap-1 text-[13px] text-ok">
          <Check className="lucide-inline" aria-hidden />
          {i18nT('settings.secrets.saved')}
        </div>
      )}
      {/* No hand-off while this editor holds an unsaved value. */}
      {setMutation.isError && (
        <ErrorNotice variant="inline" askAgent={allowAgentHandoff && !value} message={i18nT('settings.secrets.save_error', { error: (setMutation.error as Error).message })} />
      )}
      {deleteMutation.isError && (
        <ErrorNotice variant="inline" askAgent={allowAgentHandoff && !value} message={i18nT('settings.secrets.delete_error', { error: (deleteMutation.error as Error).message })} />
      )}
    </div>
  )
}

function ConnectionsPane({ secret }: { secret: ManagedSecret }) {
  return (
    <div className="flex h-full flex-col gap-4">
      <DetailHeader icon={<ShieldCheck aria-hidden />} title={secret.name} />
      <Link
        to={settingsPath({ tab: 'connections', highlight: secret.host ? connectionsOAuthClientEntryId(secret.host) : undefined })}
        className="self-start text-[13px] text-accent hover:underline"
      >
        {i18nT('settings.secrets.connections_client_secret_manage')}
      </Link>
    </div>
  )
}

/** `WAKATIME_OAUTH_TOKENS`: written by the WakaTime sign-in flow and refused by
 *  the generic vault routes (409), so it is shown read-only, with no value, no
 *  reference and no Delete, and managed from the WakaTime panel. */
function WakatimeOAuthTokensPane({ secret }: { secret: ManagedSecret }) {
  return (
    <div className="flex h-full flex-col gap-4">
      <DetailHeader icon={<ShieldCheck aria-hidden />} title={secret.name} />
      <p className="text-[13px] text-muted">{i18nT('settings.secrets.wakatime_oauth_tokens_description')}</p>
      <Link
        to={settingsPath({ tab: 'overview', params: { view: 'wakatime' } })}
        className="self-start text-[13px] text-accent hover:underline"
      >
        {i18nT('settings.secrets.wakatime_oauth_tokens_manage')}
      </Link>
    </div>
  )
}

/** One row of a field card: label column, value column, actions that appear
 *  on hover or keyboard focus. The label is optional so a card can continue a
 *  group (the second and third reference rows) without repeating it. */
function FieldRow({ label, children, actions, mono = false }: { label?: string; children: ReactNode; actions?: ReactNode; mono?: boolean }) {
  return (
    <div className="group grid grid-cols-1 items-center gap-x-4 gap-y-1 border-t border-border py-2.5 first:border-t-0 first:pt-0 last:pb-0 sm:grid-cols-[140px_minmax(0,1fr)]">
      <div className="text-[13px] text-muted">{label}</div>
      <div className={`flex min-w-0 flex-wrap items-center gap-2.5 text-[13.5px] text-text-strong sm:flex-nowrap ${mono ? 'font-mono' : ''}`}>
        {children}
        {/* IconButtonGroup's reveal: hidden until hover or keyboard focus, always shown on touch. */}
        {actions && <IconButtonGroup reveal className="ml-auto shrink-0">{actions}</IconButtonGroup>}
      </div>
    </div>
  )
}

/** A field group. `Card` carries the project's border, glow, radius and rise;
 *  the `mb-0` hands the gap back to the pane's own column. */
function FieldCard({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <Card className="mb-0 py-3.5">
      {title && <CardTitle>{title}</CardTitle>}
      {children}
    </Card>
  )
}

/** Copy *text* to the clipboard; the icon flips to a check for a moment.
 *  *onResult* hears whether the copy landed, so the pane can say when not. */
function CopyAction({ text, label, onResult }: { text: string; label: string; onResult: (ok: boolean) => void }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const t = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(t)
  }, [copied])
  return (
    <IconButton
      aria-label={label}
      onClick={() => {
        void copyToClipboard(text).then(ok => {
          setCopied(ok)
          onResult(ok)
        })
      }}
    >
      {copied ? <Check className="lucide-inline text-ok" aria-hidden /> : <Copy className="lucide-inline" aria-hidden />}
    </IconButton>
  )
}

function ItemPane({
  item,
  unusedReason,
  allowAgentHandoff,
  onDraftChange,
  onDeleted,
  lockedNames,
}: {
  item: SecretListItem
  unusedReason: keyof typeof UNUSED_REASON_KEYS | undefined
  allowAgentHandoff: boolean
  onDraftChange: (name: string, hasDraft: boolean) => void
  onDeleted: () => void
  /** Names with a vault write in flight (see WRITE_KEY): no edit or delete of
   *  this item starts until its own pending request settles. */
  lockedNames: Set<string>
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const stillMounted = useStillMounted()
  const [copyFailed, setCopyFailed] = useState(false)
  const onCopied = useCallback((ok: boolean) => setCopyFailed(!ok), [])
  const locked = lockedNames.has(item.name)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const deleteButtonRef = useRef<HTMLButtonElement>(null)
  const deleteCancelRef = useRef<HTMLButtonElement>(null)
  const pub = loginPublic(item)
  const draftChange = useCallback((has: boolean) => onDraftChange(item.name, has), [onDraftChange, item.name])
  const deleteMutation = useMutation({
    mutationKey: [WRITE_KEY, item.name],
    mutationFn: () => api.secretsDelete(item.name),
    onSuccess: async () => {
      // Read before the refetch: the refetch itself unmounts this pane (its
      // item is gone). Not mounted here means the user opened another pane
      // while the delete ran; moving them to Add would drop what they edit.
      const stillHere = stillMounted()
      await queryClient.invalidateQueries({ queryKey: ['secrets'] })
      if (stillHere) onDeleted()
    },
  })
  useResetOnAuthRecovered(() => resetIfAuthExpired(deleteMutation))
  useEffect(() => {
    if (deleteConfirm) deleteCancelRef.current?.focus()
  }, [deleteConfirm])
  const cancelDelete = () => {
    setDeleteConfirm(false)
    deleteMutation.reset()
    requestAnimationFrame(() => deleteButtonRef.current?.focus())
  }

  const references = item.type === 'login'
    ? [`secret://${item.name}`, `secret://${item.name}#username`]
    : [`secret://${item.name}`]
  const secretLabel = item.type === 'note'
    ? i18nT('settings.secrets.text_label')
    : item.type === 'login'
      ? i18nT('settings.secrets.password_label')
      : i18nT('settings.secrets.value_label')

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-start gap-3">
        <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-accent-subtle font-mono text-xl font-bold text-accent" aria-hidden>
          {item.name.slice(0, 1).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <h4 className="truncate font-mono text-[17px] font-semibold text-text-strong">{item.name}</h4>
          <div className="mt-0.5 text-[12.5px] text-muted">{typeLabel(item.type)}</div>
        </div>
        {/* Two controls, per the row budget: Edit and Delete. */}
        {!editing && !item.corrupt && (
          <Btn onClick={() => setEditing(true)} disabled={locked || deleteConfirm}>
            <Pencil className="lucide-inline mr-1" aria-hidden />
            {i18nT('settings.secrets.edit')}
          </Btn>
        )}
        {!editing && (
          <IconButton
            ref={deleteButtonRef}
            variant="danger"
            aria-label={i18nT('settings.secrets.delete_secret_name', { name: item.name })}
            onClick={() => setDeleteConfirm(true)}
            disabled={locked}
          >
            <Trash2 className="lucide-inline" aria-hidden />
          </IconButton>
        )}
      </div>
      {unusedReason && <p className="text-[12px] text-warn">{i18nT(UNUSED_REASON_KEYS[unusedReason])}</p>}

      {deleteConfirm && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-danger-subtle bg-danger-subtle px-3 py-2">
          <span className="text-[13px] text-danger">{i18nT('settings.secrets.delete_confirm', { name: item.name })}</span>
          <Btn danger onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>
            {i18nT('settings.secrets.delete')}
          </Btn>
          <Btn ref={deleteCancelRef} onClick={cancelDelete} disabled={deleteMutation.isPending}>
            {i18nT('settings.secrets.cancel')}
          </Btn>
        </div>
      )}
      {/* No hand-off while the confirmation is open: leaving the pane drops it. */}
      {deleteMutation.isError && (
        <ErrorNotice variant="inline" askAgent={allowAgentHandoff && !deleteConfirm} message={i18nT('settings.secrets.delete_error', { error: (deleteMutation.error as Error).message })} />
      )}
      {/* Same hand-off rule as the delete error: withheld while any draft is
          open (allowAgentHandoff) or the confirmation is, which leaving drops. */}
      {item.corrupt && <ErrorNotice variant="inline" askAgent={allowAgentHandoff && !deleteConfirm} message={i18nT('settings.secrets.corrupt_item')} />}

      {editing ? (
        <FieldCard>
          <ItemForm
              name={item.name}
              type={item.type}
              initialLogin={pub ? { username: pub.username, urls: pub.urls } : undefined}
              editing
              disabled={locked}
              onSaved={async () => {
                await queryClient.invalidateQueries({ queryKey: ['secrets'] })
                setEditing(false)
              }}
              onCancel={() => setEditing(false)}
              onDraftChange={draftChange}
              submitLabel={i18nT('settings.secrets.save')}
            />
        </FieldCard>
      ) : !item.corrupt && (
        <FieldCard>
          {pub && (
            <FieldRow label={i18nT('settings.secrets.username_label')} mono actions={pub.username ? <CopyAction text={pub.username} label={i18nT('settings.secrets.copy_reference', { reference: pub.username })} onResult={onCopied} /> : undefined}>
              <span className="truncate">{pub.username || <span className="text-muted">—</span>}</span>
            </FieldRow>
          )}
          <FieldRow label={secretLabel} mono>
            <span className="tracking-[3px]">{MASK}</span>
          </FieldRow>
          {pub && (
            <>
              {pub.urls.length === 0 ? (
                <FieldRow label={i18nT('settings.secrets.websites_label')}>
                  <span className="text-muted">{i18nT('settings.secrets.websites_none')}</span>
                </FieldRow>
              ) : (
                pub.urls.map((href, i) => (
                  <FieldRow
                    key={href}
                    label={i === 0 ? i18nT('settings.secrets.websites_label') : undefined}
                    mono
                    actions={
                      <IconButton aria-label={i18nT('settings.secrets.open_website', { href })} onClick={() => window.open(href, '_blank', 'noopener,noreferrer')}>
                        <ExternalLink className="lucide-inline" aria-hidden />
                      </IconButton>
                    }
                  >
                    <Globe className="lucide-inline shrink-0 text-muted" aria-hidden />
                    <span className="truncate text-[13px]">{href}</span>
                  </FieldRow>
                ))
              )}
            </>
          )}
        </FieldCard>
      )}

      <FieldCard title={i18nT('settings.secrets.reference_title')}>
        <p className="pb-2 text-[13px] text-muted">{i18nT('settings.secrets.reference_hint')}</p>
        {references.map(reference => (
          <div key={reference} className="group flex items-center gap-2.5 border-t border-border py-2 first:border-t-0 first:pt-0 last:pb-0">
            <code className="truncate font-mono text-[13px] text-text-strong">{reference}</code>
            <IconButtonGroup reveal className="ml-auto shrink-0">
              <CopyAction text={reference} label={i18nT('settings.secrets.copy_reference', { reference })} onResult={onCopied} />
            </IconButtonGroup>
          </div>
        ))}
        {/* Hand-off withheld only while a draft is open (allowAgentHandoff). */}
        {copyFailed && <ErrorNotice variant="inline" askAgent={allowAgentHandoff} message={i18nT('settings.secrets.copy_failed')} />}
        <p className="border-t border-border pt-2 text-[13px] text-muted">
          {i18nT('settings.secrets.custom_authorize', { server: SECRETS_SERVER_GRANT })}
          <code className="block mt-1.5 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 font-mono text-[12px] text-accent overflow-x-auto">
            {SECRETS_AUTHORIZE_COMMAND}
          </code>
        </p>
      </FieldCard>

      <div className="mt-auto flex items-center justify-between gap-4 pt-2 text-[12px] text-muted">
        <span>{item.updated_at ? i18nT('settings.secrets.updated_at', { when: fmtDateTime(item.updated_at) }) : ''}</span>
        <a href={SECRETS_SETUP_GUIDE} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
          {i18nT('settings.secrets.how_it_works')}
        </a>
      </div>
    </div>
  )
}

// ── Panel ──

/** Select a sub-nav row from outside the rail, in whichever URL model the host
 *  runs: the second path segment under `basePath`, or `?sub=`. Mirrors
 *  SettingsSubNav's own write so the row it highlights and the pane it mounts
 *  agree with a rail click. Replace, not push: this follows a save, which is
 *  not a place the Back button should return to. */
function useSubNavSelect(basePath: string | undefined): (key: string) => void {
  const navigate = useNavigate()
  const location = useLocation()
  return useCallback(
    (key: string) => {
      if (basePath) {
        const segments = parsePathSegments(basePath, location.pathname)
        const tab = segments[0] ?? 'secrets'
        const seg = toPathSegment(key)
        if (seg === null) return
        // The entry's state carries SettingsSubNav's push marker on a phone,
        // which its Back reads to pop instead of replacing.
        navigate({ pathname: `${basePath}/${tab}/${seg}`, search: location.search }, { replace: true, state: location.state })
        return
      }
      const search = new URLSearchParams(location.search)
      search.set(SUBNAV_PARAM, key)
      navigate({ pathname: location.pathname, search: `?${search.toString()}` }, { replace: true, state: location.state })
    },
    [basePath, location.pathname, location.search, location.state, navigate],
  )
}

/** Settings → Secrets: a list-detail vault. The rail groups what the product
 *  consumes by name ("Used automatically by Kiro Crew") from the user's own
 *  entries; the pane shows one item, its `secret://` references and the
 *  actions on it. On a phone the rail is the view and a row pushes its pane
 *  (SettingsSubNav). `basePath` is the Settings host's path-navigation seam. */
export function SecretsPanel({ basePath }: { basePath?: string } = {}) {
  const queryClient = useQueryClient()
  const [managedDraftNames, setManagedDraftNames] = useState<Set<string>>(() => new Set())
  const [addDraft, setAddDraft] = useState(false)
  const select = useSubNavSelect(basePath)
  const pendingWrites = useMutationState({
    filters: { mutationKey: [WRITE_KEY], status: 'pending' },
    select: mutation => String(mutation.options.mutationKey?.[1] ?? ''),
  })
  const lockedNames = useMemo(() => new Set(pendingWrites), [pendingWrites])

  // Its own key under ['secrets'] (every invalidation still reaches it): with the
  // preview off the gateway lists names only, so the legacy panel's cached
  // listing must not stand in for this one. The list is read only after the
  // switch's write has reached the gateway: read before it, the gateway still
  // answers names only.
  const { data, isLoading, isError, error: listError } = useQuery<SecretsListResponse>({
    queryKey: ['secrets', 'items'],
    queryFn: async () => {
      await whenPreviewFlagSaved(PREVIEW_SECRETS_VAULT)
      return api.secretsList()
    },
  })

  const names = useMemo(() => data?.names ?? [], [data?.names])
  const managed = useMemo(() => data?.managed ?? [], [data?.managed])
  const stored = useMemo(() => new Set(names), [names])
  const itemsByName = useMemo(() => {
    const map = new Map<string, SecretListItem>()
    for (const item of data?.items ?? []) map.set(item.name, item)
    for (const name of names) if (!map.has(name)) map.set(name, { name, type: 'secret', revision: '' })
    return map
  }, [data?.items, names])
  const managedNames = useMemo(() => new Set(managed.map(secret => secret.name)), [managed])
  const otherNames = names.filter(name => !managedNames.has(name))

  // A settings-search result for a managed slot arrives as
  // `/settings/secrets?highlight=<id>` with no pane selected: open that slot's
  // pane, so the highlight finds its field. Only when nothing is selected, so
  // it never moves a user off a pane they chose.
  const location = useLocation()
  useEffect(() => {
    const query = new URLSearchParams(location.search)
    const kind = SEARCH_PANE_KINDS[query.get('highlight') ?? '']
    const target = kind ? managed.find(secret => secret.kind === kind) : undefined
    if (!target) return
    const current = basePath
      ? parsePathSegments(basePath, location.pathname)[1]
      : query.get(SUBNAV_PARAM)
    if (!current) select(target.name)
  }, [basePath, location.pathname, location.search, managed, select])
  const unusedReasons = useMemo(() => new Map((data?.unused ?? []).map(entry => [entry.name, entry.reason])), [data?.unused])

  const handleManagedDraftChange = useCallback((name: string, hasDraft: boolean) => {
    setManagedDraftNames(current => {
      if (current.has(name) === hasDraft) return current
      const next = new Set(current)
      if (hasDraft) next.add(name)
      else next.delete(name)
      return next
    })
  }, [])
  const handleAddDraft = useCallback((hasDraft: boolean) => setAddDraft(hasDraft), [])
  const hasAnyDraft = addDraft || managedDraftNames.size > 0

  const rows: SubNavItem<string>[] = [
    {
      key: ADD_KEY,
      label: i18nT('settings.secrets.add_secret'),
      icon: <Plus className="lucide-inline text-accent" aria-hidden />,
    },
    ...managed.map(secret => {
      const configured = stored.has(secret.name)
      const reason = unusedReasons.get(secret.name)
      return {
        key: secret.name,
        label: secret.name,
        labelOpaque: true,
        icon: <ShieldCheck className="lucide-inline text-muted" aria-hidden />,
        group: i18nT('settings.secrets.managed_title'),
        summary: reason ? (
          <StatusLine tone="warn" text={i18nT('settings.secrets.status_unused')} />
        ) : configured ? (
          <StatusLine tone="ok" text={i18nT('settings.secrets.status_set')} />
        ) : (
          <StatusLine tone="muted" text={i18nT('settings.secrets.status_not_set')} />
        ),
      }
    }),
    ...otherNames.map(name => {
      const item = itemsByName.get(name)!
      const pub = loginPublic(item)
      return {
        key: name,
        label: name,
        labelOpaque: true,
        icon: <TypeIcon type={item.type} />,
        group: i18nT('settings.secrets.custom_title'),
        // The backend's catalog omits a stored name it reports as unused
        // (WakaTime disabled, a global token beside per-host ones), so the
        // warning has to be read here too. Unreadable outranks unused.
        summary: item.corrupt
          ? <StatusLine tone="warn" text={i18nT('settings.secrets.status_unreadable')} />
          : unusedReasons.has(name)
            ? <StatusLine tone="warn" text={i18nT('settings.secrets.status_unused')} />
            : pub?.username
              ? <span className="text-[11.5px] text-muted">{pub.username}</span>
              : undefined,
      }
    }),
  ]

  // Only failures live above the list; the layout carries the rest.
  const banner = isError || data?.managed_error ? (
    <div className="space-y-3 pb-3">
      {/* Keep the Add path available; only agent navigation is unsafe with a draft. */}
      {isError && <ErrorNotice askAgent={!hasAnyDraft} message={i18nT('settings.secrets.load_error', { error: (listError as Error).message })} />}
      {data?.managed_error && <ErrorNotice variant="inline" askAgent={!hasAnyDraft} message={i18nT('settings.secrets.managed_config_error')} />}
    </div>
  ) : undefined

  if (isLoading) {
    return <p className="text-sm text-muted">{i18nT('settings.secrets.loading')}</p>
  }

  return (
    <SettingsSubNav
      items={rows}
      railWidth={272}
      listLabel={i18nT('settings.tabs.secrets.label')}
      backLabel={i18nT('settings.tabs.secrets.label')}
      basePath={basePath}
      banner={banner}
      guideTargetPrefix="settings.sub.secrets."
    >
      {active => {
        if (active === null) return null
        if (active === ADD_KEY) {
          return (
            <AddPane
              managed={managed}
              lockedNames={lockedNames}
              onDraftChange={handleAddDraft}
              onSaved={async (name, stillMounted) => {
                await queryClient.invalidateQueries({ queryKey: ['secrets'] })
                // The user may have opened another pane while the save ran.
                if (stillMounted()) select(name)
              }}
            />
          )
        }
        const managedSecret = managed.find(secret => secret.name === active)
        if (managedSecret?.kind === 'connections_client_secret') return <ConnectionsPane key={active} secret={managedSecret} />
        if (managedSecret && isWakatimeOAuthTokens(managedSecret)) return <WakatimeOAuthTokensPane key={active} secret={managedSecret} />
        if (managedSecret) {
          return (
            <ManagedPane
              key={active}
              secret={managedSecret}
              configured={stored.has(active)}
              unusedReason={unusedReasons.get(active) as keyof typeof UNUSED_REASON_KEYS | undefined}
              allowAgentHandoff={!hasAnyDraft}
              onDraftChange={handleManagedDraftChange}
              lockedNames={lockedNames}
            />
          )
        }
        const item = itemsByName.get(active)
        if (!item) {
          // A deleted or never-stored name in the URL: say so instead of a blank pane.
          return <p className="text-sm italic text-muted">{i18nT('settings.secrets.not_found')}</p>
        }
        return (
          <ItemPane
            key={active}
            item={item}
            unusedReason={unusedReasons.get(active) as keyof typeof UNUSED_REASON_KEYS | undefined}
            allowAgentHandoff={!hasAnyDraft}
            onDraftChange={handleManagedDraftChange}
            onDeleted={() => select(ADD_KEY)}
            lockedNames={lockedNames}
          />
        )
      }}
    </SettingsSubNav>
  )
}
