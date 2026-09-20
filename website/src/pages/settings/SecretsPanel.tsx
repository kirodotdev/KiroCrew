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
import { api, isAuthExpiredError, type ManagedSecret, type SecretsListResponse } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { Link } from 'react-router-dom'
import { settingsPath } from '../../components/settingsPath'
import { connectionsOAuthClientEntryId } from '../../components/commandPalette/settingsManual'

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
 * At that moment a card reciting "Session expired. Run kirocrew token ... to
 * sign back in." points at a banner that is gone and a session that has been
 * replaced -- instructions for a state that no longer exists. Only a mutation's
 * own `reset()` clears it, because the failure lives in react-query's mutation
 * state and no refetch touches that.
 *
 * `isAuthExpiredError` is the second half of the guard. The event says auth
 * recovered, NOT that every request since has become valid, and the two are
 * easy to conflate: a disk-full save failure sitting unread on screen is still
 * true after an auth recovery, so resetting it would delete a message the user
 * has not acted on. Only a failure the event actually resolves is cleared.
 *
 * The listener is registered once and reads the latest callback through a ref, so
 * a caller can pass an inline closure without re-subscribing on every render.
 * Window-level, matching `_emitAuthEvent` and `KiroPrerequisiteGate`.
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

/** Reset *mutation* only if what it is showing is an expired-session failure. */
function resetIfAuthExpired(mutation: { error: unknown; reset: () => void }): void {
  if (isAuthExpiredError(mutation.error)) mutation.reset()
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
    mutationFn: () => api.secretsSave(secret.name, value),
    onSuccess: finish,
  })
  const deleteMutation = useMutation({
    mutationFn: () => api.secretsDelete(secret.name),
    onMutate: () => setMutation.reset(),
    onSuccess: finish,
  })
  const isPending = setMutation.isPending || deleteMutation.isPending
  useResetOnAuthRecovered(() => {
    resetIfAuthExpired(setMutation)
    resetIfAuthExpired(deleteMutation)
  })
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
    queryFn: () => api.secretsList(),
  })

  const setMutation = useMutation({
    mutationFn: (params: { name: string; value: string }) =>
      api.secretsSave(params.name, params.value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['secrets'] })
      setShowAdd(false)
      setNewName('')
      setNewValue('')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (name: string) => api.secretsDelete(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['secrets'] })
      setDeleteConfirm(null)
    },
  })

  const names = data?.names ?? []
  const managed = data?.managed ?? []
  useResetOnAuthRecovered(() => {
    resetIfAuthExpired(setMutation)
    resetIfAuthExpired(deleteMutation)
  })
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
                  secret.kind === 'connections_client_secret' ? (
                    // Owned by Settings → OAuth Apps: the entry is one half of a
                    // two-store record and its mutation must rebuild the agent
                    // spec, which only that panel's routes do (the generic vault
                    // routes answer 409 for this name). Listed so it is not
                    // mistaken for a stray user secret; edited elsewhere.
                    <div
                      key={secret.name}
                      className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg px-3 py-2 text-sm"
                    >
                      <div className="min-w-0">
                        <div className="truncate font-medium text-text">{secret.name}</div>
                        <div className="text-xs text-muted">
                          {i18nT('settings.secrets.connections_client_secret_description')}
                        </div>
                      </div>
                      <Link
                        to={settingsPath({ tab: 'connections', highlight: secret.host ? connectionsOAuthClientEntryId(secret.host) : undefined })}
                        className="shrink-0 text-xs text-accent hover:underline"
                      >
                        {i18nT('settings.secrets.connections_client_secret_manage')}
                      </Link>
                    </div>
                  ) : (
                  <ManagedSecretRow
                    key={secret.name}
                    secret={secret}
                    configured={storedNames.has(secret.name)}
                    allowAgentHandoff={!hasAnyDraft}
                    onDraftChange={handleManagedDraftChange}
                    onPendingChange={handleManagedPendingChange}
                    frozenByAdd={pendingAddManagedName === secret.name}
                  />
                  )
                ))}
              </div>
              </div>
            )}

            {/* Custom secrets: owner-defined vault entries that are not part of
                the managed credential catalog. Always rendered — even when
                empty — so the MCP consumption contract is explained up front. */}
            <div className="space-y-3">
              <PanelSectionHeader
                label={i18nT('settings.secrets.custom_title')}
              />
              <p className="text-sm text-muted">
                {i18nT('settings.secrets.custom_description')}
              </p>

              {otherNames.length > 0 ? (
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
              ) : (
                // Only assert an empty vault when the list actually loaded. On a
                // load failure otherNames is empty for lack of data, not because
                // the vault is empty — showing "no custom secrets" there would
                // invite re-adding an existing key and overwriting its value. The
                // top-level error notice already explains the failure.
                !isError && (
                  <p className="text-sm italic text-muted">
                    {i18nT('settings.secrets.custom_empty')}
                  </p>
                )
              )}
            </div>

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
