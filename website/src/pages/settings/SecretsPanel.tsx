import { useState } from 'react'
import { KeyRound, Plus, Trash2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { SettingsSection, SettingsCard } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/**
 * Parse a JSON response, REJECTING on a non-2xx status.
 *
 * A bare `r.json()` resolves for an error response too, so react-query treated a
 * 403/500 as a successful mutation: `onSuccess` fired and cleared the form,
 * silently discarding the secret the user had typed without ever storing it.
 * Throwing routes those statuses to `onError` instead, which leaves the form
 * populated so the value is not lost. Matches the `!r.ok` pattern used by
 * `src/api/pins.ts`.
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
const _sk = { 'X-Session-Key': localStorage.getItem('kiro_crew_token') || '' }
const get = (url: string) => fetch(url, { headers: { ..._sk } })
const post = (url: string, body?: object) =>
  fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(body) })
const del = (url: string) =>
  fetch(url, { method: 'DELETE', headers: { ..._sk } })

interface SecretsListResponse {
  names: string[]
}

export function SecretsPanel() {
  const queryClient = useQueryClient()
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [newValue, setNewValue] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)

  const { data, isLoading } = useQuery<SecretsListResponse>({
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

  const handleAdd = () => {
    if (newName.trim() && newValue) {
      setMutation.mutate({ name: newName.trim(), value: newValue })
    }
  }

  return (
    <SettingsSection title={i18nT('settings.secrets.title')}>
      <SettingsCard>
        <p className="text-sm text-muted mb-4">
          {i18nT('settings.secrets.description')}
        </p>

        {isLoading && <p className="text-sm text-muted">{i18nT('settings.secrets.loading')}</p>}

        {!isLoading && names.length === 0 && !showAdd && (
          <p className="text-sm text-muted italic">
            {i18nT('settings.secrets.no_secrets')}
          </p>
        )}

        {names.length > 0 && (
          <div className="space-y-2 mb-4">
            {names.map(name => (
              <div
                key={name}
                className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between p-2 rounded bg-bg-elevated"
              >
                <div className="flex items-center gap-2 min-w-0">
                  <KeyRound size={14} className="text-muted shrink-0" />
                  <span className="text-sm font-mono truncate">{name}</span>
                  <span className="text-xs text-muted shrink-0">••••••••</span>
                </div>
                {deleteConfirm === name ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-warn">{i18nT('settings.secrets.delete_confirm')}</span>
                    <Btn danger onClick={() => deleteMutation.mutate(name)}>
                      {i18nT('settings.secrets.delete')}
                    </Btn>
                    <Btn onClick={() => setDeleteConfirm(null)}>
                      {i18nT('settings.secrets.cancel')}
                    </Btn>
                  </div>
                ) : (
                  <button
                    onClick={() => setDeleteConfirm(name)}
                    className="p-1 rounded hover:bg-bg-hover text-muted hover:text-warn"
                    aria-label={i18nT('settings.secrets.delete_secret_name', { name })}
                  >
                    <Trash2 size={14} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {showAdd ? (
          <div className="space-y-3 border border-border rounded p-3">
            <div>
              <label className="text-xs font-medium text-muted block mb-1" htmlFor="secret-name-input">
                {i18nT('settings.secrets.name_label')}
                <input
                  id="secret-name-input"
                  type="text"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  placeholder="MY_API_KEY"
                  className="w-full px-2 py-1.5 text-sm rounded border border-border bg-bg-elevated text-text-strong mt-1 font-normal"
                  aria-label={i18nT('settings.secrets.secret_name_aria')}
                  autoFocus
                />
              </label>
            </div>
            <div>
              <label className="text-xs font-medium text-muted block mb-1" htmlFor="secret-value-input">
                {i18nT('settings.secrets.value_label')}
                <input
                  id="secret-value-input"
                  type="password"
                  value={newValue}
                  onChange={e => setNewValue(e.target.value)}
                  placeholder="sk-..."
                  className="w-full px-2 py-1.5 text-sm rounded border border-border bg-bg-elevated text-text-strong mt-1 font-normal"
                  aria-label={i18nT('settings.secrets.secret_value_aria')}
                />
              </label>
            </div>
            <div className="flex gap-2">
              <Btn primary onClick={handleAdd} disabled={!newName.trim() || !newValue}>
                {i18nT('settings.secrets.save')}
              </Btn>
              <Btn onClick={() => { setShowAdd(false); setNewName(''); setNewValue('') }}>
                {i18nT('settings.secrets.cancel')}
              </Btn>
            </div>
          </div>
        ) : (
          <Btn onClick={() => setShowAdd(true)} className="mt-2">
            <Plus size={14} className="mr-1" />
            {i18nT('settings.secrets.add_secret')}
          </Btn>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
