import { useState, useMemo, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
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
import { parseErrorCode, findReport, type ErrorReport } from '../../utils/errorReport'
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
  // Update/delete failures get their own state rather than sharing one string:
  // the save failure belongs next to the detail editor where the save was
  // attempted, the delete failure belongs on the list surface where the
  // rolled-back row reappears — one shared slot would render in the wrong place.
  const [updateError, setUpdateError] = useState('')
  // The delete state carries the journal's structured report next to the
  // display message: the visible text is a translated frame, not the raw
  // `err.message` the journal is keyed on, so ErrorNotice's own message-match
  // lookup would come up empty and the Ask-agent hand-off would lose the
  // endpoint/status/code context. Recovered once at onError time and passed
  // through the `report` prop; both halves clear together.
  const [deleteError, setDeleteError] = useState<{ message: string; report?: ErrorReport } | null>(null)

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
      setUpdateError('')
      // Deliberately NO setDeleteError(null) here: a delete failure that
      // arrived while this editor was open was suppressed the whole time, and
      // clearing it now would drop it unseen — the rolled-back row would sit
      // in the list with no explanation, the original silent-failure symptom.
      // A banner the user had already seen was retired when they entered the
      // editor (the Edit button clears it), so nothing stale survives to
      // contradict this success.
      queryClient.invalidateQueries({ queryKey: ['skills'] })
      queryClient.invalidateQueries({ queryKey: ['skill-detail'] })
    },
    // Same narrow as createSkill above. `readonly_skill_prefix` is the one
    // coded refusal the PUT verb returns, and its server prose names on-disk
    // territories — filesystem trivia next to a Save button — so it gets the
    // translated hint. Every other failure keeps the server's own message;
    // the notice's translated "Save failed" title carries the frame, so the
    // message stays the raw actionable half (and the journal's lookup key).
    onError: (e: Error) => {
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      setUpdateError(code === 'readonly_skill_prefix'
        ? i18nT('pages.overview.skillsTab.readonly_skill_error')
        : e.message)
    },
  })

  const deleteSkill = useMutation({
    mutationFn: (key: string) => api.deleteSkill(key),
    onMutate: async (key) => {
      // A retry retires the previous failure banner immediately: leaving the
      // old "could not delete" up while the new attempt is in flight would
      // report a stale outcome as current. A fresh failure re-sets it.
      setDeleteError(null)
      await queryClient.cancelQueries({ queryKey: ['skills'] })
      const prev = queryClient.getQueryData<Skill[]>(['skills'])
      queryClient.setQueryData<Skill[]>(['skills'], old => old?.filter(s => s.key !== key) ?? [])
      return { prev }
    },
    onSuccess: () => {
      // An OPEN editor must survive this teardown, whatever its save state:
      // dropping the selection or the editing flag unmounts it (a null
      // selection alone does, via the placeholder branch), which discards a
      // typed-but-unsaved draft outright and leaves an in-flight PUT's later
      // rejection with nothing to render it. `!detailEditing` covers both — a
      // pending save can only exist while the editor is open, since every
      // path that leaves it is disabled or no-ops during the save window.
      // Outside the editor, the original teardown applies.
      if (!detailEditing) {
        setSelectedKey(null)
        setDetailEditing(false)
      }
      setDeleteError(null)
      // Discover results carry an installed flag derived from the skills
      // dir -- drop them so the Add Skill browser reflects the deletion.
      queryClient.invalidateQueries({ queryKey: ['discover-skills'] })
    },
    onError: (err: Error, key, context) => {
      if (context?.prev) queryClient.setQueryData(['skills'], context.prev)
      // Rollback first (above, unchanged), then say why the row came back:
      // reversing the optimistic write alone re-renders the deleted row with
      // no explanation, which reads as the delete silently not working.
      // The report is looked up by the ORIGINAL message before it is framed.
      // The banner names the skill the way the ROW does (its display name,
      // from the rollback snapshot — guaranteed to hold the deleted skill),
      // so the reader matches banner to row without decoding the raw key.
      const target = context?.prev?.find(s => s.key === key)
      const code = err instanceof ApiError ? parseErrorCode(err.body) : undefined
      // The frame ALWAYS carries the skill name and the word "delete" — a bare
      // hint next to several rows would name neither what failed nor on which
      // skill. A coded refusal swaps only the {{error}} half for its hint.
      setDeleteError({
        report: findReport(err.message),
        message: i18nT('pages.overview.skillsTab.delete_failed_error', {
          name: target ? displayName(target) : key,
          error: code === 'readonly_skill_prefix'
            ? i18nT('pages.overview.skillsTab.readonly_skill_error')
            : err.message,
        }),
      })
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

  // Moving on to another skill also retires a delete failure — but only one
  // the user could have SEEN: the test is the banner's own render gate
  // (visible unless the editor is showing), so a click made from inside a
  // visible editor keeps the suppressed failure for later display, while a
  // click made with the banner on screen — the mobile latched-session list
  // included — retires it as acknowledged. While a save is in
  // flight the click is a no-op instead — switching rows would unmount the
  // editor mid-request, and the PUT's outcome (a failure included) would
  // report nowhere; the Cancel button is disabled for the same window.
  const selectSkill = (s: Skill) => {
    if (updateSkill.isPending) return
    // A LATCHED edit session (mobile Back) holds the only copy of a draft,
    // and on a phone a row tap is the only way back to the detail pane —
    // changing selection here would end the session and the next Edit would
    // reseed formData, silently discarding the draft. Reopen the latched
    // editor instead; leaving it goes through its own explicit exits (Cancel,
    // Save), which settle the draft's fate visibly. Only while the latched
    // selection still resolves: if the skill vanished underneath the latch
    // (an external delete picked up by a refetch), reopening would land on
    // the no-selection placeholder — the one detail branch with no Back —
    // so a dead latch falls through to the normal select path instead.
    if (detailEditing && !showDetail && selectedSkill) { openDetail(); return }
    if (!(detailEditing && showDetail)) setDeleteError(null)
    setSelectedKey(s.key); setDetailEditing(false); openDetail()
  }

  /** One row in the left list. */
  const renderRow = (s: Skill) => {
    const isSel = s.key === selectedKey
    // While a save is in flight `selectSkill` no-ops; the row says so instead
    // of keeping its live cursor and hover — a silent no-op on a control that
    // looks live reads as a broken UI.
    const rowInert = updateSkill.isPending
    return (
      <div
        key={s.key}
        role="button"
        tabIndex={0}
        aria-current={isSel ? 'true' : undefined}
        aria-disabled={rowInert ? 'true' : undefined}
        aria-label={i18nT('pages.overview.skillsTab.select', { name: displayName(s) })}
        onClick={() => selectSkill(s)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectSkill(s) } }}
        className={`flex flex-col gap-0.5 px-3 py-2.5 rounded-md mb-1 transition-colors ${
          rowInert ? 'cursor-default opacity-60' : 'cursor-pointer'
        } ${
          isSel ? 'list-selected bg-accent-subtle' : `bg-bg-elevated ${rowInert ? '' : 'hover:bg-bg-hover'}`
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

      {/* The delete failure lands here on the LIST surface, not in the detail
        * pane: the mutation's rollback re-renders the deleted row, so the
        * notice must sit next to the row that came back — the detail pane may
        * have moved on (mobile shows one pane at a time). The hand-off is only
        * safe because this banner never co-renders with a VISIBLE editor: the
        * render is gated on the editor actually showing (detailEditing AND
        * showDetail), which also covers a delayed failure arriving after the
        * user already opened another skill's editor. On a phone, Back latches
        * detailEditing (that latch restores the draft across a breakpoint
        * crossing) while showDetail goes false — the banner may then show on
        * the list. The error is retained, so it displays once the editor is
        * out of view; dismiss, row selection, and a later success retire it. */}
      {!(detailEditing && showDetail) && (
        /* No hand-off while an edit session is LATCHED (mobile Back hides the
           editor without ending it): the hand-off navigates to the chat and
           unmounts this tab, and the latched formData is still the only copy
           of that draft. The hand-off returns as soon as no session holds a
           draft. */
        <ErrorNotice className="mb-3" askAgent={!detailEditing} message={deleteError?.message} report={deleteError?.report} onDismiss={() => setDeleteError(null)} testId="skill-delete-failure" />
      )}

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
                {/* Same in-flight window as Cancel and row selection: on a
                    phone this is the editor's only other exit, and taking it
                    mid-save would unmount the pane that renders the save's
                    failure. Back deliberately does NOT exit the edit session:
                    the latched detailEditing is what restores the draft if
                    the viewport crosses back over the desktop breakpoint.
                    The delete banner handles the latched state itself — its
                    gate only suppresses it while the editor is VISIBLE. */}
                {isMobile && (
                  <div className="px-4 pt-2.5 shrink-0">
                    <ListDetailBack label={i18nT('pages.overview.skillsTab.skills')} onBack={() => { if (updateSkill.isPending) return; closeDetail() }} />
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-2.5 border-b border-border shrink-0">
                  <span className="text-sm font-mono font-bold text-text-strong truncate">{selectedSkill.key}</span>
                  <div className="flex gap-2 shrink-0">
                    <Btn disabled={updateSkill.isPending} onClick={() => setDetailEditing(false)}>{i18nT('pages.overview.skillsTab.cancel')}</Btn>
                    <Btn primary disabled={updateSkill.isPending} onClick={() => updateSkill.mutate({ key: selectedSkill.key, content: assembleSkillContent(formData) })}>{updateSkill.isPending ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.overview.skillsTab.saving')}</> : i18nT('pages.overview.skillsTab.save')}</Btn>
                  </div>
                </div>
                {updateError && (
                  <div className="px-4 pt-2.5 shrink-0">
                    {/* No hand-off: it navigates to the chat, which unmounts
                        this editor — and a failed save means the form below
                        holds the ONLY copy of the edit. This is exactly the
                        draft-loss case ErrorNotice's opt-in exists for. The
                        title carries the translated frame so the message can
                        stay the raw server text (the journal's lookup key);
                        dismiss matches the delete banner, so the two notices
                        read as the same species. */}
                    <ErrorNotice variant="inline" title={i18nT('pages.overview.skillsTab.update_failed_title')} message={updateError} onDismiss={() => setUpdateError('')} testId="skill-update-failure" />
                  </div>
                )}
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
                      {/* Entering the editor retires a delete banner the user
                          can SEE right now (Edit is only reachable from the
                          non-editing branch, where the banner gate is open) —
                          the same acknowledged-by-navigation rule as a row
                          click. A failure arriving later, while the editor is
                          open, is retained instead and displays after close. */}
                      <Btn disabled={!detailReady} onClick={() => { setDetailEditing(true); setUpdateError(''); setDeleteError(null); setFormData(parseSkillContent(detailContent, selectedSkill.key)) }}>{i18nT('pages.overview.skillsTab.edit')}</Btn>
                      {/* isPending gate: two overlapping deletes cross their
                          hook-level callbacks — the first delete's onSuccess
                          would clear the second's failure notice, and the
                          optimistic snapshots would restore each other's
                          rows. One delete at a time, like the create modal. */}
                      <Btn danger disabled={deleteSkill.isPending} onClick={() => { if (deleteSkill.isPending) return; if (confirm(i18nT('pages.overview.skillsTab.delete_confirm', { name: selectedSkill.key }))) deleteSkill.mutate(selectedSkill.key) }}>{i18nT('pages.overview.skillsTab.delete')}</Btn>
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
          {p.has_scripts && (
            /* Scripts are a hard security boundary: a script-bearing candidate
               stages for manual review even with skills.approval_required off.
               Without this note a user who disabled approval sees the row and
               has no idea why the setting "didn't work". */
            <div className="text-[11px] p-2 rounded bg-warn-subtle text-warn border border-border">
              {i18nT('pages.overview.skillsTab.scripts_always_require_review')}
            </div>
          )}
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
  const dismissAll = useMutation({
    mutationFn: () => api.dismissAllPendingSkills(pending.map(p => p.slug)),
    onSuccess: () => {
      setReviewSlug(null)
      qc.removeQueries({ queryKey: ['skills-pending-detail'] })
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
          <Btn danger className="ml-auto text-[11px]" onClick={() => { if (confirm(i18nT('pages.overview.skillsTab.dismiss_all_confirm', { count: pending.length }))) dismissAll.mutate() }}>{i18nT('pages.overview.skillsTab.dismiss_all')}</Btn>
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
