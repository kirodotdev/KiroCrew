import { useState } from 'react'
import { KeyRound, Plus, Trash2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { SettingsSection, SettingsCard } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
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
}

export function SecretsPanel() {
  const queryClient = useQueryClient()
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [newValue, setNewValue] = useState('')
  const [managedEditName, setManagedEditName] = useState<string | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)

  const { data, isLoading, isError, error } = useQuery<SecretsListResponse>({
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
      setManagedEditName(null)
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

  const handleAdd = () => {
    if (newName.trim() && newValue) {
      setMutation.mutate({ name: newName.trim(), value: newValue })
    }
  }

  const openAdd = (managedName: string | null = null) => {
    setMutation.reset()
    setManagedEditName(managedName)
    setNewName(managedName ?? '')
    setNewValue('')
    setShowAdd(true)
  }

  const cancelAdd = () => {
    setShowAdd(false)
    setNewName('')
    setNewValue('')
    setManagedEditName(null)
    setMutation.reset()
  }

  const managedCopy = (kind: string) => {
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

        {/* No hand-off while the add form holds an unsaved secret draft. */}
        {isError ? (
          <ErrorNotice
            askAgent={!showAdd}
            message={(error as Error)?.message || i18nT('api.client.unexpected_server_response')}
          />
        ) : isLoading ? (
          <p className="text-sm text-muted">{i18nT('settings.secrets.loading')}</p>
        ) : (
          <>
            {managed.length === 0 && otherNames.length === 0 && !showAdd && (
              <p className="text-sm text-muted italic">
                {i18nT('settings.secrets.no_secrets')}
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
                {managed.map(secret => {
                  const copy = managedCopy(secret.kind)
                  const configured = storedNames.has(secret.name)
                  return (
                    <div
                      key={secret.name}
                      className="flex flex-col gap-3 rounded bg-bg-elevated p-3 md:flex-row md:items-center md:justify-between"
                    >
                      <div className="flex min-w-0 items-start gap-2">
                        <KeyRound className="lucide-inline mt-0.5 shrink-0 text-muted" aria-hidden />
                        <div className="min-w-0">
                          <div className="text-[13px] font-semibold text-text-strong">
                            {copy?.label ?? secret.name}
                          </div>
                          {copy?.description && (
                            <div className="text-[12px] text-muted">{copy.description}</div>
                          )}
                          {secret.host && (
                            <code className="mt-1 block truncate text-[12px] text-text-strong">
                              {secret.host}
                            </code>
                          )}
                          <code className="mt-1 block truncate text-[12px] text-muted">{secret.name}</code>
                        </div>
                      </div>
                      {managedEditName === secret.name ? (
                        <div className="flex w-full max-w-md flex-col gap-2 md:w-auto">
                          <Input
                            type="password"
                            value={newValue}
                            onChange={e => setNewValue(e.target.value)}
                            aria-label={i18nT('settings.secrets.secret_value_aria')}
                            className="font-mono"
                            autoFocus
                          />
                          <div className="flex gap-2 self-end">
                            <Btn primary onClick={handleAdd} disabled={!newValue || setMutation.isPending}>
                              {setMutation.isPending
                                ? i18nT('settings.secrets.saving')
                                : i18nT('settings.secrets.save')}
                            </Btn>
                            <Btn disabled={setMutation.isPending} onClick={cancelAdd}>
                              {i18nT('settings.secrets.cancel')}
                            </Btn>
                          </div>
                          {/* No hand-off: navigation would discard the unsaved secret draft. */}
                          {setMutation.isError && (
                            <ErrorNotice
                              variant="inline"
                              message={i18nT('settings.secrets.save_error', {
                                error: (setMutation.error as Error).message,
                              })}
                            />
                          )}
                        </div>
                      ) : deleteConfirm === secret.name ? (
                        <div className="flex flex-col items-start gap-1 md:items-end">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-[13px] text-warn">{i18nT('settings.secrets.delete_confirm')}</span>
                            <Btn danger onClick={() => deleteMutation.mutate(secret.name)} disabled={deleteMutation.isPending || setMutation.isPending}>
                              {i18nT('settings.secrets.delete')}
                            </Btn>
                            <Btn disabled={deleteMutation.isPending} onClick={() => { setDeleteConfirm(null); deleteMutation.reset() }}>
                              {i18nT('settings.secrets.cancel')}
                            </Btn>
                          </div>
                          {/* No hand-off while the add form holds an unsaved secret draft. */}
                          {deleteMutation.isError && (
                            <ErrorNotice
                              variant="inline"
                              askAgent={!showAdd}
                              message={i18nT('settings.secrets.delete_error', {
                                error: (deleteMutation.error as Error).message,
                              })}
                            />
                          )}
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 self-end md:self-auto">
                          {configured && (
                            <span className="shrink-0 text-[13px] text-muted">••••••••</span>
                          )}
                          {!showAdd && (
                            <Btn onClick={() => openAdd(secret.name)} disabled={setMutation.isPending || deleteMutation.isPending}>
                              {configured
                                ? i18nT('components.secretField.replace')
                                : i18nT('settings.secrets.configure')}
                            </Btn>
                          )}
                          {configured && (
                            <IconButton
                              variant="danger"
                              onClick={() => requestDelete(secret.name)}
                              disabled={deleteMutation.isPending}
                              aria-label={i18nT('settings.secrets.delete_secret_name', { name: secret.name })}
                            >
                              <Trash2 className="lucide-inline" aria-hidden />
                            </IconButton>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
              </div>
            )}

            {otherNames.length > 0 && (
              <div className="space-y-3">
              <PanelSectionHeader
                label={i18nT('settings.secrets.other_stored_title')}
                count={otherNames.length}
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
                      <div className="flex min-w-0 items-center gap-2">
                        <KeyRound className="lucide-inline shrink-0 text-muted" aria-hidden />
                        <span className="truncate font-mono text-sm">{name}</span>
                        <span className="shrink-0 text-[13px] text-muted">••••••••</span>
                      </div>
                      {deleteConfirm === name ? (
                        <div className="flex flex-col items-start gap-1 md:items-end">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-[13px] text-warn">{i18nT('settings.secrets.delete_confirm')}</span>
                            <Btn danger onClick={() => deleteMutation.mutate(name)} disabled={deleteMutation.isPending || setMutation.isPending}>
                              {i18nT('settings.secrets.delete')}
                            </Btn>
                            <Btn disabled={deleteMutation.isPending} onClick={() => { setDeleteConfirm(null); deleteMutation.reset() }}>
                              {i18nT('settings.secrets.cancel')}
                            </Btn>
                          </div>
                          {/* No hand-off while the add form holds an unsaved secret draft. */}
                          {deleteMutation.isError && (
                            <ErrorNotice
                              variant="inline"
                              askAgent={!showAdd}
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

            {showAdd && managedEditName === null ? (
              <div className="mt-4 space-y-3 rounded border border-border p-3">
                <div>
                  <span className="mb-1 block text-[13px] font-medium text-muted">
                    {i18nT('settings.secrets.name_label')}
                  </span>
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
                  <Btn primary onClick={handleAdd} disabled={!newName.trim() || !newValue || setMutation.isPending || deleteMutation.isPending}>
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
            ) : !showAdd ? (
              <Btn onClick={() => openAdd()} className="mt-4">
                <Plus className="lucide-inline mr-1" aria-hidden />
                {i18nT('settings.secrets.add_secret')}
              </Btn>
            ) : null}
          </>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
