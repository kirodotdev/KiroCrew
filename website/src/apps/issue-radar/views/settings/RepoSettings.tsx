import { useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell, RefreshCw, ExternalLink, Trash2, AlertTriangle, Sparkles, ListChecks, Users, Wand2, Tags, Check, type LucideIcon,
} from 'lucide-react'
import { ProviderLogo, ProviderHostTag } from '../../components/ProviderBadge'
import {
  issueRadarApi, DEFAULT_REPO_SETTINGS, SettingsConflictError,
  type RepoSettings, type RepoLabel, type Issue, type RepoMember, type RepoRef,
} from '../../api'
import { repoWebUrl, userUrlFor, membersUrlFor, providerTerms, repoScopeKey } from '../../lib/links'
import { useIssueRadar } from '../../context'
import ReadOnlyTag, { isReadOnly } from '../../components/ReadOnlyTag'
import LabelPicker from '../../components/LabelPicker'
import { asArray } from '../../lib/format'

import { i18nT } from '../../../../i18n/t'
// Heuristic name patterns used only to *suggest* likely labels (one-click add);
// the user always confirms. Repos name these things a dozen different ways.
const TRIAGE_PATTERN = /(^|[\s:_/-])(triage|untriaged|unconfirmed|pending|needs?[\s_/-]?(triage|info|repro|reproduction|investigation|review|response|details?|decision))/i
const GFI_PATTERN = /(good[\s_/-]?first[\s_/-]?issue|first[\s_/-]?timers?|help[\s_/-]?wanted|beginner|newcomer|starter|low[\s_/-]?hanging|(^|[\s:_/-])easy([\s:_/-]|$))/i

/**
 * Catalog KEY for each of a member's repo roles (collaborators roster: admin/
 * maintain/write/triage/read) and, for the read-only derived fallback, the
 * author_association vocabulary (OWNER/MEMBER/COLLABORATOR).
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in `roleLabel()`, which runs during render. Flat `Record` of
 * full literal keys, indexed inline at the `i18nT()` call, because that is the
 * shape `scripts/check-i18n-keys.mjs` can resolve statically.
 */
const ROLE_LABEL_KEY: Record<string, string> = {
  admin: 'apps.issueRadar.views.settings.repoSettings.role_admin',
  maintain: 'apps.issueRadar.views.settings.repoSettings.role_maintainer',
  write: 'apps.issueRadar.views.settings.repoSettings.role_write',
  triage: 'apps.issueRadar.views.settings.repoSettings.role_triage',
  read: 'apps.issueRadar.views.settings.repoSettings.role_read',
  OWNER: 'apps.issueRadar.views.settings.repoSettings.role_owner',
  MEMBER: 'apps.issueRadar.views.settings.repoSettings.role_member',
  COLLABORATOR: 'apps.issueRadar.views.settings.repoSettings.role_collaborator',
  member: 'apps.issueRadar.views.settings.repoSettings.role_member',
}

/**
 * Localised label for a repo role. A role the provider reports that has no entry
 * above has no catalog entry either, so it is returned VERBATIM — it is a
 * provider identifier, not display copy.
 */
function roleLabel(role: string): string {
  // `hasOwnProperty`, not `in`: the role comes off an API response, so a provider
  // reporting `toString` would otherwise resolve to an inherited
  // Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(ROLE_LABEL_KEY, role)
    ? i18nT(ROLE_LABEL_KEY[role])
    : role
}

/** Roles that are collaborators but not maintainers — muted rather than accent. */
const ROLE_MUTED = new Set(['read'])

/** One repo's settings — full width. Local-only triage preferences that teach
 * Issue Radar how this repo labels its work (which labels mean "needs triage",
 * which mark newcomer-friendly issues), plus a per-repo data refresh and a
 * local disconnect. Nothing here is written back to GitHub. */
