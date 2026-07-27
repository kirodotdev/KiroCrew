/**
 * RegistryManager — Manage federated external app registries.
 *
 * Allows users to add, edit, and remove org-owned app registries
 * directly from the Apps UI instead of editing config.json.
 */
import type React from 'react'
import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import {
  Plus, Trash2, GitBranch, Database, ExternalLink, RefreshCw, X, ShieldCheck,
} from 'lucide-react'
import { api } from '../api/client'
import { Card, CardTitle, Btn, Input, EmptyState, Badge } from './ui'
import InfoTip from './InfoTip'
import Clickable from './Clickable'
import { recordEvent } from '../rum'

type Registry = { name: string; repo: string; branch: string }

// Shell metacharacters / whitespace that must never appear in a repo value.
const SHELL_META = /[\s;&|`$(){}<>'"\\]/

/**
 * A repo value is valid if it is EITHER a legacy bare name
 * (`[A-Za-z0-9_-]+`) OR a git URL (https/ssh/scp-style). In all cases it must
 * be free of whitespace and shell metacharacters.
 */
function isValidRepo(repo: string): boolean {
  if (!repo || SHELL_META.test(repo)) return false
  if (/^[A-Za-z0-9_-]+$/.test(repo)) return true // legacy bare name
  // HTTPS only: plaintext http:// is rejected by the backend (registry clones
  // fetch manifests whose setup code later runs with gateway privileges, so an
  // unauthenticated transport is a MITM app-injection vector). Mirror that gate
  // client-side so the form validates before a guaranteed 400.
  if (/^https:\/\/\S+$/.test(repo)) return true // https URL
  if (/^ssh:\/\/\S+$/.test(repo)) return true // ssh:// URL
  if (/^git@[^\s:]+:\S+$/.test(repo)) return true // scp-style git@host:org/repo.git
  return false
}

/**
 * Derive a browsable https web URL from a repo value:
 *  - https URLs open as-is
 *  - scp/ssh forms convert to https://host/path (stripping a trailing .git)
 *  - bare names keep the legacy kirodotdev-labs URL
 */
function repoWebUrl(repo: string): string {
  if (/^https?:\/\//.test(repo)) return repo
  const scp = repo.match(/^git@([^:]+):(.+)$/)
  if (scp) return `https://${scp[1]}/${scp[2].replace(/\.git$/, '')}`
  const ssh = repo.match(/^ssh:\/\/(?:[^@/]+@)?([^/]+)\/(.+)$/)
  if (ssh) return `https://${ssh[1]}/${ssh[2].replace(/\.git$/, '')}`
  return `https://github.com/kirodotdev-labs/${repo}`
}

/**
 * When ``bare`` is set the Card chrome is neutralized so the manager embeds
 * flat inside another surface (the Apps page Sources popover) — same
 * behavior, no double border/padding.
 */
