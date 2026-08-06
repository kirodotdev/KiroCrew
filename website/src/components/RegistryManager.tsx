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

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
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
    onError: (e: unknown) => setError(e instanceof Error ? e.message : i18nT('components.registryManager.failed_to_update_registries')),
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
        setError(i18nT('components.registryManager.could_not_refresh_still_showing_last_synced', { names: res.failed.join(', ') }))
      } else {
        setError('')
      }
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : i18nT('components.registryManager.failed_to_refresh_registries')),
  })

  const handleAdd = () => {
    const repo = editRepo.trim()
    // Send an empty name/branch when omitted so the BACKEND owns the defaults
    // (safe slug derivation + the 'main' branch default). Sending `name = repo`
    // for a URL made the backend reject it (400) since names disallow '/' & ':'.
    const name = editName.trim()
    const branch = editBranch.trim()
    if (!repo) { setError(i18nT('components.registryManager.repo_name_is_required')); return }
    if (!isValidRepo(repo)) {
      setError(i18nT('components.registryManager.repo_must_be_a_git_url_or_an_alphanumeric_name_h'))
      return
    }
    if (registries.some(r => r.repo === repo)) {
      setError(i18nT('components.registryManager.registry_already_exists', { repo }))
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
        {i18nT('components.registryManager.external_registries')}
        <InfoTip text={i18nT('components.registryManager.org_owned_app_catalogs_hosted_in_git_repositorie')} />
      </CardTitle>

      {/* No hand-off: the notice sits beside unsaved form input, and the button
          navigates away — which would discard what the user typed. */}
      <ErrorNotice message={error} onDismiss={() => setError('')} className="mb-3 animate-rise" />

      {trustNotice.length > 0 && (
        <div className="mb-3 bg-accent/10 border border-accent/20 rounded-lg p-2.5 flex items-start gap-2 animate-rise">
          <ShieldCheck size={14} className="text-accent shrink-0 mt-0.5" />
          <span className="text-accent text-[13px] flex-1">
            {i18nT('components.registryManager.you_are_now_trusting_apps_from')} {trustNotice.join(', ')}{i18nT('components.registryManager.apps_from')} {trustNotice.length > 1 ? i18nT('components.registryManager.these_hosts') : i18nT('components.registryManager.this_host')} {i18nT('components.registryManager.become_installable_and_run_setup_with_gateway_pr')}
          </span>
          <Clickable className="text-accent/60 hover:text-accent" onClick={() => setTrustNotice([])} aria-label={i18nT('components.registryManager.dismiss_trust_notice')}>
            <X size={14} />
          </Clickable>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-8 text-muted text-sm">{i18nT('components.registryManager.loading')}</div>
      ) : registries.length === 0 && !adding ? (
        <EmptyState
          icon={<Database size={32} />}
          title={i18nT('components.registryManager.no_external_registries')}
          subtitle={i18nT('components.registryManager.add_an_org_registry_to_discover_team_specific_ap')}
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
                aria-label={i18nT('components.registryManager.open_repository', { repo: reg.repo })}
              >
                <ExternalLink size={14} />
              </Clickable>
              <Clickable
                className={`text-muted hover:text-accent transition-colors opacity-0 group-hover:opacity-100 ${refreshMutation.isPending ? 'pointer-events-none opacity-30' : ''}`}
                onClick={() => refreshMutation.mutate(reg.repo)}
                aria-label={i18nT('components.registryManager.refresh_registry', { name: reg.name })}
              >
                <RefreshCw size={14} className={refreshMutation.isPending && refreshMutation.variables === reg.repo ? 'animate-spin' : ''} />
              </Clickable>
              <Clickable
                className={`text-muted hover:text-danger transition-colors opacity-0 group-hover:opacity-100 ${mutation.isPending ? 'pointer-events-none opacity-30' : ''}`}
                onClick={() => handleRemove(reg.repo)}
                aria-label={i18nT('components.registryManager.remove_registry', { name: reg.name })}
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
          <div className="grid grid-cols-[1fr_1fr_0.7fr] gap-3 mb-3 [&>div]:min-w-0">
            <div>
              {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
              <label htmlFor="registry-name" className="text-[12px] text-muted mb-1 block">{i18nT('components.registryManager.display_name')}</label>
              <Input
                id="registry-name"
                className="w-full"
                placeholder={i18nT('components.registryManager.e_g_identity_services')}
                value={editName}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditName(e.target.value)}
              />
            </div>
            <div>
              {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
              <label htmlFor="registry-repo" className="text-[12px] text-muted mb-1 block">{i18nT('components.registryManager.repo')}</label>
              <Input
                id="registry-repo"
                className="w-full"
                placeholder={i18nT('components.registryManager.https_github_com_org_app_registry')}
                value={editRepo}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditRepo(e.target.value)}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => e.key === 'Enter' && handleAdd()}
              />
            </div>
            <div>
              {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
              <label htmlFor="registry-branch" className="text-[12px] text-muted mb-1 block">{i18nT('components.registryManager.branch')}</label>
              <Input
                id="registry-branch"
                className="w-full"
                placeholder={i18nT('components.registryManager.main')}
                value={editBranch}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditBranch(e.target.value)}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => e.key === 'Enter' && handleAdd()}
              />
            </div>
          </div>
          <div className="flex items-center gap-2 justify-end">
            <Btn onClick={() => { setAdding(false); setError('') }}>{i18nT('components.registryManager.cancel')}</Btn>
            <Btn onClick={handleAdd} disabled={mutation.isPending}>
              {mutation.isPending ? i18nT('components.registryManager.adding') : i18nT('components.registryManager.add_registry')}
            </Btn>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex items-center gap-2">
          <Btn onClick={() => setAdding(true)}>
            <Plus size={14} /> {i18nT('components.registryManager.add_registry')}
          </Btn>
          {registries.length > 0 && (
            <>
              <Btn
                onClick={() => refreshMutation.mutate(undefined)}
                disabled={refreshMutation.isPending}
                aria-label={i18nT('components.registryManager.sync_registry_apps')}
              >
                <RefreshCw size={14} className={refreshMutation.isPending && !refreshMutation.variables ? 'animate-spin' : ''} />
                {refreshMutation.isPending && !refreshMutation.variables ? i18nT('components.registryManager.syncing') : i18nT('components.registryManager.sync_apps')}
              </Btn>
              {lastSyncedAt && (
                <span className="text-[12px] text-muted">
                  {i18nT('components.registryManager.last_synced')} {fmtTimeNumeric(lastSyncedAt)}
                </span>
              )}
            </>
          )}
        </div>
      )}
    </Wrapper>
  )
}
