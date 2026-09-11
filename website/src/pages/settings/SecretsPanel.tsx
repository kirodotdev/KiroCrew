import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, KeyRound, Plus, Trash2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { SettingsSection, SettingsCard } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { SecretField } from '../../components/SecretField'

const SECRETS_SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/secrets-env.md'
const UNUSED_REASON_KEYS = {
  wakatime_disabled: 'settings.secrets.wakatime_unused_hint',
  jira_multi_host: 'settings.secrets.jira_global_unused_multi_host',
  jira_host_precedence: 'settings.secrets.jira_global_unused_host_token',
} as const
import { Btn, IconButton, Input, PanelSectionHeader } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/**
 * Parse a JSON response, REJECTING on a non-2xx status.
 *
 * A bare `r.json()` resolves for an error response too, so react-query treated a
 * 403/500 as a successful mutation: `onSuccess` fired and cleared the form,
 * silently discarding the secret the user had typed without ever storing it.
 * Throwing routes those statuses to `onError` instead, which leaves the form
 * populated so the value is not lost. This is a local `!r.ok` guard rather than
 * the shared `api/client.ts` transport because SecretsPanel authenticates with
 * the raw stored token, not the transport's `dashboard:ui` session key.
 */
const j = async (r: Response) => {
  if (!r.ok) {
    // Surface the backend's error prose when it sent any, so the failure is
    // actionable rather than a bare status code.
    let detail = ''
    try {
      const body = await r.json()
      if (body && typeof body.error === 'string') detail = `: ${body.error}`
    } catch {
      // Non-JSON error body — the status alone is what we have.
    }
    throw new Error(`HTTP ${r.status}${detail}`)
  }
  return r.json()
}
// Send the same fixed `dashboard:ui` session key the shared transport uses
// (`src/api/client.ts`). This panel previously read `localStorage['kiro_crew_token']`,
// but nothing in the app ever writes that key — the browser's dashboard identity
// is the `dashboard:ui` literal, and the backend treats a missing/empty
// X-Session-Key as `dashboard:ui` anyway — so the read was vestigial dead code
// that always resolved to ''. Use the literal directly so the header is explicit
// and matches every other panel.
const _sk = { 'X-Session-Key': 'dashboard:ui' }
const get = (url: string) => fetch(url, { headers: { ..._sk } })
const post = (url: string, body?: object) =>
  fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(body) })
const del = (url: string) =>
  fetch(url, { method: 'DELETE', headers: { ..._sk } })

interface ManagedSecret {
  name: string
  kind: string
  host?: string
}

interface SecretsListResponse {
  names: string[]
  managed: ManagedSecret[]
  unused?: Array<{ name: string; reason: 'wakatime_disabled' | 'jira_multi_host' | 'jira_host_precedence' }>
  managed_error?: boolean
}

function managedCopy(kind: string) {
  if (kind === 'wakatime_api_key') {
    return {
      label: i18nT('settings.secrets.wakatime_api_key_label'),
      description: i18nT('settings.secrets.wakatime_api_key_description'),
    }
  }
  if (kind === 'jira_host_token') {
    return {
      label: i18nT('settings.secrets.jira_host_token_label'),
      description: i18nT('settings.secrets.jira_host_token_description'),
    }
  }
  if (kind === 'jira_api_token') {
    return {
      label: i18nT('settings.secrets.jira_api_token_label'),
      description: i18nT('settings.secrets.jira_api_token_description'),
    }
  }
  return null
}

