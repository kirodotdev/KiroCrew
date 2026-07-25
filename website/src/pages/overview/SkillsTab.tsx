import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { Card, Btn, SearchInput } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import Modal from '../../components/Modal'
import SkillForm, { assembleSkillContent, parseSkillContent, type SkillFormData } from '../../components/SkillForm'
import SkillDirectoryBrowser from '../../components/SkillDirectoryBrowser'
import SkillBrowserModal from '../../components/SkillBrowserModal'
import { useProvider } from '../../providers'
import type { Skill } from '../../types'

const EMPTY_FORM: SkillFormData = { name: '', category: '', description: '', triggers: '', tags: '', always: false, body: '' }

/** Humanize a kebab/snake-case skill name for display. */
const displayName = (s: Skill) => s.name.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())

/** Short, human label for a skill's provenance — drives the source badge. */
function sourceLabel(source: Skill['source']): string | null {
  switch (source) {
    case 'package': return 'Package'
    case 'kiro-user': return '~/.kiro/skills'
    case 'kiro-workspace': return 'workspace'
    default: return null  // kirocrew — the default home, no badge needed
  }
}

export default function SkillsTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [formData, setFormData] = useState<SkillFormData>(EMPTY_FORM)
  const [skillFilter, setSkillFilter] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [detailEditing, setDetailEditing] = useState(false)
  // Multi-provider skill browser drawer (Add Skill button).
  const [skillBrowserOpen, setSkillBrowserOpen] = useState(false)

  const { data: skills = [], isLoading, isFetching, refetch } = useQuery<Skill[]>({
    queryKey: ['skills'],
    queryFn: () => api.skills(),
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
        aria-label={`Select ${displayName(s)}`}
        onClick={() => selectSkill(s)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectSkill(s) } }}
        className={`flex flex-col gap-0.5 px-3 py-2.5 rounded-md cursor-pointer mb-1 transition-colors ${
          isSel ? 'list-selected bg-accent-subtle' : 'bg-bg-elevated hover:bg-bg-hover'
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[13px] font-semibold text-text truncate flex-1">{displayName(s)}</span>
          {s.source === 'package'
            ? <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-aim-subtle text-aim border border-aim/30 font-bold shrink-0">Package</span>
            : s.always
              ? <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-ok-subtle text-ok font-bold shrink-0">auto</span>
              : <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-bg-elevated text-muted border border-border font-bold shrink-0">on-demand</span>}
        </div>
        <div className="text-[11px] text-muted font-mono truncate">{s.key}</div>
        {s.loaded_by_agents && s.loaded_by_agents.length > 0 && (
          <div className="text-[10px] text-muted/70 truncate" title={`Loaded by: ${s.loaded_by_agents.join(', ')}`}>
            Loaded by {s.loaded_by_agents.length} agent{s.loaded_by_agents.length === 1 ? '' : 's'}
          </div>
        )}
      </div>
    )
  }

  if (isLoading) return (<>
    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">Skills <InfoTip text="On-demand skills loaded when the agent determines they're relevant." /> <Btn primary disabled>Create New Skill</Btn></h4>
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

  return (<>
    <PendingSkillsPanel />
    {/* Create Skill Modal */}
    <Modal open={creating} onClose={() => setCreating(false)} title="Create New Skill" maxWidth={560} footer={<>
      <Btn onClick={() => setCreating(false)}>Cancel</Btn>
      <Btn primary onClick={() => { if (formData.name) { const path = formData.category ? `${formData.category}/${formData.name}` : formData.name; createSkill.mutate({ name: path, content: assembleSkillContent(formData) }) } }} disabled={!formData.name}>Create</Btn>
    </>}>
      <SkillForm data={formData} onChange={setFormData} />
    </Modal>

    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">Skills ({skills.length}) <InfoTip text="On-demand skills loaded when the agent determines they're relevant. Skills with the 'auto' badge are always injected into every session. Skills are discovered from KiroCrew, kiro-cli (~/.kiro/skills), and AIM packages." /> <span className="ml-auto flex items-center gap-2"><Btn onClick={() => setSkillBrowserOpen(true)}><Download size={14} /> Add Skill</Btn><Btn primary onClick={() => { setFormData(EMPTY_FORM); setCreating(true) }}>Create New Skill</Btn></span></h4>
    <Card>
      <div className="flex items-center gap-2 mb-3">
        <div className="relative max-w-[480px] flex-1">
          <SearchInput placeholder="Filter skills…" value={skillFilter} onChange={e => setSkillFilter(e.target.value)} />
          {skillFilter && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer" onClick={() => setSkillFilter('')} aria-label="Clear search">&times;</button>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Btn onClick={() => refetch()} disabled={isFetching} aria-label="Refresh skills"><RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} /></Btn>
        </div>
      </div>

      {skills.length === 0 ? <div className="text-muted italic py-3.5 text-sm">No skills installed</div> : (
        /* Master-detail: skill list (pane 1) on the left, then the directory
         *  browser (panes 2+3: file tree + file content) on the right. */
        <div className="flex gap-3 h-[calc(100vh-260px)] min-h-[420px]">
          {/* Pane 1 — skill list.  ``scrollbar-overlay`` keeps the scrollbar
           *  hidden until hover and overlays it so the row width never shifts
           *  between scrollable and non-scrollable states. */}
          <div className="w-[240px] shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md p-2" role="listbox" aria-label="Skills">
            {localSkills.map(renderRow)}
            {packageSkills.length > 0 && (
              <div className="mt-2">
                <div className="text-[11px] text-aim font-semibold tracking-wider px-2 py-1.5 mb-1" title={`Skills from ${provider.labels.pluginRegistryName} — read-only`}>
                  {provider.labels.pluginRegistryName.toUpperCase()}
                </div>
                {packageSkills.map(renderRow)}
              </div>
            )}
            {allFiltered.length === 0 && <div className="text-muted/70 text-[12px] italic px-2 py-2">No skills match “{skillFilter}”.</div>}
          </div>

          {/* Panes 2+3 — directory browser, or the edit form */}
          <div className="flex-1 min-w-0 flex flex-col border border-border rounded-md bg-card overflow-hidden">
            {!selectedSkill ? (
              <div className="flex items-center justify-center h-full text-muted text-[13px]">Select a skill to view its files</div>
            ) : detailEditing ? (
              <div className="flex flex-col h-full min-h-0">
                <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-border shrink-0">
                  <span className="text-sm font-mono font-bold text-text-strong truncate">{selectedSkill.key}</span>
                  <div className="flex gap-2 shrink-0">
                    <Btn onClick={() => setDetailEditing(false)}>Cancel</Btn>
                    <Btn primary onClick={() => updateSkill.mutate({ key: selectedSkill.key, content: assembleSkillContent(formData) })}>Save</Btn>
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
                      <Btn disabled={!detailReady} onClick={() => { setDetailEditing(true); setFormData(parseSkillContent(detailContent, selectedSkill.key)) }}>Edit</Btn>
                      <Btn danger onClick={() => { if (confirm(`Delete "${selectedSkill.key}"?`)) deleteSkill.mutate(selectedSkill.key) }}>Delete</Btn>
                    </div>
                  )}
                </div>
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


/** Pending review queue for auto-generated skill candidates.
 *  Self-contained: its own query + approve/dismiss mutations, so it can be
 *  dropped into the Skills tab without touching the main list logic. Renders
 *  nothing when the queue is empty. Each row can be expanded to review the
 *  full SKILL.md body and any bundled script contents BEFORE approving. */
interface PendingSkill {
  slug: string
  name: string
  description: string
  has_scripts: boolean
}
interface PendingDetail {
  name: string
  content: string
  scripts: { filename: string; content: string }[]
}

function PendingCandidateRow({ p, onApprove, onDismiss }: {
  p: PendingSkill
  onApprove: (slug: string) => void
  onDismiss: (slug: string) => void
}) {
  const [open, setOpen] = useState(false)
  const { data: detail } = useQuery<PendingDetail>({
    queryKey: ['skills-pending-detail', p.slug],
    queryFn: () => api.skillPendingDetail(p.slug),
    enabled: open,
  })
  return (
    <div className="p-2 rounded-md border border-border">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-text-strong truncate">
            {p.name}
            {p.has_scripts && (
              <span className="ml-2 text-[10px] px-1.5 py-[1px] rounded-full bg-warn-subtle text-warn font-bold">script</span>
            )}
          </div>
          <div className="text-[12px] text-muted truncate">{p.description}</div>
        </div>
        <Btn onClick={() => setOpen(o => !o)}>{open ? 'Hide' : 'Review'}</Btn>
        <Btn primary disabled={!open || !detail} onClick={() => onApprove(p.slug)}>Approve</Btn>
        <Btn danger onClick={() => { if (confirm(`Dismiss "${p.name}"?`)) onDismiss(p.slug) }}>Dismiss</Btn>
      </div>
      {open && detail && (
        <div className="mt-2 space-y-2">
          <div className="text-[11px] font-semibold text-muted">SKILL.md</div>
          <pre className="text-[11px] whitespace-pre-wrap max-h-64 overflow-auto p-2 rounded bg-bg-elevated border border-border">{detail.content}</pre>
          {(detail.scripts ?? []).map(s => (
            <div key={s.filename}>
              <div className="text-[11px] font-semibold text-warn">scripts/{s.filename}</div>
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
  const { data } = useQuery<{ pending: PendingSkill[] }>({
    queryKey: ['skills-pending'],
    queryFn: () => api.skillsPending(),
    refetchInterval: 30000,
  })
  const pending: PendingSkill[] = data?.pending ?? []
  const approve = useMutation({
    mutationFn: (slug: string) => api.approvePendingSkill(slug),
    onSuccess: (_data, slug) => {
      // Evict the per-slug detail cache so a slug re-staged after this one went
      // live can't surface the promoted candidate's stale detail.
      qc.removeQueries({ queryKey: ['skills-pending-detail', slug] })
      qc.invalidateQueries({ queryKey: ['skills-pending'] })
      qc.invalidateQueries({ queryKey: ['skills'] })
    },
  })
  const dismiss = useMutation({
    mutationFn: (slug: string) => api.dismissPendingSkill(slug),
    onSuccess: (_data, slug) => {
      // Evict the per-slug detail cache too, so a slug re-staged shortly after
      // dismissal can't show the dismissed candidate's stale detail (which a
      // user might then approve without seeing the replacement).
      qc.removeQueries({ queryKey: ['skills-pending-detail', slug] })
      qc.invalidateQueries({ queryKey: ['skills-pending'] })
    },
  })
  if (pending.length === 0) return null
  return (
    <div className="mt-4 mb-2">
      <h4 className="text-sm font-semibold text-text-strong mb-2 flex items-center gap-2">
        Pending review ({pending.length})
        <InfoTip text="Auto-generated skill candidates awaiting your approval. Click Review to inspect the SKILL.md and any bundled script before approving. Approve makes the skill live (and marks scripts executable); dismiss discards it. Nothing goes live until you approve it." />
      </h4>
      <Card>
        <div className="space-y-2">
          {pending.map(p => (
            <PendingCandidateRow
              key={p.slug}
              p={p}
              onApprove={s => approve.mutate(s)}
              onDismiss={s => dismiss.mutate(s)}
            />
          ))}
        </div>
      </Card>
    </div>
  )
}
