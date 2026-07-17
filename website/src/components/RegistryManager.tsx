/**
 * RegistryManager — Manage federated external app registries.
 *
 * Allows users to add, edit, and remove org-owned app registries
 * directly from the App Store UI instead of editing config.json.
 */
import type React from 'react'
import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import {
  Plus, Trash2, GitBranch, Database, ExternalLink, RefreshCw, X,
} from 'lucide-react'
import { api } from '../api/client'
import { Card, CardTitle, Btn, Input, EmptyState, Badge } from './ui'
import InfoTip from './InfoTip'
import Clickable from './Clickable'
import { recordEvent } from '../rum'

type Registry = { name: string; repo: string; branch: string }

export default function RegistryManager() {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editName, setEditName] = useState('')
  const [editRepo, setEditRepo] = useState('')
  const [editBranch, setEditBranch] = useState('mainline')
  const [error, setError] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['registries'],
    queryFn: () => api.listRegistries(),
  })
  const registries: Registry[] = data?.registries || []

  const mutation = useMutation({
    mutationFn: (regs: Registry[]) => api.updateRegistries(regs),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['registries'] })
      queryClient.invalidateQueries({ queryKey: ['registry'] })
      setError('')
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : 'Failed to update registries'),
  })

  const handleAdd = () => {
    const repo = editRepo.trim()
    const name = editName.trim() || repo
    const branch = editBranch.trim() || 'mainline'
    if (!repo) { setError('Repo name is required'); return }
    if (!/^[A-Za-z0-9_\-]+$/.test(repo)) {
      setError('Repo must be alphanumeric (hyphens/underscores allowed)')
      return
    }
    if (registries.some(r => r.repo === repo)) {
      setError(`Registry "${repo}" already exists`)
      return
    }
    mutation.mutate([...registries, { name, repo, branch }])
    recordEvent('registry_add', { repo, name, branch })
    setAdding(false)
    setEditName('')
    setEditRepo('')
    setEditBranch('mainline')
  }

  const handleRemove = (repo: string) => {
    mutation.mutate(registries.filter(r => r.repo !== repo))
    recordEvent('registry_remove', { repo })
  }

  return (
    <Card>
      <CardTitle>
        External Registries
        <InfoTip text="Org-owned app catalogs hosted in Git repositories. Apps from these repos appear alongside core registry apps in the Browse tab." />
      </CardTitle>

      {error && (
        <div className="mb-3 bg-danger/10 border border-danger/20 rounded-lg p-2.5 flex items-center gap-2 animate-rise">
          <span className="text-danger text-[13px] flex-1">{error}</span>
          <Clickable className="text-danger/60 hover:text-danger" onClick={() => setError('')} aria-label="Dismiss error">
            <X size={14} />
          </Clickable>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-8 text-muted text-sm">Loading…</div>
      ) : registries.length === 0 && !adding ? (
        <EmptyState
          icon={<Database size={32} />}
          title="No external registries"
          subtitle="Add an org registry to discover team-specific apps"
        />
      ) : (
        <div className="space-y-2 mt-3">
          {registries.map(reg => (
            <div
              key={reg.repo}
              className="flex items-center gap-3 px-3 py-2.5 border border-border rounded-lg hover:border-accent/30 transition-colors group"
            >
              <Database size={16} className="text-accent shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-text text-[14px] truncate">{reg.name}</span>
                  <Badge variant="ok">{reg.branch}</Badge>
                </div>
                <div className="text-[12px] text-muted truncate flex items-center gap-1.5 mt-0.5">
                  <GitBranch size={10} className="shrink-0" />
                  {reg.repo}
                </div>
              </div>
              <Clickable
                className="text-muted hover:text-accent transition-colors opacity-0 group-hover:opacity-100"
                onClick={() => window.open(`https://github.com/kirodotdev-labs/${reg.repo}`, '_blank')}
                aria-label={`Open ${reg.repo} repository`}
              >
                <ExternalLink size={14} />
              </Clickable>
              <Clickable
                className={`text-muted hover:text-danger transition-colors opacity-0 group-hover:opacity-100 ${mutation.isPending ? 'pointer-events-none opacity-30' : ''}`}
                onClick={() => handleRemove(reg.repo)}
                aria-label={`Remove ${reg.name} registry`}
              >
                <Trash2 size={14} />
              </Clickable>
            </div>
          ))}
        </div>
      )}

      {/* Add form */}
      {adding ? (
        <div className="mt-4 border border-accent/30 rounded-lg p-4 bg-accent/5 animate-rise">
          <div className="grid grid-cols-[1fr_1fr_auto] gap-3 mb-3">
            <div>
              {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
              <label htmlFor="registry-name" className="text-[12px] text-muted mb-1 block">Display Name</label>
              <Input
                id="registry-name"
                placeholder="e.g. Identity Services"
                value={editName}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditName(e.target.value)}
              />
            </div>
            <div>
              {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
              <label htmlFor="registry-repo" className="text-[12px] text-muted mb-1 block">Repo *</label>
              <Input
                id="registry-repo"
                placeholder="e.g. my-kirocrew-app-registry"
                value={editRepo}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditRepo(e.target.value)}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => e.key === 'Enter' && handleAdd()}
              />
            </div>
            <div>
              {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
              <label htmlFor="registry-branch" className="text-[12px] text-muted mb-1 block">Branch</label>
              <Input
                id="registry-branch"
                placeholder="mainline"
                value={editBranch}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditBranch(e.target.value)}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => e.key === 'Enter' && handleAdd()}
              />
            </div>
          </div>
          <div className="flex items-center gap-2 justify-end">
            <Btn onClick={() => { setAdding(false); setError('') }}>Cancel</Btn>
            <Btn onClick={handleAdd} disabled={mutation.isPending}>
              {mutation.isPending ? 'Adding…' : 'Add Registry'}
            </Btn>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex items-center gap-2">
          <Btn onClick={() => setAdding(true)}>
            <Plus size={14} /> Add Registry
          </Btn>
          {registries.length > 0 && (
            <Btn
              onClick={() => queryClient.invalidateQueries({ queryKey: ['registry'] })}
              aria-label="Refresh registry apps"
            >
              <RefreshCw size={14} /> Sync Apps
            </Btn>
          )}
        </div>
      )}
    </Card>
  )
}