function ManagedSecretRow({
  secret,
  configured,
  allowAgentHandoff,
  onDraftChange,
  onPendingChange,
  frozenByAdd,
}: {
  secret: ManagedSecret
  configured: boolean
  allowAgentHandoff: boolean
  onDraftChange: (name: string, hasDraft: boolean) => void
  onPendingChange: (name: string, isPending: boolean) => void
  frozenByAdd: boolean
}) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const deleteButtonRef = useRef<HTMLButtonElement>(null)
  const deleteCancelRef = useRef<HTMLButtonElement>(null)
  const [editing, setEditing] = useState(!configured)
  const copy = managedCopy(secret.kind)
  const label = secret.kind === 'jira_host_token' && secret.host
    ? `${copy?.label ?? secret.name} — ${secret.host}`
    : copy?.label ?? secret.name

  useEffect(() => {
    if (deleteConfirm) deleteCancelRef.current?.focus()
  }, [deleteConfirm])

  useEffect(() => {
    onDraftChange(secret.name, Boolean(value))
  }, [onDraftChange, secret.name, value])

  useEffect(() => () => {
    onDraftChange(secret.name, false)
  }, [onDraftChange, secret.name])

  const finish = async () => {
    setValue('')
    setDeleteConfirm(false)
    await queryClient.invalidateQueries({ queryKey: ['secrets'] })
    setEditing(false)
  }

  const setMutation = useMutation({
    mutationFn: () => post('/api/secrets', { name: secret.name, value }).then(j),
    onSuccess: finish,
  })
  const deleteMutation = useMutation({
    mutationFn: () => del(`/api/secrets/${encodeURIComponent(secret.name)}`).then(j),
    onMutate: () => setMutation.reset(),
    onSuccess: finish,
  })
  const isPending = setMutation.isPending || deleteMutation.isPending
  // Freeze the row while its own mutation is in flight OR the parent Add is
  // saving this same canonical name — a concurrent row delete/replace during
  // the Add POST could otherwise reorder around it and corrupt the credential.
  const locked = isPending || frozenByAdd
  useEffect(() => {
    onPendingChange(secret.name, isPending)
  }, [onPendingChange, secret.name, isPending])
  useEffect(() => () => {
    onPendingChange(secret.name, false)
  }, [onPendingChange, secret.name])
  const handleValueChange = (next: string) => {
    setMutation.reset()
    setDeleteConfirm(false)
    setValue(next)
  }
  const cancelDelete = () => {
    setDeleteConfirm(false)
    requestAnimationFrame(() => deleteButtonRef.current?.focus())
  }

  return (
    <div className="rounded bg-bg-elevated p-2">
      <fieldset disabled={locked}>
        <SecretField
          label={label}
          description={copy?.description}
          isSet={configured}
          preview="••••••••"
          placeholder={i18nT('settings.secrets.managed_value_placeholder')}
          setupLink={secret.kind === 'wakatime_api_key' ? {
            href: 'https://wakatime.com/settings/api-key',
            label: i18nT('settings.secrets.wakatime_setup_link'),
          } : undefined}
          value={value}
          onChange={handleValueChange}
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
                <span className="text-[13px] text-warn">
                  {i18nT('settings.secrets.delete_managed_confirm', { name: label })}
                </span>
                <Btn danger onClick={() => deleteMutation.mutate()} disabled={locked}>
                  {i18nT('settings.secrets.delete')}
                </Btn>
                <Btn ref={deleteCancelRef} disabled={locked} onClick={cancelDelete}>
                  {i18nT('settings.secrets.cancel')}
                </Btn>
              </>
            ) : undefined,
          }}
        />
      </fieldset>
      {copy && (
        <code className="mt-1 block truncate text-[12px] text-muted">{secret.name}</code>
      )}
      {(!configured || editing) && (
        <Btn
          primary
          aria-label={i18nT('settings.secrets.save_managed_name', { name: secret.name })}
          onClick={() => setMutation.mutate()}
          disabled={!value || locked}
          className="mt-2"
        >
          {isPending ? i18nT('settings.secrets.saving') : i18nT('settings.secrets.save')}
        </Btn>
      )}
      {setMutation.isSuccess && (
        <div role="status" className="mt-2 flex items-center gap-1 text-[13px] text-ok">
          <Check className="lucide-inline" aria-hidden />
          {i18nT('settings.secrets.saved')}
        </div>
      )}
      {/* No hand-off while this or any sibling editor holds an unsaved value. */}
      {setMutation.isError && (
        <ErrorNotice
          variant="inline"
          askAgent={allowAgentHandoff && !value}
          message={i18nT('settings.secrets.save_error', { error: (setMutation.error as Error).message })}
        />
      )}
      {deleteMutation.isError && (
        <ErrorNotice
          variant="inline"
          askAgent={allowAgentHandoff && !value}
          message={i18nT('settings.secrets.delete_error', { error: (deleteMutation.error as Error).message })}
        />
      )}
    </div>
  )
}

