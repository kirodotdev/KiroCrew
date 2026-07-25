import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell, RefreshCw, ExternalLink, Trash2, AlertTriangle, Sparkles, ListChecks, Users, Wand2, Plus, Check, type LucideIcon,
} from 'lucide-react'
import GithubLogo from '../../../../components/icons/GithubLogo'
import {
  issueRadarApi, DEFAULT_REPO_SETTINGS,
  type RepoSettings, type LabelRecommendation, type RepoLabel, type Issue, type RepoMember,
} from '../../api'
import { useIssueRadar } from '../../context'
import ReadOnlyTag, { isReadOnly } from '../../components/ReadOnlyTag'
import LabelPicker from '../../components/LabelPicker'
import { readableText, relativeDate, asArray } from '../../lib/format'

// Heuristic name patterns used only to *suggest* likely labels (one-click add);
// the user always confirms. Repos name these things a dozen different ways.
const TRIAGE_PATTERN = /(^|[\s:_/-])(triage|untriaged|unconfirmed|pending|needs?[\s_/-]?(triage|info|repro|reproduction|investigation|review|response|details?|decision))/i
const GFI_PATTERN = /(good[\s_/-]?first[\s_/-]?issue|first[\s_/-]?timers?|help[\s_/-]?wanted|beginner|newcomer|starter|low[\s_/-]?hanging|(^|[\s:_/-])easy([\s:_/-]|$))/i

/** Human labels for a member's repo role (collaborators roster: admin/maintain/
 * write/triage/read) and, for the read-only derived fallback, the
 * author_association vocabulary (OWNER/MEMBER/COLLABORATOR). */
const ROLE_LABEL: Record<string, string> = {
  admin: 'Admin', maintain: 'Maintainer', write: 'Write', triage: 'Triage', read: 'Read',
  OWNER: 'Owner', MEMBER: 'Member', COLLABORATOR: 'Collaborator', member: 'Member',
}
/** Roles that are collaborators but not maintainers — muted rather than accent. */
const ROLE_MUTED = new Set(['read'])

/** One repo's settings — full width. Local-only triage preferences that teach
 * Issue Radar how this repo labels its work (which labels mean "needs triage",
 * which mark newcomer-friendly issues), plus a per-repo data refresh and a
 * local disconnect. Nothing here is written back to GitHub. */