export default function RepoSettings({ repoRef }: { repoRef: RepoRef }) {
  const { owner, repo } = repoRef
  const scopeKey = repoScopeKey(repoRef)
  const terms = providerTerms(repoRef)
  const qc = useQueryClient()
  const { repos, active, openSettings, openDashboard, switchRepo } = useIssueRadar()
  // Full-identity match: the same slug can exist on two providers, and a loose
  // match would show the other repo's permissions and settings.
  const entry = repos.find(
    (r) => r.owner === owner
      && r.repo === repo
      && (r.provider || 'github') === (repoRef.provider || 'github')
      && (r.host || 'github.com') === (repoRef.host || 'github.com'),
  )

  const labelsQuery = useQuery({
    queryKey: ['issue-radar', 'labels', scopeKey],
    queryFn: () => issueRadarApi.labels(repoRef),
  })
  const settingsQuery = useQuery({
    queryKey: ['issue-radar', 'settings', scopeKey],
    queryFn: () => issueRadarApi.getSettings(repoRef),
  })
  const issuesQuery = useQuery({
    queryKey: ['issue-radar', 'issues', scopeKey, 'open'],
    queryFn: () => issueRadarApi.issues(repoRef, { state: 'open' }),
  })
  // Members are derived server-side from the cached issues, so wait until the
  // issues query has succeeded (by then the member cache is built) — same gate
  // as the shared context, to avoid a redundant fetch or an empty first read.
  const membersQuery = useQuery({
    queryKey: ['issue-radar', 'members', scopeKey],
    queryFn: () => issueRadarApi.members(repoRef),
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

  /** Saves are SERIALIZED and the newest draft always wins.
   *
   * Every toggle autosaves, so two quick clicks could otherwise send two writes
   * built on the same revision: the first succeeds, the second 409s, and clearing
   * the draft would throw away the newer edit — silently undoing the user's last
   * click.
   *
   * So: one save at a time through `saveChain`, each one sending the LATEST draft
   * with the LATEST known revision at send time. A 409 can then only come from
   * another tab, and it is recovered rather than discarded — the conflicting
   * server document becomes the new base, this tab's edit is re-applied on top,
   * and the write is retried once. */
  const saveChain = useRef<Promise<unknown>>(Promise.resolve())
  const latestDraft = useRef<RepoSettings | null>(null)
  const knownRevision = useRef<number | null>(null)
  /** The newest document the SERVER is known to hold. Every queued payload is
   * built from this plus the dirty keys, never from the whole local draft: after a
   * conflict rebase advanced the revision, sending the draft wholesale would
   * submit this tab's stale copy of the OTHER tab's fields under an accepted
   * revision, silently reverting them. */
  const serverSettings = useRef<RepoSettings | null>(null)
  /** Monotonic edit counter. A save carries the sequence it was queued at, so a
   * response that lands while a NEWER edit is already waiting does not overwrite
   * it — adopting unconditionally would let edit A's success replace pending draft
   * B, so B would re-send A and the user's latest change would vanish. */
  const editSeq = useRef(0)

  const applySaved = ({ res, seq }: { res: { settings: RepoSettings }; seq: number }) => {
    // The revision and the cache always advance: they describe the server, not
    // the user's in-flight intent.
    knownRevision.current = res.settings.revision
    serverSettings.current = res.settings
    qc.setQueryData(['issue-radar', 'settings', scopeKey], res)
    // Retire each dirty key the server now AGREES with, rather than clearing the
    // whole set on the last edit. Blanket-clearing loses edits in one direction and
    // never clearing in the other: if save A lands while B is queued, B fails,
    // another tab changes A's field and C conflicts, A stays dirty and the retry
    // would restore this tab's stale value over theirs.
    const current = latestDraft.current ?? res.settings
    for (const k of [...dirtyKeys.current]) {
      if (JSON.stringify(current[k]) === JSON.stringify(res.settings[k])) {
        dirtyKeys.current.delete(k)
      }
    }
    // The local view only follows when this really was the last edit.
    if (seq === editSeq.current) {
      setDraft(res.settings)
      latestDraft.current = res.settings
    }
  }

  /** The keys this tab actually CHANGED, relative to the value it started from.
   *
   * Re-sending the whole draft on a conflict would overwrite the other tab's
   * field with this tab's stale copy of it — trading one lost edit for another.
   * Only the changed keys are re-applied, so both survive. */
  const changedKeys = (base: RepoSettings, next: RepoSettings): (keyof RepoSettings)[] =>
    (Object.keys(next) as (keyof RepoSettings)[]).filter(
      (k) => k !== 'revision' && JSON.stringify(next[k]) !== JSON.stringify(base[k]),
    )

  /** Keys edited since the last SUCCESSFUL save.
   *
   * A single save's own diff is not enough: if save A fails and edit B then hits a
   * conflict, rebasing only B's keys drops A entirely — from the draft and from
   * what gets persisted. So dirty keys accumulate and are only cleared once a save
   * lands. */
  const dirtyKeys = useRef<Set<keyof RepoSettings>>(new Set())

  const saveMutation = useMutation({
    mutationFn: ({ next, base, seq }: { next: RepoSettings; base: RepoSettings; seq: number }) => {
      latestDraft.current = next
      for (const k of changedKeys(base, next)) dirtyKeys.current.add(k)
      /** This tab's dirty keys applied on top of a given base. The base is always
       * the newest document the server is known to hold, so fields this tab never
       * touched are carried through exactly as they are rather than from a stale
       * local copy. The cast is the narrow price of a keyed assignment across a
       * union of value types; `dirtyKeys` only ever holds real keys. */
      const payloadFrom = (base: RepoSettings): RepoSettings => {
        const out: RepoSettings = { ...base }
        const src = latestDraft.current ?? next
        for (const k of dirtyKeys.current) {
          (out as unknown as Record<string, unknown>)[k] =
            (src as unknown as Record<string, unknown>)[k]
        }
        return out
      }

      const run = saveChain.current.then(async () => {
        const base = serverSettings.current ?? next
        const revision = knownRevision.current ?? base.revision
        try {
          return {
            res: await issueRadarApi.putSettings(repoRef, { ...payloadFrom(base), revision }),
            seq,
          }
        } catch (e) {
          if (!(e instanceof SettingsConflictError)) throw e
          // Another tab moved these settings. Rebase onto theirs: their document
          // becomes the base, and only this tab's dirty keys go back on top.
          knownRevision.current = e.current.revision
          serverSettings.current = e.current
          return {
            res: await issueRadarApi.putSettings(repoRef, payloadFrom(e.current)),
            seq,
          }
        }
      })
      // The chain must survive a rejection, or every later save is skipped.
      saveChain.current = run.catch(() => undefined)
      return run
    },
    onSuccess: applySaved,
  })

  // `base` is the value this edit started from, so a conflict can be rebased by
  // re-applying only what changed.
  /** True only once the repo's real settings are in hand.
   *
   * Until then `settings` is DEFAULT_REPO_SETTINGS, whose revision is 0 — and a
   * pre-revision config on disk ALSO normalizes to 0, so a PUT built from the
   * defaults would be accepted as current and overwrite the saved label roles
   * with empty ones. Editing is therefore disabled rather than merely
   * discouraged: there is no revision value that makes the write safe. */
  const settingsReady = settingsQuery.isSuccess

  const commit = (next: RepoSettings) => {
    // Belt and braces: the controls are disabled, but a stray call must not write.
    if (!settingsReady) return
    const base = settings
    const seq = ++editSeq.current
    setDraft(next)
    saveMutation.mutate({ next, base, seq })
  }
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
        issueRadarApi.issues(repoRef, { refresh: true, state: 'open' }),
        issueRadarApi.labels(repoRef, { refresh: true }),
      ])
      return { iss, lab }
    },
    onSuccess: ({ iss, lab }) => {
      qc.setQueryData(['issue-radar', 'issues', scopeKey, 'open'], iss)
      qc.setQueryData(['issue-radar', 'labels', scopeKey], lab)
      // A fresh issues fetch rebuilds the member cache server-side; re-read it.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'members', scopeKey] })
    },
  })

  // ── disconnect (local-only) ──
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const disconnectMutation = useMutation({
    mutationFn: () => issueRadarApi.disconnect(repoRef),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['issue-radar', 'repos'] })
      // The connect dialog's picker caches a `connected` flag per repo, so
      // without this the just-disconnected repo stays greyed out as "Connected"
      // and un-tickable until that cache expires.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'recent-repos'] })
      openSettings({ kind: 'general', anchor: 'repos' })
    },
  })

  // ── AI label recommendations live on the Tagging dashboard ──
  // The taxonomy proposals ("what labels is this repo missing?") sit next to
  // the untagged issues they get applied to; this page keeps only the LOCAL
  // definitions above. See views/tagging/LabelsPanel.tsx.

  return (
    <div className="w-full max-w-6xl px-8 py-8">
      {/* Header */}
      <div className="flex items-center gap-3 mb-1 flex-wrap">
        <ProviderLogo repoRef={repoRef} size={20} />
        <h1 className="text-[22px] font-semibold">{owner}/{repo}</h1>
        {/* Only rendered for a self-managed instance, where it is the only thing
            distinguishing this project from a same-named one on the public site. */}
        <ProviderHostTag repoRef={repoRef} />
        {isReadOnly(entry?.permissions) && <ReadOnlyTag />}
        <a
          href={repoWebUrl(repoRef)}
          target="_blank"
          rel="noreferrer"
          className="text-[12px] text-muted hover:text-text inline-flex items-center gap-1"
        >
          <ExternalLink size={12} /> {i18nT('apps.issueRadar.views.settings.repoSettings.open_on')} {terms.providerName}
        </a>
        <button
          onClick={() => refreshMutation.mutate()}
          disabled={refreshMutation.isPending}
          title={i18nT('apps.issueRadar.views.settings.repoSettings.re_fetch_issues_and_labels_from', { provider: terms.providerName })}
          className="ml-auto inline-flex items-center gap-1.5 text-[12px] text-muted hover:text-text disabled:opacity-40 cursor-pointer"
        >
          <RefreshCw size={13} className={refreshMutation.isPending ? 'animate-spin' : ''} /> {i18nT('apps.issueRadar.views.settings.repoSettings.refresh')}
        </button>
      </div>
      {!settingsReady && !settingsQuery.isError && (
        <p className="text-[13px] text-muted mb-4">{i18nT('apps.issueRadar.views.settings.repoSettings.loading_this_repo_s_saved_settings')}</p>
      )}
      <p className="text-[13px] text-muted mb-4">
        {i18nT('apps.issueRadar.views.settings.repoSettings.local_triage_settings_for_this_repo_they_teach_i')} {repo} {i18nT('apps.issueRadar.views.settings.repoSettings.organises_its_issues_and_are_never_written_back')} {terms.providerName}.
        {saveMutation.isPending
          ? <span className="ml-2 opacity-70">{i18nT('apps.issueRadar.views.settings.repoSettings.saving')}</span>
          : saveMutation.isSuccess ? <span className="ml-2 opacity-70 inline-flex items-center gap-1">{i18nT('apps.issueRadar.views.settings.repoSettings.saved')} <Check size={12} className="lucide-inline" /></span> : null}
      </p>

      {(settingsQuery.isError || saveMutation.isError) && (
        <div className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 mb-6 text-[12px] text-danger">
          <div className="font-medium mb-0.5">
            {settingsQuery.isError ? i18nT('apps.issueRadar.views.settings.repoSettings.couldn_t_load_saved_settings') : i18nT('apps.issueRadar.views.settings.repoSettings.couldn_t_save_your_changes')}
          </div>
          <div className="opacity-80">
            {((settingsQuery.error ?? saveMutation.error) as Error)?.message}
            {' — '}{i18nT('apps.issueRadar.views.settings.repoSettings.your_edits_are_kept_here_but_won_t_persist_if_yo')}
          </div>
        </div>
      )}

      <Card
        icon={Bell}
        title={i18nT('apps.issueRadar.views.settings.repoSettings.notifications')}
        desc={i18nT('apps.issueRadar.views.settings.repoSettings.watch_this_repo_in_the_background_and_post_a_kir')}
      >
        <SettingToggle
          on={settings.notify_on_new_issue}
          disabled={!settingsReady}
          onClick={() => update({ notify_on_new_issue: !settings.notify_on_new_issue })}
        >
          {i18nT('apps.issueRadar.views.settings.repoSettings.notify_me_when_a')} <strong>{i18nT('apps.issueRadar.views.settings.repoSettings.new_issue')}</strong> {i18nT('apps.issueRadar.views.settings.repoSettings.is_opened_in')} {owner}/{repo}
        </SettingToggle>
        <StatLine>
          {i18nT('apps.issueRadar.views.settings.repoSettings.checks_about_once_a_minute_inside_kirocrew_no_cr')} <code>{terms.cli}</code> {i18nT('apps.issueRadar.views.settings.repoSettings.sign_in_no_extra_credentials_no_webhook', { provider: terms.providerName })}
        </StatLine>
      </Card>

      <Card
        icon={ListChecks}
        title={i18nT('apps.issueRadar.views.settings.repoSettings.triage_labels')}
        desc={i18nT('apps.issueRadar.views.settings.repoSettings.which_labels_mean_an_issue_still_needs_triage_dr')}
      >
        <SettingToggle
          on={settings.unlabeled_is_untriaged}
          disabled={!settingsReady}
          onClick={() => update({ unlabeled_is_untriaged: !settings.unlabeled_is_untriaged })}
        >
          {i18nT('apps.issueRadar.views.settings.repoSettings.also_treat_issues_with')} <strong>{i18nT('apps.issueRadar.views.settings.repoSettings.no_labels')}</strong> {i18nT('apps.issueRadar.views.settings.repoSettings.as_needing_triage')}
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
            ? i18nT('apps.issueRadar.views.settings.repoSettings.counting_open_issues')
            : <><strong className="text-text">{triageCount}</strong> {i18nT('apps.issueRadar.views.settings.repoSettings.of')} {openIssues.length} {i18nT('apps.issueRadar.views.settings.repoSettings.open_issues_currently_need_triage')}</>}
        </StatLine>
      </Card>

      <Card
        icon={Sparkles}
        title={i18nT('apps.issueRadar.views.settings.repoSettings.good_first_issue_labels')}
        desc={i18nT('apps.issueRadar.views.settings.repoSettings.which_labels_mark_newcomer_friendly_work_so_issu')}
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
            ? i18nT('apps.issueRadar.views.settings.repoSettings.counting_open_issues')
            : <><strong className="text-text">{gfiCount}</strong> {i18nT('apps.issueRadar.views.settings.repoSettings.open_issues_are_marked_first_issue_friendly')}</>}
        </StatLine>
      </Card>

      <Card
        icon={Wand2}
        title={i18nT('apps.issueRadar.views.settings.repoSettings.ai_label_recommendations')}
        desc={i18nT('apps.issueRadar.views.settings.repoSettings.proposing_new_labels_for_this_repo_now_lives_on')}
      >
        <button
          onClick={() => {
            // Switch first: the settings page can be open for a repo that is NOT
            // the active one, and navigating without this would show the Tagging
            // dashboard for a different repository than the page you came from.
            // Only when it actually differs, though — switchRepo resets the saved
            // issue and PR filters, which would be a surprising side effect of
            // navigating within the repo you are already on.
            const sameActive = active.owner === owner
              && active.repo === repo
              && (active.provider || 'github') === (repoRef.provider || 'github')
              && (active.host || 'github.com') === (repoRef.host || 'github.com')
            if (!sameActive) switchRepo(repoRef)
            openDashboard('tagging')
          }}
          className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover cursor-pointer bg-transparent"
        >
          <Tags size={13} /> {i18nT('apps.issueRadar.views.settings.repoSettings.open_tagging')}
        </button>
        <StatLine>
          {i18nT('apps.issueRadar.views.settings.repoSettings.recommending_a_taxonomy_and_applying_it_to_issue')} {repo}{i18nT('apps.issueRadar.views.settings.repoSettings.s_labels_mean_needs_triage_or_good_first_issue')}
        </StatLine>
      </Card>

      <Card
        icon={Users}
        title={i18nT('apps.issueRadar.views.settings.repoSettings.members')}
        desc={i18nT('apps.issueRadar.views.settings.repoSettings.members_read_from_provider', { provider: terms.providerName })}
      >
        {membersLoading ? (
          <div className="text-[12px] text-muted py-1">{i18nT('apps.issueRadar.views.settings.repoSettings.loading_members')}</div>
        ) : (membersQuery.isError || issuesQuery.isError) ? (
          <div className="text-[13px] text-muted py-1">{i18nT('apps.issueRadar.views.settings.repoSettings.couldn_t_load_members_right_now')}</div>
        ) : members.length === 0 ? (
          <div className="text-[13px] text-muted py-1">
            {memberSource === 'derived'
              ? i18nT('apps.issueRadar.views.settings.repoSettings.no_members_detected_among', { repo })
              : i18nT('apps.issueRadar.views.settings.repoSettings.no_members_found_for_this_repo')}
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {members.map((m) => (
              <a
                key={m.login}
                href={userUrlFor(repoRef, m.login)}
                target="_blank"
                rel="noreferrer"
                title={`${m.login} — ${roleLabel(m.role)} · open on ${terms.providerName}`}
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
              {i18nT('apps.issueRadar.views.settings.repoSettings.without_push_access_to')} {owner}/{repo}{i18nT('apps.issueRadar.views.settings.repoSettings.this_is_an_approximate_list_inferred_from_issue')}{' '}
              <a
                href={membersUrlFor(repoRef)}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline inline-flex items-center gap-0.5"
              >
                {terms.providerName} <ExternalLink size={11} />
              </a>.
            </>
          ) : (
            <>
              {i18nT('apps.issueRadar.views.settings.repoSettings.membership_is_read_from')} {terms.providerName} {i18nT('apps.issueRadar.views.settings.repoSettings.and_can_t_be_changed_here_to_add_or_remove_a_mem')}{' '}
              <a
                href={membersUrlFor(repoRef)}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline inline-flex items-center gap-0.5"
              >
                {terms.providerName} <ExternalLink size={11} />
              </a>{i18nT('apps.issueRadar.views.settings.repoSettings.it_refreshes_here_after_the_next_sync')}
            </>
          )}
        </StatLine>
      </Card>

      {/* Danger zone */}
      <div className="rounded-xl border border-danger/40 bg-danger/5 p-5 mt-8">
        <div className="flex items-center gap-2 mb-1 text-[13px] font-semibold text-danger">
          <AlertTriangle size={14} /> {i18nT('apps.issueRadar.views.settings.repoSettings.disconnect_repository')}
        </div>
        <p className="text-[12px] text-muted mb-3">
          {i18nT('apps.issueRadar.views.settings.repoSettings.removes')} {owner}/{repo} {i18nT('apps.issueRadar.views.settings.repoSettings.from_issue_radar_and_deletes_its_local_cache_you')} {terms.providerName} {i18nT('apps.issueRadar.views.settings.repoSettings.data_and')} <code>{terms.cli}</code> {i18nT('apps.issueRadar.views.settings.repoSettings.auth_are_untouched_you_can_reconnect_anytime')}
        </p>
        {confirmingDelete ? (
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => disconnectMutation.mutate()}
              disabled={disconnectMutation.isPending}
              className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-md bg-danger text-white hover:opacity-90 disabled:opacity-40 cursor-pointer"
            >
              <Trash2 size={13} /> {disconnectMutation.isPending ? i18nT('apps.issueRadar.views.settings.repoSettings.disconnecting') : i18nT('apps.issueRadar.views.settings.repoSettings.confirm_disconnect')}
            </button>
            <button
              onClick={() => setConfirmingDelete(false)}
              className="text-[13px] px-3 py-1.5 rounded-md border border-border text-muted hover:text-text cursor-pointer bg-transparent"
            >
              {i18nT('apps.issueRadar.views.settings.repoSettings.cancel')}
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirmingDelete(true)}
            className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-md border border-danger/50 text-danger hover:bg-danger/10 cursor-pointer bg-transparent"
          >
            <Trash2 size={13} /> {i18nT('apps.issueRadar.views.settings.repoSettings.disconnect')}
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

function SettingToggle({ on, onClick, disabled, children }: {
  on: boolean; onClick: () => void; disabled?: boolean; children: ReactNode
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onClick}
      disabled={disabled}
      className="flex items-center gap-2.5 mb-4 cursor-pointer bg-transparent text-left disabled:opacity-50 disabled:cursor-default"
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
      {roleLabel(role)}
    </span>
  )
}
