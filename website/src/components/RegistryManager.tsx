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
  Plus, Trash2, GitBranch, Database, ExternalLink, RefreshCw, X, ShieldCheck, Pin, Users,
} from 'lucide-react'
import { api, type ExternalRegistryRow } from '../api/client'
import { Card, CardTitle, Btn, Input, EmptyState, Badge } from './ui'
import InfoTip from './InfoTip'
import Clickable from './Clickable'
import { useImeGuard } from '../hooks/useImeGuard'
import { recordEvent } from '../rum'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
import { orderByReview } from './appstore/registryOrder'
// ``trust`` selects the credential posture for cloning a registry's apps, and it
// is meaningful only on a BUILD-PINNED row: the backend resolves the trusted tier
// solely from what the build supplies, because ``config.json`` is agent-writable.
// An operator row therefore always reads ``index``, and the API refuses to store
// anything else — so nothing here needs to preserve it across a save.
//
// ``label`` and ``review`` are build-only for the same reason, and are DISPLAY
// metadata: ``label`` is a human name shown instead of the ``name`` id (never in
// place of it — the id keys the index cache and installed apps' ``_registry``
// tag), and ``review`` says how thoroughly the listings were reviewed. Neither
// changes the credential posture.
//
// The shape is the API client's own row type rather than a second copy: a local
// duplicate is what lets the two drift, and the drift would be silent because the
// fields are structurally identical until one side gains a field.
type Registry = ExternalRegistryRow

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
 * The review badge's hover/label text for one pinned row.
 *
 * Two tiers × two credential postures = four whole sentences, deliberately not
 * assembled from fragments. The credential clause is a SECURITY fact, so it is
 * chosen by `trust` rather than assumed: a community registry pinned at the
 * credential-free `index` tier — the posture a community tier most likely wants —
 * must not be described as cloning with the user's credentials. Whole strings,
 * because gluing a review sentence to a credential sentence per language is the
 * concatenation the i18n gates exist to catch.
 *
 * Returns `''` for a row with no review tier; that row shows the pre-existing
 * trust badge instead and never reads this.
 */
function reviewTip(reg: Registry): string {
  const credentialed = reg.trust === 'owner'
  if (reg.review === 'curated') {
    return credentialed
      ? i18nT('components.registryManager.curated_tip_credentialed')
      : i18nT('components.registryManager.curated_tip_credential_free')
  }
  if (reg.review === 'community') {
    return credentialed
      ? i18nT('components.registryManager.community_tip_credentialed')
      : i18nT('components.registryManager.community_tip_credential_free')
  }
  return ''
}

/**
 * When ``bare`` is set the Card chrome is neutralized so the manager embeds
 * flat inside another surface (the Apps page Sources popover) — same
 * behavior, no double border/padding.
 */