export default function RepoSettings({ owner, repo }: { owner: string; repo: string }) {
  const qc = useQueryClient()
  const { repos, openSettings } = useIssueRadar()
  const entry = repos.find((r) => r.owner === owner && r.repo === repo)

  const labelsQuery = useQuery({
    queryKey: ['issue-radar', 'labels', owner, repo],
    queryFn: () => issueRadarApi.labels(owner, repo),
  })
  const settingsQuery = useQuery({
    queryKey: ['issue-radar', 'settings', owner, repo],
    queryFn: () => issueRadarApi.getSettings(owner, repo),
  })
  const issuesQuery = useQuery({
    queryKey: ['issue-radar', 'issues', owner, repo, 'open'],
    queryFn: () => issueRadarApi.issues(owner, repo, { state: 'open' }),
  })
  // Members are derived server-side from the cached issues, so wait until the
  // issues query has succeeded (by then the member cache is built) — same gate
  // as the shared context, to avoid a redundant fetch or an empty first read.
  const membersQuery = useQuery({
    queryKey: ['issue-radar', 'members', owner, repo],
    queryFn: () => issueRadarApi.members(owner, repo),
    enabled: issuesQuery.isSuccess,
  })

  const labels = asArray<RepoLabel>(labelsQuery.data?.labels)
  const openIssues = useMemo(() => asArray<Issue>(issuesQuery.data?.issues), [issuesQuery.data])
  const members = useMemo(() => asArray<RepoMember>(membersQuery.data?.members), [membersQuery.data])
  const memberSource = membersQuery.data?.source ?? null
  const membersLoading = issuesQuery.isLoading || membersQuery.isFetching

  const countByLabel = useMemo(() => {
    const m = new Map<string, number>()
    for (const i of openIssues) for (const n of i.labels) m.set(n, (m.get(n) ?? 0) + 1)
    return m
  }, [openIssues])

  // Local draft is the UI's source of truth once the user edits, so the toggle
  // and label chips respond instantly and never "snap back" on a slow or failed
  // save. Saves run in the background; on success we sync the shared
  // ['settings', owner, repo] cache so the active-repo dashboards pick up the
  // change. Failures surface in the banner below instead of silently reverting.
  const [draft, setDraft] = useState<RepoSettings | null>(null)
  const settings = draft ?? settingsQuery.data?.settings ?? DEFAULT_REPO_SETTINGS

  const saveMutation = useMutation({
    mutationFn: (next: RepoSettings) => issueRadarApi.putSettings(owner, repo, next),
    onSuccess: (res) => qc.setQueryData(['issue-radar', 'settings', owner, repo], res),
  })

  const commit = (next: RepoSettings) => { setDraft(next); saveMutation.mutate(next) }
  const update = (patch: Partial<RepoSettings>) => commit({ ...settings, ...patch })
  const toggleIn = (key: 'triage_labels' | 'good_first_issue_labels', name: string) => {
    const set = new Set(settings[key])
    if (set.has(name)) set.delete(name)
    else set.add(name)
    update({ [key]: [...set] } as Partial<RepoSettings>)
  }
  const addMany = (key: 'triage_labels' | 'good_first_issue_labels', names: string[]) => {
    const set = new Set(settings[key])
    names.forEach((n) => set.add(n))
    update({ [key]: [...set] } as Partial<RepoSettings>)
  }

  // ── live counts under the current definition ──
  const triageCount = useMemo(
    () => openIssues.filter(
      (i) => (settings.unlabeled_is_untriaged && i.labels.length === 0)
        || i.labels.some((l) => settings.triage_labels.includes(l)),
    ).length,
    [openIssues, settings],
  )
  const gfiCount = useMemo(
    () => openIssues.filter((i) => i.labels.some((l) => settings.good_first_issue_labels.includes(l))).length,
    [openIssues, settings],
  )

  // ── per-repo refresh (this repo's issues + labels) ──
  const refreshMutation = useMutation({
    mutationFn: async () => {
      const [iss, lab] = await Promise.all([
        issueRadarApi.issues(owner, repo, { refresh: true, state: 'open' }),
        issueRadarApi.labels(owner, repo, { refresh: true }),
      ])
      return { iss, lab }
    },
    onSuccess: ({ iss, lab }) => {
      qc.setQueryData(['issue-radar', 'issues', owner, repo, 'open'], iss)
      qc.setQueryData(['issue-radar', 'labels', owner, repo], lab)
      // A fresh issues fetch rebuilds the member cache server-side; re-read it.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'members', owner, repo] })
    },
  })

  // ── disconnect (local-only) ──
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const disconnectMutation = useMutation({
    mutationFn: () => issueRadarApi.disconnect(owner, repo),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['issue-radar', 'repos'] })
      // The connect dialog's picker caches a `connected` flag per repo, so
      // without this the just-disconnected repo stays greyed out as "Connected"
      // and un-tickable until that cache expires.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'recent-repos'] })
      openSettings({ kind: 'general', anchor: 'repos' })
    },
  })

  // ── AI label recommendations (propose NEW labels for this repo) ──
  const writable = !isReadOnly(entry?.permissions)
  const recoQuery = useQuery({
    queryKey: ['issue-radar', 'recommendations', owner, repo],
    queryFn: () => issueRadarApi.getRecommendations(owner, repo),
  })
  const recommendations = recoQuery.data?.recommendations ?? null
  const generateReco = useMutation({
    mutationFn: () => issueRadarApi.generateRecommendations(owner, repo),
    onSuccess: (res) => qc.setQueryData(['issue-radar', 'recommendations', owner, repo], res),
  })
  const [dismissedRecos, setDismissedRecos] = useState<Set<string>>(new Set())
  const [createdLabels, setCreatedLabels] = useState<Set<string>>(new Set())
  const createLabel = useMutation({
    mutationFn: (rec: LabelRecommendation) =>
      issueRadarApi.createLabel(owner, repo, { name: rec.name, color: rec.color, description: rec.description }),
    onSuccess: (_res, rec) => {
      setCreatedLabels((prev) => new Set(prev).add(rec.name))
      // The new label now exists — refresh the pickers, and for triage/newcomer
      // roles map it straight into the corresponding settings set.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'labels', owner, repo] })
      if (rec.category === 'triage') addMany('triage_labels', [rec.name])
      else if (rec.category === 'first-issue') addMany('good_first_issue_labels', [rec.name])
    },
  })
  const visibleRecos = asArray<LabelRecommendation>(recommendations).filter((r) => !dismissedRecos.has(r.name))

  return (
    <div className="w-full max-w-6xl px-8 py-8">
      {/* Header */}
      <div className="flex items-center gap-3 mb-1 flex-wrap">
        <GithubLogo size={20} className="flex-shrink-0" />
        <h1 className="text-[22px] font-semibold">{owner}/{repo}</h1>
        {isReadOnly(entry?.permissions) && <ReadOnlyTag />}
        <a
          href={`https://github.com/${owner}/${repo}`}
          target="_blank"
          rel="noreferrer"
          className="text-[12px] text-muted hover:text-text inline-flex items-center gap-1"
        >
          <ExternalLink size={12} /> Open on GitHub
        </a>
        <button
          onClick={() => refreshMutation.mutate()}
          disabled={refreshMutation.isPending}
          title="Re-fetch this repo's issues + labels from GitHub"
          className="ml-auto inline-flex items-center gap-1.5 text-[12px] text-muted hover:text-text disabled:opacity-40 cursor-pointer"
        >
          <RefreshCw size={13} className={refreshMutation.isPending ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>
      <p className="text-[13px] text-muted mb-4">
        Local triage settings for this repo — they teach Issue Radar how {repo} organises its issues and are never written back to GitHub.
        {saveMutation.isPending
          ? <span className="ml-2 opacity-70">Saving…</span>
          : saveMutation.isSuccess ? <span className="ml-2 opacity-70 inline-flex items-center gap-1">Saved <Check size={12} className="lucide-inline" /></span> : null}
      </p>

      {(settingsQuery.isError || saveMutation.isError) && (
        <div className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 mb-6 text-[12px] text-danger">
          <div className="font-medium mb-0.5">
            {settingsQuery.isError ? "Couldn't load saved settings." : "Couldn't save your changes."}
          </div>
          <div className="opacity-80">
            {((settingsQuery.error ?? saveMutation.error) as Error)?.message}
            {' — '}your edits are kept here but won't persist. If you just updated Issue Radar, restart the backend so the settings API is available.
          </div>
        </div>
      )}

      <Card
        icon={Bell}
        title="Notifications"
        desc="Watch this repo in the background and post a KiroCrew notification whenever a new issue is opened."
      >
        <SettingToggle
          on={settings.notify_on_new_issue}
          onClick={() => update({ notify_on_new_issue: !settings.notify_on_new_issue })}
        >
          Notify me when a <strong>new issue</strong> is opened in {owner}/{repo}
        </SettingToggle>
        <StatLine>
          Checks about once a minute, inside KiroCrew — no cron job. It only runs while KiroCrew is
          open and your machine is awake, using your existing <code>gh</code> sign-in (no extra
          credentials, no GitHub webhook).
        </StatLine>
      </Card>

      <Card
        icon={ListChecks}
        title="Triage labels"
        desc="Which labels mean an issue still needs triage. Drives the Overview's “Untriaged” count and the needs-attention queue."
      >
        <SettingToggle
          on={settings.unlabeled_is_untriaged}
          onClick={() => update({ unlabeled_is_untriaged: !settings.unlabeled_is_untriaged })}
        >
          Also treat issues with <strong>no labels</strong> as needing triage
        </SettingToggle>
        <LabelPicker
          labels={labels}
          selected={settings.triage_labels}
          onToggle={(n) => toggleIn('triage_labels', n)}
          onAddMany={(ns) => addMany('triage_labels', ns)}
          countByLabel={countByLabel}
          suggestPattern={TRIAGE_PATTERN}
          loading={labelsQuery.isLoading}
          error={labelsQuery.error as Error | null}
        />
        <StatLine>
          {issuesQuery.isLoading
            ? 'Counting open issues…'
            : <><strong className="text-text">{triageCount}</strong> of {openIssues.length} open issues currently need triage</>}
        </StatLine>
      </Card>

      <Card
        icon={Sparkles}
        title="Good first issue labels"
        desc="Which labels mark newcomer-friendly work, so Issue Radar can surface issues to route to first-time contributors."
      >
        <LabelPicker
          labels={labels}
          selected={settings.good_first_issue_labels}
          onToggle={(n) => toggleIn('good_first_issue_labels', n)}
          onAddMany={(ns) => addMany('good_first_issue_labels', ns)}
          countByLabel={countByLabel}
          suggestPattern={GFI_PATTERN}
          loading={labelsQuery.isLoading}
          error={labelsQuery.error as Error | null}
        />
        <StatLine>
          {issuesQuery.isLoading
            ? 'Counting open issues…'
            : <><strong className="text-text">{gfiCount}</strong> open issues are marked first-issue-friendly</>}
        </StatLine>
      </Card>

      <Card
        icon={Wand2}
        title="AI label recommendations"
        desc="Analyze this repo + its open issues and propose NEW labels to add — priority / area / type / triage / newcomer. Suggestions only; you choose what to create on GitHub."
      >
        <div className="flex items-center gap-3 flex-wrap mb-4">
          <button
            onClick={() => generateReco.mutate()}
            disabled={generateReco.isPending}
            className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-50 cursor-pointer"
          >
            <Wand2 size={13} className={generateReco.isPending ? 'animate-pulse' : ''} />
            {generateReco.isPending
              ? 'Analyzing issues…'
              : recommendations === null ? 'Recommend labels' : 'Regenerate'}
          </button>
          {recoQuery.data?.generated_at && !generateReco.isPending && (
            <span className="text-[12px] text-muted">Generated {relativeDate(recoQuery.data.generated_at)}</span>
          )}
          {!writable && (
            <span className="text-[12px] text-muted inline-flex items-center gap-1">
              <ReadOnlyTag /> creating labels needs write access
            </span>
          )}
        </div>

        {(generateReco.isError || recoQuery.isError) && (
          <div className="text-[12px] text-danger mb-3">
            {((generateReco.error ?? recoQuery.error) as Error)?.message}
          </div>
        )}

        {generateReco.isPending && (
          <div className="text-[12px] text-muted">
            Reading the repo's labels and a sample of open issues, then proposing a taxonomy — one model call, ~10–30s.
          </div>
        )}

        {!generateReco.isPending && recommendations === null && !recoQuery.isLoading && (
          <div className="text-[13px] text-muted">No recommendations yet — click “Recommend labels” to analyze this repo.</div>
        )}

        {!generateReco.isPending && recommendations !== null && visibleRecos.length === 0 && (
          <div className="text-[13px] text-muted">
            {asArray<LabelRecommendation>(recommendations).length === 0
              ? 'No new labels suggested — the repo’s taxonomy already covers what its open issues need.'
              : 'All suggestions dismissed.'}
          </div>
        )}

        <div className="flex flex-col gap-3">
          {visibleRecos.map((rec) => {
            const isCreated = createdLabels.has(rec.name)
            const isCreating = createLabel.isPending && createLabel.variables?.name === rec.name
            const failed = createLabel.isError && createLabel.variables?.name === rec.name
            const examples = asArray<number>(rec.examples)
            return (
              <div key={rec.name} className="rounded-lg border border-border p-3.5">
                <div className="flex items-start gap-2 flex-wrap">
                  <span
                    className="inline-flex items-center rounded-full px-2.5 py-0.5 text-[12px] font-semibold"
                    style={{ backgroundColor: `#${rec.color}`, color: readableText(rec.color) }}
                  >
                    {rec.name}
                  </span>
                  <span className="text-[10px] uppercase tracking-wide rounded px-1.5 py-0.5 bg-bg-hover text-muted self-center">
                    {rec.category}
                  </span>
                  <div className="ml-auto flex items-center gap-2">
                    {isCreated ? (
                      <span className="inline-flex items-center gap-1 text-[12px] text-accent"><Check size={13} /> Created</span>
                    ) : (
                      <button
                        onClick={() => createLabel.mutate(rec)}
                        disabled={!writable || isCreating}
                        title={writable ? 'Create this label on GitHub' : 'Read-only repo — needs triage/push access'}
                        className="inline-flex items-center gap-1 text-[12px] px-2.5 py-1 rounded-md border border-border text-text hover:bg-bg-hover disabled:opacity-40 cursor-pointer bg-transparent"
                      >
                        <Plus size={12} /> {isCreating ? 'Creating…' : 'Create on GitHub'}
                      </button>
                    )}
                    <button
                      onClick={() => setDismissedRecos((prev) => new Set(prev).add(rec.name))}
                      title="Dismiss this suggestion"
                      className="text-[12px] text-muted hover:text-text cursor-pointer bg-transparent px-1"
                    >
                      Dismiss
                    </button>
                  </div>
                </div>
                {rec.description && <div className="text-[13px] text-text mt-2">{rec.description}</div>}
                {rec.rationale && <div className="text-[12px] text-muted mt-1">{rec.rationale}</div>}
                {examples.length > 0 && (
                  <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                    <span className="text-[11px] text-muted">e.g.</span>
                    {examples.map((n) => (
                      <a
                        key={n}
                        href={`https://github.com/${owner}/${repo}/issues/${n}`}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[11px] px-1.5 py-0.5 rounded-full border border-border text-muted hover:text-text hover:border-border-strong"
                      >
                        #{n}
                      </a>
                    ))}
                  </div>
                )}
                {failed && (
                  <div className="text-[11px] text-danger mt-2">{(createLabel.error as Error).message}</div>
                )}
              </div>
            )
          })}
        </div>
      </Card>

      <Card
        icon={Users}
        title="Members"
        desc="Everyone with access to this repo, read from GitHub — each with their role. Shown for reference; read-only here."
      >
        {membersLoading ? (
          <div className="text-[12px] text-muted py-1">Loading members…</div>
        ) : (membersQuery.isError || issuesQuery.isError) ? (
          <div className="text-[13px] text-muted py-1">Couldn't load members right now.</div>
        ) : members.length === 0 ? (
          <div className="text-[13px] text-muted py-1">
            {memberSource === 'derived'
              ? `No members detected among ${repo}'s issues yet. Full roster access needs push permission on this repo.`
              : 'No members found for this repo.'}
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {members.map((m) => (
              <a
                key={m.login}
                href={`https://github.com/${m.login}`}
                target="_blank"
                rel="noreferrer"
                title={`${m.login} — ${ROLE_LABEL[m.role] ?? m.role} · open on GitHub`}
                className="inline-flex items-center gap-1.5 rounded-full border border-border bg-bg-hover pl-2.5 pr-2 py-1 text-[13px] text-text hover:border-border-strong transition-colors"
              >
                <span className="truncate max-w-[160px]">{m.login}</span>
                <MemberRoleTag role={m.role} />
              </a>
            ))}
          </div>
        )}
        <StatLine>
          {memberSource === 'derived' ? (
            <>
              Without push access to {owner}/{repo}, this is an approximate list inferred from issue
              authors. The full roster and member management live on{' '}
              <a
                href={`https://github.com/${owner}/${repo}/settings/access`}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline inline-flex items-center gap-0.5"
              >
                GitHub <ExternalLink size={11} />
              </a>.
            </>
          ) : (
            <>
              Membership is read from GitHub and can't be changed here — to add or remove a member or
              collaborator, manage access on{' '}
              <a
                href={`https://github.com/${owner}/${repo}/settings/access`}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline inline-flex items-center gap-0.5"
              >
                GitHub <ExternalLink size={11} />
              </a>. It refreshes here after the next sync.
            </>
          )}
        </StatLine>
      </Card>

      {/* Danger zone */}
      <div className="rounded-xl border border-danger/40 bg-danger/5 p-5 mt-8">
        <div className="flex items-center gap-2 mb-1 text-[13px] font-semibold text-danger">
          <AlertTriangle size={14} /> Disconnect repository
        </div>
        <p className="text-[12px] text-muted mb-3">
          Removes {owner}/{repo} from Issue Radar and deletes its local cache. Your GitHub data and <code>gh</code> auth are untouched — you can reconnect anytime.
        </p>
        {confirmingDelete ? (
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => disconnectMutation.mutate()}
              disabled={disconnectMutation.isPending}
              className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-md bg-danger text-white hover:opacity-90 disabled:opacity-40 cursor-pointer"
            >
              <Trash2 size={13} /> {disconnectMutation.isPending ? 'Disconnecting…' : 'Confirm disconnect'}
            </button>
            <button
              onClick={() => setConfirmingDelete(false)}
              className="text-[13px] px-3 py-1.5 rounded-md border border-border text-muted hover:text-text cursor-pointer bg-transparent"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirmingDelete(true)}
            className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-md border border-danger/50 text-danger hover:bg-danger/10 cursor-pointer bg-transparent"
          >
            <Trash2 size={13} /> Disconnect
          </button>
        )}
        {disconnectMutation.error && (
          <div className="text-[12px] text-danger mt-2">{(disconnectMutation.error as Error).message}</div>
        )}
      </div>
    </div>
  )
}