export default function RegistryManager({ bare = false }: { bare?: boolean } = {}) {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editName, setEditName] = useState('')
  const [editRepo, setEditRepo] = useState('')
  const [editBranch, setEditBranch] = useState('')
  const [error, setError] = useState('')
  const [trustNotice, setTrustNotice] = useState<string[]>([])
  const [lastSyncedAt, setLastSyncedAt] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['registries'],
    queryFn: () => api.listRegistries(),
  })
  const registries: Registry[] = data?.registries || []

  const mutation = useMutation({
    mutationFn: (regs: Registry[]) => api.updateRegistries(regs),
    onSuccess: (res: { newlyTrustedHosts?: string[] }) => {
      queryClient.invalidateQueries({ queryKey: ['registries'] })
      queryClient.invalidateQueries({ queryKey: ['registry'] })
      setError('')
      // Surface the trust grant that just happened. Adding a registry host is
      // not a neutral config edit: that host's apps become installable (their
      // setup runs with gateway privileges, signatures optional by default) and
      // ssh-form hosts join the loosened-sandbox clone set. The owner who
      // clicked "Add" is the one actor who should consciously acknowledge it,
      // so echo the backend's authoritative newlyTrustedHosts list here.
      setTrustNotice(res?.newlyTrustedHosts && res.newlyTrustedHosts.length > 0 ? res.newlyTrustedHosts : [])
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : 'Failed to update registries'),
  })

  const refreshMutation = useMutation({
    mutationFn: (repo?: string) => api.refreshRegistries(repo),
    onSuccess: (res: { lastSyncedAt?: string; ok?: boolean; failed?: string[] }) => {
      queryClient.invalidateQueries({ queryKey: ['registry'] })
      queryClient.invalidateQueries({ queryKey: ['registries'] })
      if (res?.lastSyncedAt) setLastSyncedAt(res.lastSyncedAt)
      // Surface per-registry failures instead of reporting a blanket success:
      // a failed refetch keeps serving the prior (stale) listing rather than
      // dropping the registry's apps, so the user must know it didn't sync.
      if (res?.ok === false && res.failed && res.failed.length > 0) {
        setError(`Could not refresh: ${res.failed.join(', ')} — still showing the last-synced apps for these.`)
      } else {
        setError('')
      }
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : 'Failed to refresh registries'),
  })

  const handleAdd = () => {
    const repo = editRepo.trim()
    // Send an empty name/branch when omitted so the BACKEND owns the defaults
    // (safe slug derivation + the 'main' branch default). Sending `name = repo`
    // for a URL made the backend reject it (400) since names disallow '/' & ':'.
    const name = editName.trim()
    const branch = editBranch.trim()
    if (!repo) { setError('Repo name is required'); return }
    if (!isValidRepo(repo)) {
      setError('Repo must be a git URL or an alphanumeric name (hyphens/underscores allowed)')
      return
    }
    if (registries.some(r => r.repo === repo)) {
      setError(`Registry "${repo}" already exists`)
      return
    }
    // Keep the form open and populated until the mutation actually succeeds:
    // clearing synchronously here would lose the user's input if the backend
    // rejects the value (e.g. 400), forcing them to reopen and re-enter it.
    mutation.mutate([...registries, { name, repo, branch }], {
      onSuccess: () => {
        setAdding(false)
        setEditName('')
        setEditRepo('')
        setEditBranch('')
      },
    })
    recordEvent('registry_add', { repo, name, branch })
  }

  const handleRemove = (repo: string) => {
    setTrustNotice([])
    mutation.mutate(registries.filter(r => r.repo !== repo))
    recordEvent('registry_remove', { repo })
  }

  // In bare mode swap the Card for a plain div so no card chrome
  // (border, padding, glow) leaks into the embedding surface.
  const Wrapper = bare ? 'div' : Card
  return (
    <Wrapper>
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

      {trustNotice.length > 0 && (
        <div className="mb-3 bg-accent/10 border border-accent/20 rounded-lg p-2.5 flex items-start gap-2 animate-rise">
          <ShieldCheck size={14} className="text-accent shrink-0 mt-0.5" />
          <span className="text-accent text-[13px] flex-1">
            You are now trusting apps from {trustNotice.join(', ')}. Apps from {trustNotice.length > 1 ? 'these hosts' : 'this host'} become installable and run setup with gateway privileges.
          </span>
          <Clickable className="text-accent/60 hover:text-accent" onClick={() => setTrustNotice([])} aria-label="Dismiss trust notice">
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
                onClick={() => window.open(repoWebUrl(reg.repo), '_blank')}
                aria-label={`Open ${reg.repo} repository`}
              >
                <ExternalLink size={14} />
              </Clickable>
              <Clickable
                className={`text-muted hover:text-accent transition-colors opacity-0 group-hover:opacity-100 ${refreshMutation.isPending ? 'pointer-events-none opacity-30' : ''}`}
                onClick={() => refreshMutation.mutate(reg.repo)}
                aria-label={`Refresh ${reg.name} registry`}
              >
                <RefreshCw size={14} className={refreshMutation.isPending && refreshMutation.variables === reg.repo ? 'animate-spin' : ''} />
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
                placeholder="https://github.com/org/app-registry"
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
                placeholder="main"
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
            <>
              <Btn
                onClick={() => refreshMutation.mutate(undefined)}
                disabled={refreshMutation.isPending}
                aria-label="Sync registry apps"
              >
                <RefreshCw size={14} className={refreshMutation.isPending && !refreshMutation.variables ? 'animate-spin' : ''} />
                {refreshMutation.isPending && !refreshMutation.variables ? 'Syncing…' : 'Sync Apps'}
              </Btn>
              {lastSyncedAt && (
                <span className="text-[12px] text-muted">
                  Last synced {new Date(lastSyncedAt).toLocaleTimeString()}
                </span>
              )}
            </>
          )}
        </div>
      )}
    </Wrapper>
  )
}