export function SecretsPanel() {
  const queryClient = useQueryClient()
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [newValue, setNewValue] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)
  const [managedDraftNames, setManagedDraftNames] = useState<Set<string>>(() => new Set())
  const [managedPendingNames, setManagedPendingNames] = useState<Set<string>>(() => new Set())

  const { data, isLoading, isError, error: listError } = useQuery<SecretsListResponse>({
    queryKey: ['secrets'],
    queryFn: () => get('/api/secrets').then(j),
  })

  const setMutation = useMutation({
    mutationFn: (params: { name: string; value: string }) =>
      post('/api/secrets', params).then(j),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['secrets'] })
      setShowAdd(false)
      setNewName('')
      setNewValue('')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (name: string) => del(`/api/secrets/${encodeURIComponent(name)}`).then(j),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['secrets'] })
      setDeleteConfirm(null)
    },
  })

  const names = data?.names ?? []
  const managed = data?.managed ?? []
  const storedNames = new Set(names)
  const managedNames = new Set(managed.map(secret => secret.name))
  const otherNames = names.filter(name => !managedNames.has(name))
  const unusedReasons = new Map((data?.unused ?? []).map(item => [item.name, item.reason]))
  const foldedNewName = newName.trim().toUpperCase()
  const addManagedTarget = managed.find(secret => secret.name.toUpperCase() === foldedNewName)
  const handleManagedDraftChange = useCallback((name: string, hasDraft: boolean) => {
    setManagedDraftNames(current => {
      if (current.has(name) === hasDraft) return current
      const next = new Set(current)
      if (hasDraft) next.add(name)
      else next.delete(name)
      return next
    })
  }, [])
  const handleManagedPendingChange = useCallback((name: string, isPending: boolean) => {
    setManagedPendingNames(current => {
      if (current.has(name) === isPending) return current
      const next = new Set(current)
      if (isPending) next.add(name)
      else next.delete(name)
      return next
    })
  }, [])
  // A managed row's own set/delete is in flight: block an Add targeting the same
  // canonical name, or the delayed request could reorder after the Add's save and
  // silently erase the just-saved credential.
  const addTargetPending = addManagedTarget !== undefined && managedPendingNames.has(addManagedTarget.name)
  // The reverse direction: while the parent Add POST is in flight against a
  // managed canonical name, that row must be frozen too — otherwise a concurrent
  // delete/replace on the row can reorder around the Add and corrupt the value.
  const pendingAddManagedName = setMutation.isPending ? (setMutation.variables?.name ?? null) : null
  const hasAnyDraft = showAdd || deleteConfirm !== null || managedDraftNames.size > 0

  const handleAdd = () => {
    if (addTargetPending) return
    if (newName.trim() && newValue) {
      setMutation.mutate({ name: addManagedTarget?.name ?? newName.trim(), value: newValue })
    }
  }

  const openAdd = () => {
    setMutation.reset()
    setNewName('')
    setNewValue('')
    setShowAdd(true)
  }

  const cancelAdd = () => {
    setShowAdd(false)
    setNewName('')
    setNewValue('')
    setMutation.reset()
  }

  const requestDelete = (name: string) => {
    // Never reset a pending mutation: switching rows during an in-flight
    // DELETE would clear its gate and permit a duplicate whose delayed first
    // request could erase a re-saved value.
    if (deleteMutation.isPending) return
    deleteMutation.reset()
    setDeleteConfirm(name)
  }

  return (
    <SettingsSection title={i18nT('settings.secrets.title')}>
      <SettingsCard>
        <p className="text-sm text-muted mb-5">
          {i18nT('settings.secrets.description')}
        </p>

        {isLoading ? (
          <p className="text-sm text-muted">{i18nT('settings.secrets.loading')}</p>
        ) : (
          <>
            {/* Keep the Add path available; only agent navigation is unsafe with a draft. */}
            {isError && (
              <ErrorNotice
                className="mb-4"
                askAgent={!hasAnyDraft}
                message={i18nT('settings.secrets.load_error', {
                  error: (listError as Error).message,
                })}
              />
            )}

            {data?.managed_error && (
              <ErrorNotice
                className="mb-4"
                variant="inline"
                askAgent={!hasAnyDraft}
                message={i18nT('settings.secrets.managed_config_error')}
              />
            )}

            {!isError && managed.length === 0 && !showAdd && (
              <p className="mb-4 text-sm text-muted">
                {otherNames.length === 0 && (
                  <>
                    <span className="italic">{i18nT('settings.secrets.no_secrets')}</span>{' '}
                    <span>{i18nT('settings.secrets.managed_unconfigured_hint')}</span>{' '}
                  </>
                )}
                <a
                  href={SECRETS_SETUP_GUIDE}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent hover:underline"
                >
                  {i18nT('settings.secrets.managed_setup_guide')}
                </a>
              </p>
            )}

            {managed.length > 0 && (
              <div className="space-y-3 mb-5">
              <PanelSectionHeader
                label={i18nT('settings.secrets.managed_title')}
              />
              <p className="text-sm text-muted">
                {i18nT('settings.secrets.managed_description')}
              </p>
              <div className="space-y-2">
                {managed.map(secret => (
                  <ManagedSecretRow
                    key={secret.name}
                    secret={secret}
                    configured={storedNames.has(secret.name)}
                    allowAgentHandoff={!hasAnyDraft}
                    onDraftChange={handleManagedDraftChange}
                    onPendingChange={handleManagedPendingChange}
                    frozenByAdd={pendingAddManagedName === secret.name}
                  />
                ))}
              </div>
              </div>
            )}

            {otherNames.length > 0 && (
              <div className="space-y-3">
              <PanelSectionHeader
                label={i18nT(managed.length > 0
                  ? 'settings.secrets.other_stored_title'
                  : 'settings.secrets.stored_title')}
              />
              <p className="text-sm text-muted">
                {i18nT('settings.secrets.other_stored_description')}
              </p>

              {otherNames.length > 0 && (
                <div className="space-y-2">
                  {otherNames.map(name => (
                    <div
                      key={name}
                      className="flex flex-col gap-2 rounded bg-bg-elevated p-2 md:flex-row md:items-center md:justify-between"
                    >
                      <div className="min-w-0">
                        <div className="flex min-w-0 items-center gap-2">
                          <KeyRound className="lucide-inline shrink-0 text-muted" aria-hidden />
                          <span className="truncate font-mono text-sm">{name}</span>
                          <span className="shrink-0 text-[13px] text-muted">••••••••</span>
                        </div>
                        {unusedReasons.get(name) && (
                          <p className="mt-1 text-[12px] text-warn">
                            {i18nT(UNUSED_REASON_KEYS[unusedReasons.get(name)!])}
                          </p>
                        )}
                      </div>
                      {deleteConfirm === name ? (
                        <div className="flex flex-col items-start gap-1 md:items-end">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-[13px] text-warn">{i18nT('settings.secrets.delete_confirm', { name })}</span>
                            <Btn danger onClick={() => deleteMutation.mutate(name)} disabled={deleteMutation.isPending || setMutation.isPending}>
                              {i18nT('settings.secrets.delete')}
                            </Btn>
                            <Btn disabled={deleteMutation.isPending} onClick={() => { setDeleteConfirm(null); deleteMutation.reset() }}>
                              {i18nT('settings.secrets.cancel')}
                            </Btn>
                          </div>
                          {/* No hand-off while any editor or delete confirmation is active. */}
                          {deleteMutation.isError && (
                            <ErrorNotice
                              variant="inline"
                              askAgent={!hasAnyDraft}
                              message={i18nT('settings.secrets.delete_error', {
                                error: (deleteMutation.error as Error).message,
                              })}
                            />
                          )}
                        </div>
                      ) : (
                        <IconButton
                          variant="danger"
                          onClick={() => requestDelete(name)}
                          disabled={deleteMutation.isPending}
                          aria-label={i18nT('settings.secrets.delete_secret_name', { name })}
                          className="self-end md:self-auto"
                        >
                          <Trash2 className="lucide-inline" aria-hidden />
                        </IconButton>
                      )}
                    </div>
                  ))}
                </div>
              )}
              </div>
            )}

            {showAdd ? (
              <div className="mt-4 space-y-3 rounded border border-border p-3">
                <div>
                  <label
                    className="mb-1 block text-[13px] font-medium text-muted"
                    htmlFor="secret-name-input"
                  >
                    {i18nT('settings.secrets.name_label')}
                  </label>
                  <Input
                    id="secret-name-input"
                    type="text"
                    value={newName}
                    onChange={e => setNewName(e.target.value)}
                    placeholder={i18nT('settings.secrets.name_placeholder')}
                    className="font-mono"
                    aria-label={i18nT('settings.secrets.secret_name_aria')}
                    autoFocus
                  />
                  {addManagedTarget && (
                    <p className="mt-1 text-[12px] text-warn">
                      {i18nT('settings.secrets.managed_name_hint', { name: addManagedTarget.name })}
                    </p>
                  )}
                </div>
                <div>
                  <label className="mb-1 block text-[13px] font-medium text-muted" htmlFor="secret-value-input">
                    {i18nT('settings.secrets.value_label')}
                  </label>
                  <Input
                    id="secret-value-input"
                    type="password"
                    value={newValue}
                    onChange={e => setNewValue(e.target.value)}
                    placeholder={i18nT('settings.secrets.value_placeholder')}
                    className="font-mono"
                    aria-label={i18nT('settings.secrets.secret_value_aria')}
                  />
                </div>
                <div className="flex gap-2">
                  <Btn primary onClick={handleAdd} disabled={!newName.trim() || !newValue || setMutation.isPending || deleteMutation.isPending || addTargetPending}>
                    {setMutation.isPending
                      ? i18nT('settings.secrets.saving')
                      : i18nT('settings.secrets.save')}
                  </Btn>
                  <Btn disabled={setMutation.isPending} onClick={cancelAdd}>
                    {i18nT('settings.secrets.cancel')}
                  </Btn>
                </div>
                {/* No ask-agent hand-off: navigation would discard the unsaved value. */}
                {setMutation.isError && (
                  <ErrorNotice
                    variant="inline"
                    message={i18nT('settings.secrets.save_error', {
                      error: (setMutation.error as Error).message,
                    })}
                  />
                )}
              </div>
            ) : (
              <Btn onClick={openAdd} className="mt-4">
                <Plus className="lucide-inline mr-1" aria-hidden />
                {i18nT('settings.secrets.add_secret')}
              </Btn>
            )}
          </>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
