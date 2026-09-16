import { useState, useMemo, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient, useIsMutating, useMutationState } from '@tanstack/react-query'
import { Download, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { api, ApiError } from '../../api/client'
import ProjectSkillsTrustList from '../../components/ProjectSkillsTrustList'
import { Card, Btn, SearchInput, EmptyState, Toggle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import Modal from '../../components/Modal'
import SkillForm, { assembleSkillContent, parseSkillContent, skillPathProblem, skillPostPath, type SkillFormData } from '../../components/SkillForm'
import SkillDirectoryBrowser from '../../components/SkillDirectoryBrowser'
import SkillBrowserModal from '../../components/SkillBrowserModal'
import DiffBlock from '../../components/DiffBlock'
import ErrorNotice from '../../components/ErrorNotice'
import ListDetailBack from '../../components/ListDetailBack'
import { useListDetailView } from '../../hooks/useListDetailView'
import { useProvider } from '../../providers'
import type { Skill } from '../../types'
import SkillContextBudget from './SkillContextBudget'

import { Trans } from 'react-i18next'

import { fmtBytes, fmtCompact } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { parseErrorCode } from '../../utils/errorReport'
import { SettingRef } from '../../components/settingRef/SettingRef'
const EMPTY_FORM: SkillFormData = { name: '', category: '', description: '', triggers: '', tags: '', always: false, body: '' }

/**
 * The list-detail shell's height.
 *
 * `svh` (the viewport with browser chrome SHOWING) rather than `vh`: `vh`
 * resolves against the large viewport, so on a phone the pane runs under the
 * address bar and its bottom edge — which while narrow holds the only visible
 * pane — is unreachable. `svh` also does not re-resolve as the URL bar
 * animates, unlike `dvh`. Identical to `vh` on a desktop, where there is no
 * dynamic chrome. The `vh` declaration stays as the fallback for browsers
 * without `svh`, matching the shell's own `supports-[height:100dvh]` pattern.
 */
const PANE_SHELL_CLASS = 'flex gap-3 -mx-2 md:mx-0 h-[calc(100vh-260px)] supports-[height:100svh]:h-[calc(100svh-260px)] min-h-[420px]'

/** Humanize a kebab/snake-case skill name for display. */
const displayName = (s: Skill) => s.name.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())

/** A skill only carries injection cost when a trigger can fire it, so the
 *  control is meaningless for a pinned (`always: true`) skill — the matcher
 *  skips those entirely — and for sources the dashboard cannot write.
 *
 *  `owned === false` is the backend's own write predicate: a skill reached
 *  through `skills.extra_paths` still reports `source: 'kirocrew'`, but
 *  `set_inject_on_trigger` refuses to rewrite it. Gating on the reported
 *  writability, not on source alone, is what keeps the UI from offering a
 *  toggle that always fails. */
const canControlInjection = (s: Skill) =>
  s.source === 'kirocrew' && !s.always && s.owned !== false

/** Short, human label for a skill's provenance — drives the source badge. */
function sourceLabel(source: Skill['source']): string | null {
  switch (source) {
    case 'package': return i18nT('pages.overview.skillsTab.package')
    case 'kiro-user': return '~/.kiro/skills'
    case 'kiro-workspace': return i18nT('pages.overview.skillsTab.workspace')
    default: return null  // kirocrew — the default home, no badge needed
  }
}

export default function SkillsTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [creating, setCreating] = useState(false)
  const [formData, setFormData] = useState<SkillFormData>(EMPTY_FORM)
  const [skillFilter, setSkillFilter] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [detailEditing, setDetailEditing] = useState(false)
  // Multi-provider skill browser drawer (Add Skill button).
  const [skillBrowserOpen, setSkillBrowserOpen] = useState(false)
  const [createError, setCreateError] = useState('')

  // Deep-linkable view param: ?view=budget swaps to the control plane.
  // Entering the budget view PUSHES a history entry so browser Back returns to
  // Skills; leaving via the in-app affordance replaces (pops back cleanly).
  const viewBudget = searchParams.get('view') === 'budget'
  const showBudget = () => setSearchParams(prev => { const next = new URLSearchParams(prev); next.set('view', 'budget'); return next })
  const hideBudget = () => setSearchParams(prev => { const next = new URLSearchParams(prev); next.delete('view'); return next }, { replace: true })

  // Light prefetch removed: the Design reviewer correctly noted that firing the
  // budget endpoint on every Skills-tab mount contradicts the PR's own
  // justification that Context Budget is a deliberate, user-initiated path.
  // The doorway label is now static; the data is fetched when the user opens it.

  const { data: skills = [], isLoading, isFetching, refetch } = useQuery<Skill[]>({
    queryKey: ['skills'],
    queryFn: () => api.skills(),
    // Fetch fresh on each mount so an approved/edited skill is reflected the
    // moment the tab opens (the 30s global staleTime otherwise serves a cached
    // list). The shared ['skills'] cache still backs the palette/picker.
    staleTime: 0,
    refetchOnMount: 'always',
  })

  // Content of the selected skill's SKILL.md — only needed to seed the edit
  // form.  The directory browser fetches its own copy for display.
  const { data: skillDetail } = useQuery({
    queryKey: ['skill-detail', selectedKey],
    queryFn: () => api.skill(selectedKey!).then(d => d.content || ''),
    enabled: !!selectedKey,
  })
  const detailContent = skillDetail ?? ''
  const detailReady = skillDetail !== undefined

  const createSkill = useMutation({
    mutationFn: ({ name, content }: { name: string; content: string }) => api.createSkill(name, content),
    onSuccess: () => {
      setFormData(EMPTY_FORM)
      setCreating(false)
      setCreateError('')
      queryClient.invalidateQueries({ queryKey: ['skills'] })
    },
    // The form's sanitizeSkillName mirror gates most bad names before they leave
    // the browser, but it is a mirror rather than the authority: a name the
    // preview accepted and the server did not still lands here. `invalid_name`
    // is the empty-sanitize refusal a non-Latin name earns, and it is the one
    // whose English prose the user seeing it is least able to read, so it gets a
    // translated hint; every other code's server prose is already actionable.
    onError: (e: Error) => {
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      setCreateError(code === 'invalid_name'
        ? i18nT('components.skillForm.invalid_name_hint')
        : e.message)
    },
  })

  const updateSkill = useMutation({
    mutationFn: ({ key, content }: { key: string; content: string }) => api.updateSkill(key, content),
    onSuccess: () => {
      setDetailEditing(false)
      queryClient.invalidateQueries({ queryKey: ['skills'] })
      queryClient.invalidateQueries({ queryKey: ['skill-detail'] })
    },
  })

  const deleteSkill = useMutation({
    mutationFn: (key: string) => api.deleteSkill(key),
    onMutate: async (key) => {
      await queryClient.cancelQueries({ queryKey: ['skills'] })
      const prev = queryClient.getQueryData<Skill[]>(['skills'])
      queryClient.setQueryData<Skill[]>(['skills'], old => old?.filter(s => s.key !== key) ?? [])
      return { prev }
    },
    onSuccess: () => {
      setSelectedKey(null)
      setDetailEditing(false)
      // Discover results carry an installed flag derived from the skills
      // dir -- drop them so the Add Skill browser reflects the deletion.
      queryClient.invalidateQueries({ queryKey: ['discover-skills'] })
    },
    onError: (_err, _key, context) => {
      if (context?.prev) queryClient.setQueryData(['skills'], context.prev)
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['skills'] })
    },
  })

  // Two groups: skills KiroCrew can edit (kirocrew + kiro-cli's own dirs) and
  // read-only AIM-package skills.  The text filter is applied to both.
  const { localSkills, packageSkills } = useMemo(() => {
    const q = skillFilter.toLowerCase()
    const match = (s: Skill) => !q || (s.name + ' ' + s.key + ' ' + (s.description || '')).toLowerCase().includes(q)
    return {
      localSkills: skills.filter(s => s.source !== 'package').filter(match),
      packageSkills: skills.filter(s => s.source === 'package').filter(match),
    }
  }, [skills, skillFilter])

  const allFiltered = useMemo(() => [...localSkills, ...packageSkills], [localSkills, packageSkills])
  const selectedSkill = useMemo(() => skills.find(s => s.key === selectedKey) ?? null, [skills, selectedKey])

  // Narrow viewport shows one pane at a time; a desktop shows both.
  const { isMobile, showList, showDetail, openDetail, closeDetail } = useListDetailView()

  // Keep a valid selection: default to the first skill, and recover if the
  // current selection is filtered out or deleted.  Suspended while editing:
  // selectedSkill is derived from the *unfiltered* skills array, so the
  // editor stays mounted even if the skill is filtered out of the list —
  // auto-reselecting here would silently discard unsaved form changes.
  useEffect(() => {
    if (detailEditing) return
    if (allFiltered.length === 0) { if (selectedKey !== null) setSelectedKey(null); return }
    if (!selectedKey || !allFiltered.some(s => s.key === selectedKey)) {
      setSelectedKey(allFiltered[0].key)
    }
  }, [allFiltered, selectedKey, detailEditing])

  const selectSkill = (s: Skill) => { setSelectedKey(s.key); setDetailEditing(false); openDetail() }

  /** One row in the left list. */
  const renderRow = (s: Skill) => {
    const isSel = s.key === selectedKey
    return (
      <div
        key={s.key}
        role="button"
        tabIndex={0}
        aria-current={isSel ? 'true' : undefined}
        aria-label={i18nT('pages.overview.skillsTab.select', { name: displayName(s) })}
        onClick={() => selectSkill(s)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectSkill(s) } }}
        className={`flex flex-col gap-0.5 px-3 py-2.5 rounded-md cursor-pointer mb-1 transition-colors ${
          isSel ? 'list-selected bg-accent-subtle' : 'bg-bg-elevated hover:bg-bg-hover'
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[13px] font-semibold text-text truncate flex-1">{displayName(s)}</span>
          {s.source === 'package'
            ? <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-aim-subtle text-aim border border-aim/30 font-bold shrink-0">{i18nT('pages.overview.skillsTab.package')}</span>
            : s.always
              ? <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-ok-subtle text-ok font-bold shrink-0">{i18nT('pages.overview.skillsTab.auto')}</span>
              : s.inject_on_trigger === false
                ? <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-accent-subtle text-accent border border-accent/30 font-bold shrink-0">{i18nT('pages.overview.skillsTab.pointer')}</span>
                : <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-bg-elevated text-muted border border-border font-bold shrink-0">{i18nT('pages.overview.skillsTab.on_demand')}</span>}
        </div>
        <div className="text-[11px] text-muted font-mono truncate">{s.key}</div>
        {s.loaded_by_agents && s.loaded_by_agents.length > 0 && (
          <div className="text-[10px] text-muted/70 truncate" title={i18nT('pages.overview.skillsTab.loaded_by_2', { agents: s.loaded_by_agents.join(', ') })}>
            {i18nT('pages.overview.skillsTab.loaded_by')} {i18nT('pages.overview.skillsTab.agent', { count: s.loaded_by_agents.length })}
          </div>
        )}
      </div>
    )
  }

  if (isLoading) return (<>
    <h4 className="text-sm font-semibold text-text-strong mb-2 flex items-center gap-2">{i18nT('pages.overview.skillsTab.skills')} <InfoTip text={i18nT('pages.overview.skillsTab.on_demand_skills_loaded_when_the_agent_determine')} /> <Btn primary disabled>{i18nT('pages.overview.skillsTab.create_new_skill')}</Btn></h4>
    <Card>
      <div className="flex items-center gap-2 mb-3"><div className="h-8 max-w-[480px] flex-1 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5 }} /></div>
      <div className={PANE_SHELL_CLASS}>
        <div className="w-[240px] shrink-0 space-y-1">{Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-[58px] rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5, animationDelay: `${i * 80}ms` }} />
        ))}</div>
        <div className="flex-1 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.3 }} />
      </div>
    </Card>
  </>)

  // Control plane: full-page budget view, deep-linkable via ?view=budget.
  if (viewBudget) return <SkillContextBudget onBack={hideBudget} />

  // One predicate for the Create button's `disabled` and its onClick guard, so
  // the two can never disagree — a keyboard activation that races the disabled
  // attribute would otherwise send a request the button was already refusing.
  // `.trim()` is what makes a whitespace-only name a non-submission rather than a
  // truthy string that earns an untranslated `name is required` 400.
  const createCanSubmit = formData.name.trim() !== ''
    && skillPathProblem(formData.name, formData.category) === null
    && !createSkill.isPending

  return (<>
    <PendingSkillsPanel />
    <ProjectSkillsTrustList />
    {/* Create Skill Modal */}
    {/* The gate reads the SANITIZED name and category, not the raw ones and not
        the combined path: a segment that sanitizes to nothing (typically one
        written entirely in a non-Latin script) would otherwise pass
        `!formData.name`, or hide behind a surviving sibling segment, and reach the
        server only to be silently renamed or refused with an English 400. A name
        that is nothing BUT separators (`/`) is the sharpest case, because the
        surviving sibling is the category and the skill lands under it with the
        name discarded — hence skillPathProblem, not a bare emptiness test.
        `isPending` closes the same window a second time over, since an in-flight
        create must not be re-sent or abandoned mid-request. */}
    <Modal open={creating} onClose={() => { if (createSkill.isPending) return; setCreating(false) }} title={i18nT('pages.overview.skillsTab.create_new_skill')} maxWidth={560} footer={<>
      <Btn disabled={createSkill.isPending} onClick={() => setCreating(false)}>{i18nT('pages.overview.skillsTab.cancel')}</Btn>
      <Btn primary onClick={() => { if (!createCanSubmit) return; createSkill.mutate({ name: skillPostPath(formData.name, formData.category), content: assembleSkillContent(formData) }) }} disabled={!createCanSubmit}>{i18nT('pages.overview.skillsTab.create')}</Btn>
    </>}>
      <SkillForm data={formData} onChange={setFormData} />
      {createError && <p className="text-danger text-[12px] mt-2">{createError}</p>}
    </Modal>

    {/* No top margin: the pane that hosts this tab owns the gap under the tab
      * strip (SidePanelLayout's narrow `pt-3`, the desktop header's `pb-3`).
      * A margin here would stack on top of it and put this tab further from the
      * divider than the tabs whose first element is a Card. Dropped outright
      * rather than with `first:mt-0`, because `PendingSkillsPanel` above returns
      * null when nothing is pending — this heading moves in and out of
      * `:first-child` with the pending count, so a positional rule would make
      * the gap depend on it. */}
    <h4 className="text-sm font-semibold text-text-strong mb-2 flex flex-wrap items-center gap-2">{i18nT('pages.overview.skillsTab.skills_count', { count: skills.length })} <InfoTip text={i18nT('pages.overview.skillsTab.skills_tip')} /> <span className="w-full md:w-auto md:ml-auto flex flex-col md:flex-row items-stretch md:items-center [&>button]:justify-center md:[&>button]:justify-start gap-2"><Btn onClick={showBudget} className="text-accent border-accent/30 bg-accent/5 hover:bg-accent/10">{i18nT('pages.overview.skillsTab.budget_doorway_static')}</Btn><Btn onClick={() => setSkillBrowserOpen(true)}><Download size={14} /> {i18nT('pages.overview.skillsTab.add_skill')}</Btn><Btn primary onClick={() => { setFormData(EMPTY_FORM); setCreateError(''); setCreating(true) }}>{i18nT('pages.overview.skillsTab.create_new_skill')}</Btn></span></h4>
    <p className="text-[12px] text-muted mb-2"><Trans i18nKey="pages.overview.skillsTab.auto_create_hint" components={{ settingRef: <SettingRef configKey="skills.auto_create_from_sessions" /> }} /></p>
    <Card>
      <div className="flex items-center gap-2 mb-3">
        <div className="relative max-w-[480px] flex-1">
          <SearchInput placeholder={i18nT('pages.overview.skillsTab.filter_skills')} value={skillFilter} onChange={e => setSkillFilter(e.target.value)} />
          {skillFilter && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer" onClick={() => setSkillFilter('')} aria-label={i18nT('pages.overview.skillsTab.clear_search')}>{"\u00d7"}</button>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Btn onClick={() => refetch()} disabled={isFetching} aria-label={i18nT('pages.overview.skillsTab.refresh_skills')}><RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} /></Btn>
        </div>
      </div>

      {skills.length === 0 ? <EmptyState icon={<Sparkles className="lucide-inline" />} title={i18nT('pages.overview.skillsTab.no_skills_yet')} subtitle={i18nT('pages.overview.skillsTab.empty_subtitle')} action={<Btn onClick={() => setSkillBrowserOpen(true)}><Download size={14} /> {i18nT('pages.overview.skillsTab.add_skill')}</Btn>} /> : (
        /* List-detail: skill list (pane 1) on the left, then the directory
         *  browser (panes 2+3: file tree + file content) on the right. */
        <div className={PANE_SHELL_CLASS}>
          {/* Pane 1 — skill list.  ``scrollbar-overlay`` keeps the scrollbar
           *  hidden until hover and overlays it so the row width never shifts
           *  between scrollable and non-scrollable states. */}
          {showList && <div className={`${isMobile ? 'w-full' : 'w-[240px]'} shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md p-2`} role="listbox" aria-label={i18nT('pages.overview.skillsTab.skills')}>
            {localSkills.map(renderRow)}
            {packageSkills.length > 0 && (
              <div className="mt-2">
                <div className="text-[11px] text-aim font-semibold tracking-wider px-2 py-1.5 mb-1" title={i18nT('pages.overview.skillsTab.skills_from_read_only', { name: provider.labels.pluginRegistryName })}>
                  {provider.labels.pluginRegistryName.toUpperCase()}
                </div>
                {packageSkills.map(renderRow)}
              </div>
            )}
            {allFiltered.length === 0 && <div className="text-muted/70 text-[12px] italic px-2 py-2">{i18nT('pages.overview.skillsTab.no_skills_match_query', { query: skillFilter })}</div>}
          </div>}

          {/* Panes 2+3 — directory browser, or the edit form */}
          {showDetail && <div className="flex-1 min-w-0 flex flex-col border border-border rounded-md bg-card overflow-hidden">
            {!selectedSkill ? (
              <div className="flex items-center justify-center h-full text-muted text-[13px]">{i18nT('pages.overview.skillsTab.select_a_skill_to_view_its_files')}</div>
            ) : detailEditing ? (
              <div className="flex flex-col h-full min-h-0">
                {/* Back gets its own full-width row rather than joining the
                    action row: with Cancel and Save already there, adding a
                    third control to one row trips AUTOSDE's
                    max-two-buttons-per-row. A row that already carries three is
                    tolerated; a compliant one may not grow into that. */}
                {isMobile && (
                  <div className="px-4 pt-2.5 shrink-0">
                    <ListDetailBack label={i18nT('pages.overview.skillsTab.skills')} onBack={closeDetail} />
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-2.5 border-b border-border shrink-0">
                  <span className="text-sm font-mono font-bold text-text-strong truncate">{selectedSkill.key}</span>
                  <div className="flex gap-2 shrink-0">
                    <Btn onClick={() => setDetailEditing(false)}>{i18nT('pages.overview.skillsTab.cancel')}</Btn>
                    <Btn primary onClick={() => updateSkill.mutate({ key: selectedSkill.key, content: assembleSkillContent(formData) })}>{i18nT('pages.overview.skillsTab.save')}</Btn>
                  </div>
                </div>
                <div className="flex-1 min-h-0 overflow-y-auto p-4">
                  <SkillForm data={formData} onChange={setFormData} hideIdentity />
                </div>
              </div>
            ) : (
              <div className="flex flex-col h-full min-h-0">
                {/* Detail header: name, source badge, Edit/Delete (kirocrew only) */}
                {/* Own row, same reason as the edit header: Edit and Delete
                    already fill this row's two-control budget. */}
                {isMobile && (
                  <div className="px-4 pt-2.5 shrink-0">
                    <ListDetailBack label={i18nT('pages.overview.skillsTab.skills')} onBack={closeDetail} />
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-2.5 border-b border-border shrink-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-bold text-text-strong truncate">{displayName(selectedSkill)}</span>
                    {sourceLabel(selectedSkill.source) && (
                      <span className={`text-[11px] px-1.5 py-[1px] rounded-full font-bold shrink-0 ${selectedSkill.source === 'package' ? 'bg-aim-subtle text-aim border border-aim/30' : 'bg-bg-elevated text-muted border border-border'}`}>{sourceLabel(selectedSkill.source)}</span>
                    )}
                  </div>
                  {selectedSkill.source === 'kirocrew' && (
                    <div className="flex gap-2 shrink-0">
                      <Btn disabled={!detailReady} onClick={() => { setDetailEditing(true); setFormData(parseSkillContent(detailContent, selectedSkill.key)) }}>{i18nT('pages.overview.skillsTab.edit')}</Btn>
                      <Btn danger onClick={() => { if (confirm(i18nT('pages.overview.skillsTab.delete_confirm', { name: selectedSkill.key }))) deleteSkill.mutate(selectedSkill.key) }}>{i18nT('pages.overview.skillsTab.delete')}</Btn>
                    </div>
                  )}
                </div>
                <InjectionRow skill={selectedSkill} />
                <div className="flex-1 min-h-0 p-3">
                  <SkillDirectoryBrowser key={selectedSkill.key} skillKey={selectedSkill.key} skill={selectedSkill} />
                </div>
              </div>
            )}
          </div>}
        </div>
      )}
    </Card>

    {/* Multi-provider Skill Browser Modal */}
    <SkillBrowserModal open={skillBrowserOpen} onClose={() => setSkillBrowserOpen(false)} />
  </>)
}


/** The full-content-vs-pointer control for one skill, with the cost that makes
 *  the choice informed.
 *
 *  Applies immediately on flip and refetches, matching the poolable-MCP-server
 *  row rather than the surrounding Edit/Save flow: it is a single boolean whose
 *  new state is visible at once and whose undo is one more click.
 *
 *  Rendered only for a skill the matcher can actually fire and the dashboard can
 *  write — see `canControlInjection`. */
function InjectionRow({ skill }: { skill: Skill }) {
  const qc = useQueryClient()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!canControlInjection(skill)) return null

  const inject = skill.inject_on_trigger !== false
  const size = skill.size_bytes ?? 0
  const deliveries = skill.deliveries ?? null
  const spent = deliveries !== null && size ? deliveries * size : null

  const flip = async (next: boolean) => {
    setError(null)
    setPending(true)
    try {
      await api.setSkillInjectOnTrigger(skill.key, next)
    } catch {
      setError(i18nT('pages.overview.skillsTab.injection_update_failed'))
      setPending(false)
      return
    }
    // Await the refetch before clearing pending: invalidateQueries resolves once
    // the active query has refetched, and releasing the control earlier would
    // briefly render the stale value as interactive.
    await qc.invalidateQueries({ queryKey: ['skills'] })
    setPending(false)
  }

  return (
    <div className="px-4 py-2.5 border-b border-border shrink-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[13px] text-text">
            {i18nT('pages.overview.skillsTab.inject_full_content_on_match')}
          </div>
          <div className="text-[11px] text-muted mt-0.5">
            {inject
              ? i18nT('pages.overview.skillsTab.injection_on_help')
              : i18nT('pages.overview.skillsTab.injection_off_help')}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {pending && <Loader2 size={14} className="animate-spin text-accent" />}
          <Toggle
            checked={inject}
            onChange={flip}
            disabled={pending}
            label={i18nT('pages.overview.skillsTab.inject_full_content_on_match')}
          />
        </div>
      </div>
      <div className="mt-2 text-[11px] text-muted font-mono">
        {deliveries === null
          ? i18nT('pages.overview.skillsTab.size_no_deliveries', { size: fmtBytes(size) })
          : i18nT(
              inject
                ? 'pages.overview.skillsTab.cost_line'
                : 'pages.overview.skillsTab.cost_line_frozen',
              {
                size: fmtBytes(size),
                deliveries: String(deliveries),
                chars: fmtCompact(spent ?? 0),
              },
            )}
      </div>
      {error && <div className="text-[11px] text-danger mt-1.5">{error}</div>}
    </div>
  )
}

/** Pending review queue for auto-generated skill candidates. *  Self-contained: its own query + approve/dismiss mutations, so it can be
 *  dropped into the Skills tab without touching the main list logic. Renders
 *  nothing when the queue is empty. Each row can be expanded to review the
 *  full SKILL.md body and any bundled script contents BEFORE approving. */
interface PendingSkill {
  slug: string
  name: string
  description: string
  has_scripts: boolean
  /** 'new' (default) or 'update' — an update proposal against a live skill. */
  kind?: string
  /** For updates: the live skill this proposes to change (e.g. 'auto/deploy'). */
  target?: string | null
  base_version?: number | null
  /**
   * When this candidate was staged. The queue has always returned it
   * (`skills.py` `list_pending_skills`); it is declared here because it is the
   * only thing that tells two GENERATIONS of one slug apart.
   *
   * A slug is reusable: the backend stages a distinct slug while one is in use,
   * but once a candidate is approved or dismissed its slug is free, and a later
   * candidate takes it with a fresh timestamp. Without the generation, that
   * successor inherits its predecessor's row instance (so it renders already
   * expanded) and its predecessor's cached detail (so the panel shows the OLD
   * body while Approve promotes the NEW one, scripts and all) -- approving
   * something nobody reviewed, which is the defect this whole surface exists to
   * prevent.
   */
  created_at?: string
}
interface PendingDetail {
  name: string
  content: string
  scripts: { filename: string; content: string }[]
  /** Update-only approval preview (server-computed; null if target is gone). */
  diff?: string | null
  live_body?: string | null
  proposed_body?: string | null
  from_version?: number | null
  to_version?: number | null
  /** True when the live skill advanced past the version this was merged from. */
  stale_base?: boolean
}

function PendingCandidateRow({ p, autoOpen, busy, busySelf, justRefused, onApprove, onDismiss }: {
  p: PendingSkill
  /** True when a notification deep-linked at THIS candidate (?review=<slug>). */
  autoOpen?: boolean
  /** An action is in flight SOMEWHERE in the queue — see the panel's comment. */
  busy?: boolean
  /** …and it is this row's own action, so this row shows the progress. */
  busySelf?: boolean
  /** True while this row's Approve must be withheld: either the queue read is
   *  failing (nothing on screen is confirmed) or the server refused this
   *  candidate and no successful read has reconciled the list since. */
  justRefused?: boolean
  onApprove: (slug: string) => void
  onDismiss: (slug: string) => void
}) {
  const [open, setOpen] = useState(false)
  const rowRef = useRef<HTMLDivElement>(null)
  // Deliberately an effect and not a `useState(autoOpen)` initializer: the panel
  // latches the deep-linked slug in an effect of its own, so `autoOpen` can flip
  // to true on a re-render AFTER this row already mounted (the queue renders
  // from cache before that latch lands). An initializer would have run once,
  // with the wrong value, and the deep link would open nothing. Depending only
  // on `autoOpen` -- which only ever goes true then false (the panel clears the
  // latch when the user approves or dismisses this row), and whose false pass is
  // a no-op thanks to the early return -- also means a user who collapses the
  // row is not fought by a re-opening effect.
  useEffect(() => {
    if (!autoOpen) return
    setOpen(true)
    rowRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [autoOpen])
  const isUpdate = p.kind === 'update'
  const { data: detail, error: detailError } = useQuery<PendingDetail>({
    // The generation is part of the key, so a successor under the same slug can
    // never be served its predecessor's body -- on any path, including the 30s
    // staleTime window and the deep link. The panel's `removeQueries` /
    // `invalidateQueries` calls match on the `['skills-pending-detail', slug]`
    // PREFIX, so they still reach every generation of a slug.
    queryKey: ['skills-pending-detail', p.slug, p.created_at ?? ''],
    queryFn: () => api.skillPendingDetail(p.slug),
    enabled: open,
  })
  // Why this candidate cannot be approved, as ONE value rather than a condition
  // on the button and a notice somewhere else in the panel. The backend refuses
  // both cases, so the same expression has to decide the disabled state and the
  // sentence that explains it — computing them separately is how the two drift
  // apart and a disabled button loses its caption.
  const refusal = !isUpdate || !detail
    ? null
    : !detail.diff
      ? i18nT('pages.overview.skillsTab.the_skill_this_update_targets_no_longer_exists_s')
      : detail.stale_base
        ? i18nT('pages.overview.skillsTab.this_skill_changed_after_this_update_was_written')
        : null
  return (
    <div ref={rowRef} className={`p-2 rounded-md border ${autoOpen ? 'border-accent ring-1 ring-accent' : 'border-border'}`}>
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-text-strong truncate">
            {p.name}
            {isUpdate && (
              <span className="ml-2 text-[10px] px-1.5 py-[1px] rounded-full bg-accent-subtle text-accent font-bold">{i18nT('pages.overview.skillsTab.update')}</span>
            )}
            {p.has_scripts && (
              /* Plain badge: the always-requires-review explanation renders as
                 visible text in the expanded panel (and the panel hint carries
                 the same caveat), so a hover title here would be a third
                 rendering of one sentence — and a tooltip BUTTON would be a
                 fourth control in the row (AUTOSDE max-two-buttons-per-row). */
              <span className="ml-2 text-[10px] px-1.5 py-[1px] rounded-full bg-warn-subtle text-warn font-bold">{i18nT('pages.overview.skillsTab.script')}</span>
            )}
          </div>
          <div className="text-[12px] text-muted truncate">
            {isUpdate && p.target
              ? i18nT('pages.overview.skillsTab.adds_new_requirements_to', { target: p.target, description: p.description })
              : p.description}
          </div>
        </div>
        {/* Review is the row's PRIMARY action while collapsed, and Approve is not
            rendered at all until the panel is open. The previous layout showed a
            greyed-out Approve beside Review on every collapsed row, which reads as
            a broken button rather than as a rule: approval is gated on having seen
            the content, and nothing said so. Moving Approve into the panel makes
            the gate the shape of the UI instead of a disabled state needing a
            caption, and keeps the row at the two-buttons-per-row maximum. */}
        <Btn primary={!open} onClick={() => setOpen(o => !o)}>{open ? i18nT('pages.overview.skillsTab.hide') : i18nT('pages.overview.skillsTab.review')}</Btn>
        {busySelf && <Loader2 size={14} className="animate-spin text-accent" aria-hidden="true" data-testid="pending-action-spinner" />}
        {/* Pushed away from Review/Hide: one collapses a row, the other discards
            a candidate, and at equal weight and adjacency a reader hesitates over
            which is which. */}
        <Btn danger className="ml-3" disabled={busy} onClick={() => { if (confirm(i18nT('pages.overview.skillsTab.dismiss_confirm', { name: p.name }))) onDismiss(p.slug) }}>{i18nT('pages.overview.skillsTab.dismiss')}</Btn>
      </div>
      {/* The body could not be READ. Opening a row whose detail fetch fails used
          to render an empty panel — no content, no Approve, no reason — and this
          change made that worse, because Approve moving inside `open && detail`
          means a failed read now shows nothing at all where a disabled button at
          least used to be. The server's own message carries the reason, so no new
          copy is needed to say it.

          Keyed on the ERROR rather than on missing data, because a failed REFETCH
          keeps the previous body in cache: `!detail` is false there, so the
          failure would be silent in exactly the case that matters. While the read
          is failing the body is withheld along with Approve — offering to approve
          content the client could not re-read is the same bargain this surface
          exists to refuse. */}
      {open && detailError && (
        <ErrorNotice
          message={detailError.message}
          askAgent
          className="mt-2"
          testId={`pending-detail-error-${p.slug}`}
        />
      )}
      {open && detail && !detailError && (
        <div className="mt-2 space-y-2">
          {p.has_scripts && (
            /* Scripts are a hard security boundary: a script-bearing candidate
               stages for manual review even with skills.approval_required off.
               Without this note a user who disabled approval sees the row and
               has no idea why the setting "didn't work". */
            <div className="text-[11px] p-2 rounded bg-warn-subtle text-warn border border-border">
              {i18nT('pages.overview.skillsTab.scripts_always_require_review')}
            </div>
          )}
          {isUpdate && detail.diff ? (
            <>
              <div className="text-[11px] font-semibold text-muted">
                {i18nT('pages.overview.skillsTab.proposed_change')}{detail.from_version != null && detail.to_version != null
                  ? ` ${i18nT('pages.overview.skillsTab.version_range', { from: detail.from_version, to: detail.to_version })}`
                  : ''}
              </div>
              <DiffBlock code={detail.diff} complete />
            </>
          ) : isUpdate ? null : (
            <>
              <div className="text-[11px] font-semibold text-muted">{i18nT('pages.overview.skillsTab.skill_md')}</div>
              <pre className="text-[11px] whitespace-pre-wrap max-h-64 overflow-auto p-2 rounded bg-bg-elevated border border-border">{detail.content}</pre>
            </>
          )}
          {(detail.scripts ?? []).map(s => (
            <div key={s.filename}>
              <div className="text-[11px] font-semibold text-warn">{i18nT('pages.overview.skillsTab.scripts')}{s.filename}</div>
              <pre className="text-[11px] whitespace-pre-wrap max-h-64 overflow-auto p-2 rounded bg-bg-elevated border border-border">{s.content}</pre>
            </div>
          ))}
          {/* Approve sits at the END of the content it approves, so the click is
              reached by scrolling past the body/diff rather than offered beside a
              collapsed row. The refusal sentence sits in this same block, NOT at
              the top of the panel: a stale update renders a full diff, and a
              reason printed above that diff has scrolled out of view by the time
              the user reaches the button it disables — which is the very
              unexplained-disabled shape this row was changed to remove. */}
          <div className="flex items-center justify-end gap-2 pt-0.5">
            {refusal && (
              <div className="text-[11px] p-2 rounded bg-warn-subtle text-warn border border-border flex-1">
                {refusal}
              </div>
            )}
            {/* `justRefused` covers two states, and the second is why it is not
                keyed on the refusal record alone. While the QUEUE READ is failing
                nothing on screen is confirmed, so no row offers Approve — that
                also survives the errored mutation being garbage-collected out of
                the cache, which would otherwise take the per-row lock with it and
                bring back the enabled-Approve-under-a-stale-row contradiction. */}
            <Btn primary disabled={!!refusal || busy || justRefused} onClick={() => onApprove(p.slug)}>{i18nT('pages.overview.skillsTab.approve')}</Btn>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * The one mutation key every queue action shares, so "is an action in flight"
 * is a question about the mutation cache rather than about any one component's
 * lifetime.
 */
const QUEUE_ACTION_KEY = ['skills-pending-action'] as const

/**
 * Which button the user pressed. It rides on the mutation key, under the shared
 * prefix, so every cache read that filters on `QUEUE_ACTION_KEY` still sees all
 * three (react-query matches keys by prefix) while a failure can say WHICH
 * action was refused — the one thing about the outcome the client knows for
 * certain, because it rendered the button.
 */
type QueueAction = 'approve' | 'dismiss' | 'dismiss-all'
const queueActionKey = (action: QueueAction) => [...QUEUE_ACTION_KEY, action] as const

/**
 * The lead of a failed-action notice: the verb that was refused and the
 * candidate it was for, as a complete sentence, so the server's own sentence
 * reads as a separate clause after it. (A full stop rather than a colon: the
 * catalog gate refuses a value ending in a connector as a sentence split across
 * keys, and the sibling "could not be loaded." lead already takes this shape.)
 *
 * It names the ACTION, never the candidate's fate. The server's refusal is one
 * 409 covering "not found", "a live skill already exists" and "script
 * validation failed", and only some of those leave the candidate still pending —
 * so the client cannot honestly say whether the skill was installed, discarded
 * or is still there. It can say what the user asked for and that the server
 * said no; a bare slug over the raw sentence left a blind reader unable to tell
 * whether their approval had worked or failed.
 */
function failedActionTitle(action: QueueAction, slug: string | undefined): string {
  switch (action) {
    case 'approve':
      return i18nT('pages.overview.skillsTab.couldnt_approve_slug', { slug })
    case 'dismiss':
      return i18nT('pages.overview.skillsTab.couldnt_dismiss_slug', { slug })
    case 'dismiss-all':
      return i18nT('pages.overview.skillsTab.couldnt_dismiss_all')
  }
}

/**
 * How long a settled queue action stays in the cache.
 *
 * Longer than the 5-minute default because an errored one carries the only
 * explanation the user has for a refusal, and it must outlive a trip to another
 * tab — `useMutationState` subscribes to the cache, not to individual mutations,
 * so nothing else pins it. The refusal LOCK deliberately does not depend on this
 * record surviving (a failing queue read withholds Approve on its own); this is
 * about not losing the sentence that says why.
 */
const QUEUE_ACTION_GC_MS = 30 * 60 * 1000

function PendingSkillsPanel() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const reviewParam = params.get('review')
  // Latch the deep-linked slug, then strip it from the URL. Reading the param
  // directly on every render would keep the highlight alive forever, and once
  // the candidate is approved the same param would render the "no longer
  // awaiting review" notice for work the user had just finished.
  const [reviewSlug, setReviewSlug] = useState<string | null>(null)
  useEffect(() => {
    if (!reviewParam) return
    // Evict this slug's cached detail BEFORE latching. The latch auto-expands
    // the row, and the detail query would otherwise serve a cache entry from an
    // EARLIER candidate that reused the same slug (30s global staleTime, 5min
    // gcTime) -- while `Approve` is enabled on `!!detail`, so the user could
    // approve content they never saw. Both mutations already evict this key for
    // the same reason; the deep link is a third entry point that displays detail
    // without a user click, and it arrives from a notification that fires when a
    // candidate is STAGED, which is exactly the slug-reuse case.
    qc.removeQueries({ queryKey: ['skills-pending-detail', reviewParam] })
    setReviewSlug(reviewParam)
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('review')
      return next
    }, { replace: true })
  }, [reviewParam, setParams, qc])
  const { data, isSuccess, error: listError, dataUpdatedAt } = useQuery<{ pending: PendingSkill[] }>({
    queryKey: ['skills-pending'],
    queryFn: () => api.skillsPending(),
    // Skills tab is conditionally mounted (CapabilitiesPage), so it remounts on
    // every open. Fetch fresh on each mount (overriding the 30s global
    // staleTime) so a just-staged candidate appears immediately instead of
    // after the cached list expires; the interval stays as a live backstop.
    refetchInterval: 30000,
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const pending: PendingSkill[] = data?.pending ?? []
  // ONE guard for "the queue takes one action at a time", and it lives in
  // react-query's mutation cache rather than in this component.
  //
  // Two earlier attempts at this rule failed for the same reason: they were
  // component state. `isPending` is render-derived, so it cannot see a second
  // activation in the same task — measured: three synchronous Approve clicks
  // sent three requests. A `useRef` latch fixed that but died with the
  // component, so switching Capabilities tabs mid-request and coming back
  // handed the queue a fresh, unlocked latch while the first request was still
  // running. The mutation cache has neither problem: `isMutating` is registered
  // synchronously by `mutate()` (verified) and outlives this panel's mount.
  //
  // Why one action at a time at all: each hook renders only its LATEST call, so
  // a second attempt detaches the first, and the detached one can fail with
  // nothing on screen saying so. Two attempts on one candidate is worse still —
  // the loser is refused, and that refusal can be the only thing shown for an
  // approval that in fact succeeded.
  const queueBusy = useIsMutating({ mutationKey: QUEUE_ACTION_KEY }) > 0
  // The slug of whatever attempt is in flight, from the cache rather than from a
  // hook, for the same lifetime reason.
  const pendingActionSlugs = useMutationState({
    filters: { mutationKey: QUEUE_ACTION_KEY, status: 'pending' },
    select: m => m.state.variables as string | undefined,
  })
  // …and the failures, for the same reason again. A hook's error dies with the
  // component: leave the Skills tab while a request is in flight and the
  // remounted hooks start idle, so the rejection that arrived meanwhile is never
  // shown — the user is told nothing about an action they started. The cache
  // outlives the mount, so the message survives being away from the tab.
  //
  // `submittedAt` comes along because it is what makes the refusal LOCK below
  // cache-derived too: "refused more recently than the last successful queue
  // read" is a comparison between two things the caches already know, so it needs
  // no state of its own and survives a remount exactly as the message does.
  const failedActions = useMutationState({
    filters: { mutationKey: QUEUE_ACTION_KEY, status: 'error' },
    select: m => ({
      message: (m.state.error as Error | null)?.message,
      slug: m.state.variables as string | undefined,
      // Every queue mutation is keyed by `queueActionKey`, so the suffix is
      // always one of the three verbs.
      action: m.options.mutationKey?.[1] as QueueAction,
      submittedAt: m.state.submittedAt,
    }),
  })
  // The most recent one: insertion order, and only the latest attempt is the one
  // the user is waiting on.
  const latestFailure = failedActions[failedActions.length - 1]
  // Dismissal is a DISPLAY decision, so it is mount-local on purpose and is kept
  // strictly apart from the lock below. Conflating the two is what let a user
  // switch off a safety property by tidying a message away; the lock now cannot
  // see this value at all. A message reappearing after a remount is the honest
  // cost, and it is the correct direction to fail in.
  const [dismissedAt, setDismissedAt] = useState(0)
  const failedAction = latestFailure && latestFailure.submittedAt > dismissedAt ? latestFailure : undefined
  /**
   * Rows whose Approve must be withheld because the queue on screen has not been
   * confirmed since the server last refused something.
   *
   * `dataUpdatedAt` only advances on a SUCCESSFUL fetch, so a failed reconcile
   * leaves it behind the refusal and the lock holds — which is what the queue on
   * screen being stale should mean. It survives leaving the tab and coming back,
   * because both halves of the comparison live in the caches rather than in this
   * component: an earlier version held the refused slug in component state, and a
   * remount while the queue was unreadable handed the stale row its Approve back
   * with no successful read ever having happened.
   */
  const refusedSinceLastRead = failedActions.filter(a => a.submittedAt > dataUpdatedAt)
  /**
   * The row named by the refusal currently ON SCREEN.
   *
   * `refusedSinceLastRead` lifts as soon as a read confirms the row is still
   * pending — correct on its own terms, since the server's current answer is that
   * the row is actionable. But the sentence above it says the opposite ("this
   * candidate is no longer pending"), and it outlives that read, so the panel ends
   * up arguing with itself: the message says the candidate is gone, the button
   * invites you to approve it. A blind reader shown exactly that frame said they
   * had "no idea whether that Approve still works" and would not press it — the
   * enabled button did not read as permission, it read as a broken screen.
   *
   * So a displayed refusal also withholds its own row's Approve. The two surfaces
   * now change together: message up, button held; message dismissed, button live.
   *
   * This is deliberately keyed on the DISPLAYED message (`failedAction`, which
   * respects `dismissedAt`) rather than on the retained cache records or on
   * fetching state. Keying it on the records re-locks a reconciled row on every
   * 30s poll for the 30 minutes they are retained — measured, and rejected. And
   * the safety lock is untouched: `refusedSinceLastRead` never consults
   * `dismissedAt`, so tidying the message away still cannot hand back an Approve
   * over a queue that has not been read successfully since the refusal.
   */
  const displayedRefusalSlug = failedAction?.slug
  // Cache-owned state has to be cleared deliberately, where a hook's error was
  // discarded for us. Scoped to the slug being acted on: clearing every retained
  // error would drop ANOTHER row's refusal record, and that row's Approve would
  // re-enable for the gap until the next list fetch. The notice's dismiss only
  // hides, so dismissing can never lift a refusal lock.
  const clearFailedActions = (slug?: string) => {
    const cache = qc.getMutationCache()
    cache
      .findAll({ mutationKey: QUEUE_ACTION_KEY, status: 'error' })
      .filter(m => slug === undefined || m.state.variables === slug)
      .forEach(m => cache.remove(m))
  }
  const startAction = (slug: string | undefined, run: () => void) => {
    if (qc.isMutating({ mutationKey: QUEUE_ACTION_KEY })) return
    clearFailedActions(slug)
    run()
  }
  /**
   * Reconcile the queue against the server when an action FAILS.
   *
   * Without this the refusal contradicts the screen: "this candidate is no
   * longer pending" renders above the very row it names, still carrying a live
   * Approve and the old count, until the 30s poll happens to catch up. A reader
   * shown that state does not press the button — and cannot tell which half is
   * lying. Refetching on failure is also the honest reading of a refusal: the
   * server has just told us our list is wrong, so the row leaves and the notice
   * is left as the explanation for where it went.
   *
   * Deliberately NOT awaited from `onError`. Handing this promise back would keep
   * the mutation in `pending` until the read settles, covering the gap between a
   * refusal and its reconcile landing via `busy` — but it was measured to suppress
   * the failure MESSAGE: while an `onError` promise is unresolved the mutation
   * reads `status: pending` with `error: null`, so nothing renders the refusal
   * until an unrelated network call finishes, and a hanging read shows a spinner
   * and no explanation at all. That trades away the thing this surface exists to
   * fix, so the residual gap is accepted and recorded instead.
   */
  const reconcileAfterFailure = (slug?: string) => {
    // INVALIDATE the detail rather than remove it: removing evicts the body the
    // open panel is rendering, so Approve and the content vanish for a beat and
    // come back — a flash of nothing on the row the user is reading. Invalidating
    // refetches underneath the visible data. (The success paths do remove it, for
    // a different reason: there the candidate is gone and its slug may be
    // re-staged, so stale detail must not survive.)
    if (slug) {
      qc.invalidateQueries({ queryKey: ['skills-pending-detail', slug] })
    }
    qc.invalidateQueries({ queryKey: ['skills-pending'] })
  }
  const approve = useMutation({
    mutationKey: queueActionKey('approve'),
    gcTime: QUEUE_ACTION_GC_MS,
    mutationFn: (slug: string) => api.approvePendingSkill(slug),
    onError: (_e, slug) => reconcileAfterFailure(slug),
    onSuccess: (_data, slug) => {
      // Drop the deep-link latch when the user acts on the linked candidate
      // THEMSELVES. Without this, approving the row you arrived at makes the
      // refetch omit it, which flips reviewMissing and reports "no longer
      // awaiting review -- it was approved or dismissed" one click after the
      // user approved it; when it was the only row that sentence becomes the
      // whole panel. The notice is for a candidate resolved BEFORE you got
      // here, not for your own action.
      if (slug === reviewSlug) setReviewSlug(null)
      // Evict the per-slug detail cache so a slug re-staged after this one went
      // live can't surface the promoted candidate's stale detail.
      qc.removeQueries({ queryKey: ['skills-pending-detail', slug] })
      // Approving changes the live skill, which invalidates the diff/version of
      // every OTHER open update candidate targeting it. Without this, a sibling
      // row keeps rendering its pre-approval diff from cache.
      qc.invalidateQueries({ queryKey: ['skills-pending-detail'] })
      qc.invalidateQueries({ queryKey: ['skills-pending'] })
      qc.invalidateQueries({ queryKey: ['skills'] })
    },
  })
  const dismiss = useMutation({
    mutationKey: queueActionKey('dismiss'),
    gcTime: QUEUE_ACTION_GC_MS,
    mutationFn: (slug: string) => api.dismissPendingSkill(slug),
    onError: (_e, slug) => reconcileAfterFailure(slug),
    onSuccess: (_data, slug) => {
      // Same reason as approve: a dismissal the user just performed must not
      // come back as "someone resolved this already".
      if (slug === reviewSlug) setReviewSlug(null)
      // Evict the per-slug detail cache too, so a slug re-staged shortly after
      // dismissal can't show the dismissed candidate's stale detail (which a
      // user might then approve without seeing the replacement).
      qc.removeQueries({ queryKey: ['skills-pending-detail', slug] })
      qc.invalidateQueries({ queryKey: ['skills-pending'] })
    },
  })
  const dismissAll = useMutation({
    mutationKey: queueActionKey('dismiss-all'),
    gcTime: QUEUE_ACTION_GC_MS,
    mutationFn: () => api.dismissAllPendingSkills(pending.map(p => p.slug)),
    onError: () => reconcileAfterFailure(),
    onSuccess: () => {
      setReviewSlug(null)
      qc.removeQueries({ queryKey: ['skills-pending-detail'] })
      qc.invalidateQueries({ queryKey: ['skills-pending'] })
    },
  })
  // The queue takes ONE action at a time. Each mutation hook holds the state of
  // its latest call, so a second attempt started before the first settles
  // detaches the first: that request can then fail with nothing rendering its
  // failure, which is the silent-refusal defect this panel was changed to remove,
  // reappearing only when two actions overlap. Serialising the controls removes
  // the overlap rather than tracking it — with at most one attempt in flight,
  // the hook's single error slot always belongs to the attempt the user is
  // waiting on. The actions are one request long, so the lock is imperceptible
  // except as the spinner on the row that owns it.
  const busy = queueBusy
  // The slug of the attempt in flight, read from the cache for the same reason:
  // after a remount the hooks are fresh, but the request is not.
  const busySlug = pendingActionSlugs[0]
  // Only claim a deep-linked candidate is gone once the queue has actually been
  // read -- `pending` is [] while the first fetch is in flight, which would
  // otherwise flash the notice on every deep link.
  const reviewMissing = !!reviewSlug && isSuccess && !pending.some(p => p.slug === reviewSlug)
  // Without the notice a deep link from a notification whose candidate was
  // already resolved lands on a Skills tab that looks completely normal, and
  // the user is left hunting for a row that no longer exists.
  //
  // The panel also stays rendered while it OWES the user a message. Approving the
  // last candidate empties the queue, and a failure that lands after the queue
  // went empty — another client resolved it, the refetch returned nothing — would
  // otherwise have nowhere to appear: this early return would have already
  // replaced the only surface that could carry it with null.
  const owesMessage = busy || !!failedAction || !!listError
  if (pending.length === 0 && !reviewMissing && !owesMessage) return null
  // No top margin on the root, for the same reason as the tab's heading below:
  // this panel is the Skills tab's FIRST in-flow element whenever it renders,
  // and the pane already owns the gap under the tab strip. It is also WHY that
  // heading drops its margin outright instead of using `first:mt-0` — this panel
  // returns null when there is nothing pending, so the heading moves in and out
  // of `:first-child` with the pending count.
  return (
    <div className="mb-2">
      {/* Suppressed when the ONLY thing to show is the resolved-candidate
          notice: a "Pending review (0)" heading over a sentence explaining
          there is nothing to review reads like a broken count. */}
      {pending.length > 0 && (
        <h4 className="text-sm font-semibold text-text-strong mb-2 flex items-center gap-2">
          {i18nT('pages.overview.skillsTab.pending_review_count', { count: pending.length })}
          <InfoTip text={i18nT('pages.overview.skillsTab.auto_generated_skill_candidates_awaiting_your_ap')} />
          <Btn danger disabled={busy} className="ml-auto text-[11px]" onClick={() => { if (confirm(i18nT('pages.overview.skillsTab.dismiss_all_confirm', { count: pending.length }))) startAction(undefined, () => dismissAll.mutate()) }}>{i18nT('pages.overview.skillsTab.dismiss_all')}</Btn>
        </h4>
      )}
      {pending.length > 0 && (
        <p className="text-[11px] text-muted mb-2">
          <Trans i18nKey="pages.overview.skillsTab.approval_required_hint" components={{ settingRef: <SettingRef configKey="skills.approval_required" /> }} />
        </p>
      )}
      {reviewMissing && (
        <div className="mb-2 text-[11px] p-2 rounded bg-bg-elevated border border-border text-muted">
          {i18nT('pages.overview.skillsTab.linked_candidate_no_longer_pending')}
        </div>
      )}
      {/* The QUEUE itself could not be read. No `onDismiss`: this is live query
          state rather than a one-off event, so a dismissal would be undone by the
          next poll — and a control that will not stay dismissed reads as broken.

          The title scopes the failure to THIS queue. The server's sentence
          ("skills directory is unreadable") names the whole store, and the live
          Skills list renders confidently right underneath it, so without the
          scope the two read as contradicting each other — a reader could not
          tell which half to believe. */}
      <ErrorNotice
        title={i18nT('pages.overview.skillsTab.pending_review_could_not_be_loaded')}
        message={listError?.message}
        askAgent
        className="mb-2"
        testId="pending-list-error"
      />
      {/* A failed approve / dismiss belongs to the ATTEMPT, not to a row, and
          not to this component's mount either. Both were tried: a per-candidate
          store had to be re-judged every time the queue moved under it (it
          mis-attributed twice), and hook-held state was discarded the moment the
          user left the tab. So the mutation cache owns it, and the sentence is
          "Couldn't <verb> <slug>. <what the server said>", which stays true
          however the queue has since changed and whichever tab the user is on
          when it lands. The lead names the verb the user pressed and nothing
          about where the candidate ended up — see `failedActionTitle`.

          `askAgent` is on: the queue holds no draft input and the candidate is
          already persisted server-side, so the hand-off destroys nothing — and a
          refusal the user cannot act on is often one the agent can. */}
      <ErrorNotice
        message={failedAction?.message}
        title={failedAction && failedActionTitle(failedAction.action, failedAction.slug)}
        askAgent
        onDismiss={() => setDismissedAt(Date.now())}
        className="mb-2"
        testId="pending-action-error"
      />
      {/* A refused APPROVE is the one refusal whose arc otherwise ends unresolved:
          the server's single 409 covers "not found", "a live skill already
          exists" and "script validation failed", and those leave the candidate in
          three different places (installed, gone, still pending). The client
          cannot say which — see `failedActionTitle` — but it can say WHERE the
          answer is: the Skills list further down this tab shows what is live.
          So this line names the place to look and nothing about the outcome;
          a reader who had just seen the banner said they "never learn whether
          the approval happened", and a pointer is the honest part of an answer.
          It follows the notice's own display decision (`failedAction`), so it
          goes when the notice is dismissed. */}
      {failedAction?.action === 'approve' && (
        <p className="text-[11px] text-muted mb-2" data-testid="pending-action-next-step">
          {i18nT('pages.overview.skillsTab.approve_refused_check_skills_list', { slug: failedAction.slug })}
        </p>
      )}
      {/* The queue is CONFIRMED empty and the panel is up only because it owes a
          message. Without this line the panel is a lone error banner with no
          heading and no list, and a reader cannot tell whether the list is hidden
          because there is nothing in it or because it failed to load. Keyed on
          `isSuccess`, not on `pending.length` alone: a failed read RETAINS the
          previous data (so `pending` can be [] with the queue unreadable), and
          claiming emptiness over a queue the client could not read is exactly the
          ambiguity this line exists to remove. */}
      {pending.length === 0 && isSuccess && owesMessage && (
        <p className="text-[11px] text-muted mb-2" data-testid="pending-queue-empty">
          {i18nT('pages.overview.skillsTab.no_candidates_pending')}
        </p>
      )}
      {pending.length > 0 && (
        <Card>
          <div className="space-y-2">
            {pending.map(p => (
              <PendingCandidateRow
                // Generation in the key: a successor under a reused slug is a
                // DIFFERENT candidate, so it must mount fresh and collapsed
                // rather than inherit the expanded state of the row it replaced.
                key={`${p.slug}@${p.created_at ?? ''}`}
                p={p}
                autoOpen={p.slug === reviewSlug}
                busy={busy}
                busySelf={busySlug === p.slug}
                justRefused={!!listError
                  || refusedSinceLastRead.some(a => a.slug === p.slug)
                  || displayedRefusalSlug === p.slug}
                onApprove={s => startAction(s, () => approve.mutate(s))}
                onDismiss={s => startAction(s, () => dismiss.mutate(s))}
              />
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