function Card({ icon: Icon, title, desc, children }: {
  icon: LucideIcon; title: string; desc: string; children: ReactNode
}) {
  return (
    <section className="rounded-xl border border-border bg-bg-elevated shadow-sm p-5 mb-6">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-8 h-8 rounded-lg bg-accent-subtle flex items-center justify-center flex-shrink-0">
          <Icon size={16} className="text-accent" />
        </div>
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold leading-tight">{title}</h2>
          <p className="text-[12px] text-muted mt-0.5">{desc}</p>
        </div>
      </div>
      {children}
    </section>
  )
}

function SettingToggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onClick}
      className="flex items-center gap-2.5 mb-4 cursor-pointer bg-transparent text-left"
    >
      <span className={`relative w-9 h-5 rounded-full flex-shrink-0 transition-colors ${on ? 'bg-accent' : 'bg-bg-hover border border-border'}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${on ? 'left-[18px]' : 'left-0.5'}`} />
      </span>
      <span className="text-[13px] text-text">{children}</span>
    </button>
  )
}

function StatLine({ children }: { children: ReactNode }) {
  return <div className="mt-4 pt-3 border-t border-border text-[12px] text-muted">{children}</div>
}

/** Small role tag (Admin / Maintainer / Read / …) shown after a member's login.
 * Maintainer-ish roles read as accent; read-only collaborators stay muted —
 * matches the detail pane's member badge. */
function MemberRoleTag({ role }: { role: string }) {
  const cls = ROLE_MUTED.has(role) ? 'bg-bg-elevated text-muted' : 'bg-accent-subtle text-accent'
  return (
    <span className={`text-[10.5px] px-1.5 py-0.5 rounded-full font-medium ${cls}`}>
      {ROLE_LABEL[role] ?? role}
    </span>
  )
}