export default function RegistryManager({ bare = false }: { bare?: boolean } = {}) {
  const queryClient = useQueryClient()
  const ime = useImeGuard()
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
    // handleAdd sends a REPLACE-ALL PUT built from this cached list, so a stale
    // cache would silently erase a registry added from another tab. Use a finite
    // staleTime so focus-refetch fires here (the global default is Infinity).
    staleTime: 30_000,
  })
  const registries: Registry[] = data?.registries || []
  // Registries this build pins. Deliberately NOT merged into `registries`:
  // handleAdd/handleRemove send a replace-all PUT built from that list, so a
  // pinned row folded in would be written into the operator's own config and
  // could then no longer be moved by a build update. Empty on the public
  // default, so this renders nothing unless a deployment pins one.
  const pinned: Registry[] = data?.pinned || []

  // ONE display list, ordered by review tier, so this card and the App Store
  // SOURCES rail cannot disagree about where a community source sits — the whole
  // point of the shared helper. Rendering the two arrays as two blocks put a
  // community row ABOVE an operator row here and below it on the rail, so a user
  // comparing the surfaces saw the sources swapped.
  //
  // The origin travels with each row rather than being inferred, because it
  // decides which controls the row gets: a pinned row must never offer remove,
  // and the mutating handlers keep reading `registries` alone. Ordering is
  // display only; nothing here reaches the PUT. Stable, so rows claiming no tier
  // keep the order the backend sent, with pinned rows still ahead of operator
  // rows inside that middle rank.
  const displayRows = orderByReview([
    ...pinned.map(reg => ({ ...reg, isPinned: true })),
    ...registries.map(reg => ({ ...reg, isPinned: false })),
  ])

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
    if (registries.some(r => r.repo === repo) || pinned.some(r => r.repo === repo)) {
      setError(i18nT('components.registryManager.registry_already_exists', { repo }))
      return
    }
    // A NAME collision with a pinned row is rejected too, not just a repo one:
    // the backend merge drops a same-named operator row ("a pinned row wins"),
    // so accepting it would render a row whose apps never appear and whose
    // per-row refresh 404s, with nothing on screen explaining why.
    const collidingName = name && pinned.find(r => (r.name || r.repo) === name)
    if (collidingName) {
      setError(i18nT('components.registryManager.name_is_taken_by_a_build_pinned_registry', { name }))
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

      {bare && (
        <p className="text-[12px] text-muted mb-3">{i18nT('components.registryManager.registry_url_install_public_repos_only')}</p>
      )}

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
      ) : registries.length === 0 && pinned.length === 0 && !adding ? (
        <EmptyState
          icon={<Database size={32} />}
          title={i18nT('components.registryManager.no_external_registries')}
          subtitle={i18nT('components.registryManager.add_an_org_registry_to_discover_team_specific_ap')}
        />
      ) : (
        <div className="space-y-2 mt-3">
          {/* ONE list, ordered by review tier, so this card and the App Store
              SOURCES rail agree. A row's origin decides its chrome and its
              controls: a pinned row comes from the build, not from config.json,
              so a delete button on it would appear to work and then be undone by
              the next read. */}
          {displayRows.map(reg => (
            <div
              key={reg.isPinned ? `pinned:${reg.repo}` : reg.repo}
              className={`flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5 border border-border rounded-lg group ${
                reg.isPinned ? 'bg-accent/5' : 'hover:border-accent/30 transition-colors'
              }`}
            >
              {/* ONE icon per tier, the same glyph the SOURCES rail uses, so a
                  reader comparing the surfaces is not left wondering why the same
                  source is drawn two ways.

                  The shield is reserved for a source someone is accountable for.
                  A pinned row defaults to the untrusted `index` tier, so a shield
                  on every pinned row would read as "verified" and over-claim.

                  A `community` row never gets it, whatever its trust tier: the
                  shield is the strongest reassurance on this card, and a source
                  whose listings nobody vetted must not carry it beside the badge
                  that says exactly that. It gets `Users`, which names who listed
                  it. `curated` earns the shield because a team read the listings.
                  `Pin` stays the untiered pinned row's mark, and an operator row
                  keeps its own Database icon. */}
              {!reg.isPinned
                ? <Database size={16} className="text-accent shrink-0" />
                : reg.review === 'community'
                  ? <Users size={16} className="text-muted shrink-0" aria-hidden="true" />
                  : reg.review === 'curated' || reg.trust === 'owner'
                    ? <ShieldCheck size={16} className="text-accent shrink-0" aria-hidden="true" />
                    : <Pin size={16} className="text-accent shrink-0" aria-hidden="true" />}
              {/* `basis-full` below `sm` gives the text its own line so the
                  always-visible controls wrap beneath it instead of landing on
                  top of the wrapped badges. */}
              <div className="basis-full sm:basis-0 sm:flex-1 min-w-0 order-last sm:order-none">
                <div className="flex items-center gap-2 flex-wrap">
                  {/* The label is a display name; the id stays visible beneath it
                      as a labelled subtitle rather than being replaced. Two rows
                      can share a label, and the id is what a support
                      conversation, an index cache path and an installed app's
                      `_registry` tag all name, so hiding it would leave nothing on
                      screen to match them against. With no label the id is the
                      title, as before. */}
                  <span className="font-medium text-text text-[14px] truncate">{reg.label || reg.name || reg.repo}</span>
                  <Badge variant="ok">{reg.branch}</Badge>
                  {reg.isPinned && (
                    <Badge variant="muted">{i18nT('components.registryManager.included_with_this_installation')}</Badge>
                  )}
                  {/* ONE badge carries the source's standing, and `review` wins
                      when the build set it.

                      Both axes must reach the user, but not as two badges: a row
                      reading "Trusted source" beside "Community" says opposite
                      things and the reader has to guess which governs. So the
                      review badge names the review, and its tip states the
                      credential posture in the same breath — which is why there
                      are two tips per tier rather than one. The credential
                      sentence is a security fact; picking it by `trust` keeps it
                      true for a community registry pinned at the credential-free
                      `index` tier, which is the posture a community tier most
                      likely wants.

                      The badge word carries the PAYLOAD, not the tier's name: a
                      community registry is usually already called something like
                      "Community apps", so a badge reading "Community" echoed the
                      title and left "not vetted" reachable only in the tip. "Not
                      vetted" is the fact, and it pairs with "Team reviewed".

                      The curated badge says "Team reviewed", not "Trusted": a
                      one-word-apart sibling of the "Trusted source" badge that
                      still ships for an untiered owner row is undecodable, and
                      the two mean different things — one that listings were read,
                      one that apps clone with the user's credentials.

                      Both words come from `components.appstore.registryTier`,
                      which the SOURCES rail reads too, so one tier can never be
                      named two things across the two surfaces.

                      With no review tier this is byte-for-byte the previous
                      behaviour: the owner tier's own badge and tip, nothing else.

                      The icons are decorative (aria-hidden) — the badge beside
                      them carries the meaning, labelled for a screen reader,
                      because a 16px glyph swap is undecodable and silent. */}
                  {!reg.review && reg.trust === 'owner' && (
                    <span className="inline-flex items-center gap-1">
                      <Badge variant="aim">{i18nT('components.registryManager.trusted_source')}</Badge>
                      <InfoTip text={i18nT('components.registryManager.trusted_source_clones_with_your_git_credentials')} />
                    </span>
                  )}
                  {reg.review === 'curated' && (
                    <span className="inline-flex items-center gap-1" title={reviewTip(reg)}>
                      <Badge variant="aim" aria-label={reviewTip(reg)}>
                        {i18nT('components.appstore.registryTier.curated')}
                      </Badge>
                      <InfoTip text={reviewTip(reg)} />
                    </span>
                  )}
                  {reg.review === 'community' && (
                    <span className="inline-flex items-center gap-1" title={reviewTip(reg)}>
                      <Badge variant="warn" aria-label={reviewTip(reg)}>
                        {i18nT('components.appstore.registryTier.community')}
                      </Badge>
                      <InfoTip text={reviewTip(reg)} />
                    </span>
                  )}
                </div>
                {/* The id subtitle is PREFIXED. Unprefixed, three near-identical
                    strings stack up on a community row — "Community apps" as the
                    title, "Community" on the badge, "community" underneath — and
                    a first-time reader cannot tell the small grey word is an
                    identifier rather than more label. */}
                {reg.label && reg.name && (
                  <div className="text-[11px] text-muted truncate mt-0.5">
                    {i18nT('components.registryManager.registry_id', { id: reg.name })}
                  </div>
                )}
                <div className="text-[12px] text-muted truncate flex items-center gap-1.5 mt-0.5">
                  <GitBranch size={10} className="shrink-0" />
                  {reg.repo}
                </div>
              </div>
              {/* Opening the repo is read-only, so a pinned row offers it too —
                  withholding it would make the pinned source harder to inspect
                  than a user-added one. Only the MUTATING control is absent.
                  Controls stay visible without hover below `sm`: a touch
                  viewport has no hover, so hover-only actions are unreachable. */}
              <Clickable
                className="text-muted hover:text-accent transition-colors opacity-100 sm:opacity-0 sm:group-hover:opacity-100"
                onClick={() => window.open(repoWebUrl(reg.repo), '_blank')}
                aria-label={i18nT('components.registryManager.open_repository', { repo: reg.repo })}
              >
                <ExternalLink size={14} />
              </Clickable>
              <Clickable
                className={`text-muted hover:text-accent transition-colors opacity-100 sm:opacity-0 sm:group-hover:opacity-100 ${refreshMutation.isPending ? 'pointer-events-none opacity-30' : ''}`}
                onClick={() => refreshMutation.mutate(reg.repo)}
                aria-label={i18nT('components.registryManager.refresh_registry', { name: reg.label || reg.name || reg.repo })}
              >
                <RefreshCw size={14} className={refreshMutation.isPending && refreshMutation.variables === reg.repo ? 'animate-spin' : ''} />
              </Clickable>
              {!reg.isPinned && (
                <Clickable
                  className={`text-muted hover:text-danger transition-colors opacity-100 sm:opacity-0 sm:group-hover:opacity-100 ${mutation.isPending ? 'pointer-events-none opacity-30' : ''}`}
                  onClick={() => handleRemove(reg.repo)}
                  aria-label={i18nT('components.registryManager.remove_registry', { name: reg.name })}
                >
                  <Trash2 size={14} />
                </Clickable>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Add form */}
      {adding ? (
        <div className="mt-4 border border-accent/30 rounded-lg p-4 bg-accent/5 animate-rise">
          <div className="grid grid-cols-[1fr_1fr_0.7fr] gap-3 mb-3 [&>div]:min-w-0">
            <div>
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
              <label htmlFor="registry-repo" className="text-[12px] text-muted mb-1 block">{i18nT('components.registryManager.repo')}</label>
              <Input
                id="registry-repo"
                className="w-full"
                placeholder={i18nT('components.registryManager.https_github_com_org_app_registry')}
                value={editRepo}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditRepo(e.target.value)}
                {...ime.bindComposition()}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
                  if (e.key !== 'Enter') return
                  // Rule 1: single-line input — decline the IME's committing Enter only.
                  if (ime.isComposing(e)) return
                  handleAdd()
                }}
              />
            </div>
            <div>
              <label htmlFor="registry-branch" className="text-[12px] text-muted mb-1 block">{i18nT('components.registryManager.branch')}</label>
              <Input
                id="registry-branch"
                className="w-full"
                placeholder={i18nT('components.registryManager.main')}
                value={editBranch}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEditBranch(e.target.value)}
                {...ime.bindComposition()}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
                  if (e.key !== 'Enter') return
                  // Rule 1: single-line input — one shared instance covers both fields.
                  if (ime.isComposing(e)) return
                  handleAdd()
                }}
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
          {registries.length > 0 || pinned.length > 0 ? (
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
          ) : null}
        </div>
      )}
    </Wrapper>
  )
}
