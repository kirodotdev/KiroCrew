import { useState, useMemo, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { api } from '../../api/client'
import { Card, Btn, SearchInput, EmptyState, Toggle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import Modal from '../../components/Modal'
import SkillForm, { assembleSkillContent, parseSkillContent, type SkillFormData } from '../../components/SkillForm'
import SkillDirectoryBrowser from '../../components/SkillDirectoryBrowser'
import SkillBrowserModal from '../../components/SkillBrowserModal'
import DiffBlock from '../../components/DiffBlock'
import { useProvider } from '../../providers'
import type { Skill } from '../../types'
import SkillContextBudget from './SkillContextBudget'

import { fmtBytes, fmtCompact } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
const EMPTY_FORM: SkillFormData = { name: '', category: '', description: '', triggers: '', tags: '', always: false, body: '' }

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
      queryClient.invalidateQueries({ queryKey: ['skills'] })
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

  const selectSkill = (s: Skill) => { setSelectedKey(s.key); setDetailEditing(false) }

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
    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">{i18nT('pages.overview.skillsTab.skills')} <InfoTip text={i18nT('pages.overview.skillsTab.on_demand_skills_loaded_when_the_agent_determine')} /> <Btn primary disabled>{i18nT('pages.overview.skillsTab.create_new_skill')}</Btn></h4>
    <Card>
      <div className="flex items-center gap-2 mb-3"><div className="h-8 max-w-[480px] flex-1 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5 }} /></div>
      <div className="flex gap-3 h-[calc(100vh-260px)] min-h-[420px]">
        <div className="w-[240px] shrink-0 space-y-1">{Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-[58px] rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5, animationDelay: `${i * 80}ms` }} />
        ))}</div>
        <div className="flex-1 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.3 }} />
      </div>
    </Card>
  </>)

  // Control plane: full-page budget view, deep-linkable via ?view=budget.
  if (viewBudget) return <SkillContextBudget onBack={hideBudget} />

  return (<>
    <PendingSkillsPanel />
    {/* Create Skill Modal */}
    <Modal open={creating} onClose={() => setCreating(false)} title={i18nT('pages.overview.skillsTab.create_new_skill')} maxWidth={560} footer={<>
      <Btn onClick={() => setCreating(false)}>{i18nT('pages.overview.skillsTab.cancel')}</Btn>
      <Btn primary onClick={() => { if (formData.name) { const path = formData.category ? `${formData.category}/${formData.name}` : formData.name; createSkill.mutate({ name: path, content: assembleSkillContent(formData) }) } }} disabled={!formData.name}>{i18nT('pages.overview.skillsTab.create')}</Btn>
    </>}>
      <SkillForm data={formData} onChange={setFormData} />
    </Modal>

    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">{i18nT('pages.overview.skillsTab.skills_count', { count: skills.length })} <InfoTip text={i18nT('pages.overview.skillsTab.skills_tip')} /> <span className="ml-auto flex items-center gap-2"><Btn onClick={showBudget} className="text-accent border-accent/30 bg-accent/5 hover:bg-accent/10">{i18nT('pages.overview.skillsTab.budget_doorway_static')}</Btn><Btn onClick={() => setSkillBrowserOpen(true)}><Download size={14} /> {i18nT('pages.overview.skillsTab.add_skill')}</Btn><Btn primary onClick={() => { setFormData(EMPTY_FORM); setCreating(true) }}>{i18nT('pages.overview.skillsTab.create_new_skill')}</Btn></span></h4>
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
        /* Master-detail: skill list (pane 1) on the left, then the directory
         *  browser (panes 2+3: file tree + file content) on the right. */
        <div className="flex gap-3 h-[calc(100vh-260px)] min-h-[420px]">
          {/* Pane 1 — skill list.  ``scrollbar-overlay`` keeps the scrollbar
           *  hidden until hover and overlays it so the row width never shifts
           *  between scrollable and non-scrollable states. */}
          <div className="w-[240px] shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md p-2" role="listbox" aria-label={i18nT('pages.overview.skillsTab.skills')}>
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
          </div>

          {/* Panes 2+3 — directory browser, or the edit form */}
          <div className="flex-1 min-w-0 flex flex-col border border-border rounded-md bg-card overflow-hidden">
            {!selectedSkill ? (
              <div className="flex items-center justify-center h-full text-muted text-[13px]">{i18nT('pages.overview.skillsTab.select_a_skill_to_view_its_files')}</div>
            ) : detailEditing ? (
              <div className="flex flex-col h-full min-h-0">
                <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-border shrink-0">
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
                <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-border shrink-0">
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
          </div>
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

function PendingCandidateRow({ p, autoOpen, onApprove, onDismiss }: {
  p: PendingSkill
  /** True when a notification deep-linked at THIS candidate (?review=<slug>). */
  autoOpen?: boolean
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
  const { data: detail } = useQuery<PendingDetail>({
    queryKey: ['skills-pending-detail', p.slug],
    queryFn: () => api.skillPendingDetail(p.slug),
    enabled: open,
  })
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
              <span className="ml-2 text-[10px] px-1.5 py-[1px] rounded-full bg-warn-subtle text-warn font-bold">{i18nT('pages.overview.skillsTab.script')}</span>
            )}
          </div>
          <div className="text-[12px] text-muted truncate">
            {isUpdate && p.target
              ? i18nT('pages.overview.skillsTab.adds_new_requirements_to', { target: p.target, description: p.description })
              : p.description}
          </div>
        </div>
        <Btn onClick={() => setOpen(o => !o)}>{open ? i18nT('pages.overview.skillsTab.hide') : i18nT('pages.overview.skillsTab.review')}</Btn>
        {/* An update whose target was archived/removed after staging has nothing
            to apply, and a stale update (live moved on since the merge) would
            replace the newer approved content — the backend refuses both, so keep
            the button disabled and let the expanded panel explain. */}
        <Btn primary disabled={!open || !detail || (isUpdate && (!detail.diff || !!detail.stale_base))} onClick={() => onApprove(p.slug)}>{i18nT('pages.overview.skillsTab.approve')}</Btn>
        <Btn danger onClick={() => { if (confirm(i18nT('pages.overview.skillsTab.dismiss_confirm', { name: p.name }))) onDismiss(p.slug) }}>{i18nT('pages.overview.skillsTab.dismiss')}</Btn>
      </div>
      {open && detail && (
        <div className="mt-2 space-y-2">
          {isUpdate && detail.stale_base && (
            <div className="text-[11px] p-2 rounded bg-warn-subtle text-warn border border-border">
              {i18nT('pages.overview.skillsTab.this_skill_changed_after_this_update_was_written')}
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
          ) : isUpdate ? (
            <div className="text-[11px] p-2 rounded bg-bg-elevated border border-border text-muted">
              {i18nT('pages.overview.skillsTab.the_skill_this_update_targets_no_longer_exists_s')}
            </div>
          ) : (
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
        </div>
      )}
    </div>
  )
}

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
  const { data, isSuccess } = useQuery<{ pending: PendingSkill[] }>({
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
  const approve = useMutation({
    mutationFn: (slug: string) => api.approvePendingSkill(slug),
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
    mutationFn: (slug: string) => api.dismissPendingSkill(slug),
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
  // Only claim a deep-linked candidate is gone once the queue has actually been
  // read -- `pending` is [] while the first fetch is in flight, which would
  // otherwise flash the notice on every deep link.
  const reviewMissing = !!reviewSlug && isSuccess && !pending.some(p => p.slug === reviewSlug)
  // Without the notice a deep link from a notification whose candidate was
  // already resolved lands on a Skills tab that looks completely normal, and
  // the user is left hunting for a row that no longer exists.
  if (pending.length === 0 && !reviewMissing) return null
  return (
    <div className="mt-4 mb-2">
      {/* Suppressed when the ONLY thing to show is the resolved-candidate
          notice: a "Pending review (0)" heading over a sentence explaining
          there is nothing to review reads like a broken count. */}
      {pending.length > 0 && (
        <h4 className="text-sm font-semibold text-text-strong mb-2 flex items-center gap-2">
          {i18nT('pages.overview.skillsTab.pending_review_count', { count: pending.length })}
          <InfoTip text={i18nT('pages.overview.skillsTab.auto_generated_skill_candidates_awaiting_your_ap')} />
        </h4>
      )}
      {reviewMissing && (
        <div className="mb-2 text-[11px] p-2 rounded bg-bg-elevated border border-border text-muted">
          {i18nT('pages.overview.skillsTab.linked_candidate_no_longer_pending')}
        </div>
      )}
      {pending.length > 0 && (
        <Card>
          <div className="space-y-2">
            {pending.map(p => (
              <PendingCandidateRow
                key={p.slug}
                p={p}
                autoOpen={p.slug === reviewSlug}
                onApprove={s => approve.mutate(s)}
                onDismiss={s => dismiss.mutate(s)}
              />
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
