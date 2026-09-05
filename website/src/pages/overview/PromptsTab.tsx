import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { ScrollText, Pencil, Trash2, MessageSquare } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector } from '../../store'
import { setPendingInput, switchSlot } from '../../store/chatSlice'
import { api, ApiError } from '../../api/client'
import { Card, CardTitle, Btn, Badge, SearchInput, EmptyState } from '../../components/ui'
import Modal from '../../components/Modal'
import PromptForm, { PROMPT_FILENAME_MAX_BYTES, assemblePromptContent, parsePromptContent, promptNameProblem, type PromptFormData, type PromptScope } from '../../components/PromptForm'
import InfoTip from '../../components/InfoTip'
import ErrorNotice from '../../components/ErrorNotice'
import ListDetailBack from '../../components/ListDetailBack'
import { useSidePanelLeaveGuard } from '../../components/SidePanelLayout'
import { parseErrorCode, findReport, type ErrorReport } from '../../utils/errorReport'
import { useListDetailView } from '../../hooks/useListDetailView'
import { useProvider } from '../../providers'

import { i18nT } from '../../i18n/t'
interface Prompt {
  name: string
  fullName: string
  description: string
  path: string
  package: string
  source: string
}

/** Height math and the `svh` fallback are the shell convention shared with
 *  SkillsTab — see the comment on its own PANE_SHELL_CLASS for why `svh`
 *  rather than `dvh`. Kept identical so the two tabs cannot drift apart. */
const PANE_SHELL_CLASS = 'flex gap-3 -mx-2 md:mx-0 h-[calc(100vh-260px)] supports-[height:100svh]:h-[calc(100svh-260px)] min-h-[420px]'

function SlotPicker({ prompt, onClose }: { prompt: Prompt; onClose: () => void }) {
  const slots = useAppSelector(s => s.dashboard.slots)
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const ref = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onCloseRef.current() }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const send = (slotKey?: string) => {
    dispatch(setPendingInput(`@${prompt.fullName}`))
    if (slotKey) {
      dispatch(switchSlot(slotKey))
      navigate('/chat?autoSend=1')
    } else {
      navigate('/chat?autoSend=1&newSession=1')
    }
    onClose()
  }

  return (
    <div ref={ref} className="absolute right-0 top-full mt-1 z-50 bg-bg-elevated border border-border rounded-lg shadow-lg min-w-[220px] max-h-[240px] overflow-y-auto py-1 animate-slide-in-left">
      <div className="px-3 py-1.5 text-[11px] text-muted uppercase tracking-wider font-semibold">{i18nT('pages.overview.promptsTab.send_to')}</div>
      <div role="button" tabIndex={0} className="px-2 py-1 mx-1 rounded-md cursor-pointer text-[13px] text-accent font-medium hover:bg-bg-hover transition-colors" onClick={() => send()} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); send() } }}>{i18nT('pages.overview.promptsTab.new_chat')}</div>
      {slots.map(s => (
        <div key={s.key} role="button" tabIndex={0} className="px-2 py-1.5 mx-1 rounded-md cursor-pointer text-[13px] hover:bg-bg-hover transition-colors flex items-center gap-2" onClick={() => send(s.key)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); send(s.key) } }}>
          {s.running && <span className="w-1.5 h-1.5 rounded-full bg-green-400 shrink-0" />}
          <span className="truncate">{s.title && s.title !== s.key ? s.title : s.agent || s.key}</span>
        </div>
      ))}
      {slots.length === 0 && <div className="px-3 py-1.5 text-[12px] text-muted italic">{i18nT('pages.overview.promptsTab.no_active_chats')}</div>}
    </div>
  )
}

export default function PromptsTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const { isMobile, showList, showDetail, openDetail, closeDetail } = useListDetailView()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [filter, setFilter] = useState('')
  const [pickerOpen, setPickerOpen] = useState(false)

  const { data: prompts = [], isLoading: loading, error } = useQuery<Prompt[]>({
    queryKey: ['prompts'],
    queryFn: api.prompts,
  })

  const EMPTY_FORM: PromptFormData = { name: '', description: '', scope: 'global', body: '' }
  const [creating, setCreating] = useState(false)
  const [detailEditing, setDetailEditing] = useState(false)
  // Create and edit each own their form state. Sharing one slot let an open
  // editor read the modal's identity (and the reverse blanked a draft), so a
  // Save could write one prompt's body under another prompt's name.
  const [createForm, setCreateForm] = useState<PromptFormData>(EMPTY_FORM)
  const [editForm, setEditForm] = useState<PromptFormData>(EMPTY_FORM)
  // `content` is a DISPLAY copy: the detail endpoint redacts credential and
  // exfiltration patterns, and a failed fetch substitutes placeholder text.
  // Editing seeds from it, so a transformed copy must not be an edit base —
  // saving it would replace the real bytes with the marker.
  const [contentEditable, setContentEditable] = useState(false)
  // WHY the copy is not editable, which decides which caption is honest: a
  // redaction means the file really does hold a credential-shaped token, a
  // fetch failure means we know nothing about its contents. One string for
  // both would tell a user recovering from a network blip that their prompt
  // looks like it contains a secret.
  const [readOnlyReason, setReadOnlyReason] = useState<'redacted' | 'lossy' | 'failed' | null>(null)
  // The failed read's journal report, captured in `load()`'s catch by the
  // error's OWN message. The pane displays localized copy, and a localized
  // string cannot match the journal (it keys on the server's English text),
  // so without carrying the report here the Ask-agent hand-off would lose the
  // structured context — endpoint, HTTP status, backend code — that is the
  // hand-off's whole value.
  const [failedReadReport, setFailedReadReport] = useState<ErrorReport | undefined>(undefined)
  // True from selection until that prompt's own detail response settles. Gates
  // the Edit/Delete row and the content box: neither can say anything honest
  // about a file whose read hasn't answered yet.
  const [detailLoading, setDetailLoading] = useState(false)
  const [mutationError, setMutationError] = useState('')

  // A write's own error text is the server's, and four codes need translating
  // before a user can act on them. Two name the mechanism rather than the
  // control: `no_active_project` says "no active project for local scope", but
  // the control they chose is labelled "This project" and the word "local"
  // appears nowhere in this UI; `content_conflict` names a compare-and-swap the
  // user never saw. The other two are the create path's own refusals of a
  // typed name -- `invalid_name` when nothing survives sanitizing,
  // `name_too_long` when the filename exceeds the byte cap -- and both answer
  // in English without saying which rule was broken. A name of characters
  // outside a-z0-9 earns the first and a multi-byte name earns the second at a
  // third of the length an ASCII one would, so the user least likely to read
  // the English prose is the one most likely to be shown it. The form now
  // catches both client-side, but that sanitizer is a mirror rather than the
  // authority, so these stay the honest answer for a name the preview accepted
  // and the server did not. Every other code's prose is already actionable, so
  // this maps the exceptions rather than building a table that would drift from
  // the handler.
  const writeError = (e: Error) => {
    const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
    // The conflict copy tells the user to reopen the prompt, so the same-row
    // click must actually reload after a 409 — this flag is what arms it.
    if (code === 'content_conflict') conflictRef.current = true
    setMutationError(code === 'no_active_project'
      ? i18nT('pages.overview.promptsTab.no_active_project_hint')
      : code === 'content_conflict'
        ? i18nT('pages.overview.promptsTab.content_conflict_hint')
        : code === 'invalid_name'
          ? i18nT('pages.overview.promptsTab.invalid_name_hint')
          : code === 'name_too_long'
            ? i18nT('pages.overview.promptsTab.name_too_long_hint', { max: PROMPT_FILENAME_MAX_BYTES })
            : e.message)
  }

  // Prefix-invalidates the list AND every cached detail (['prompts', <key>]).
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['prompts'] })

  const createPrompt = useMutation({
    mutationFn: (d: PromptFormData) => api.createPrompt(d.name, assemblePromptContent(d), d.scope),
    onSuccess: () => { setCreating(false); setCreateForm(EMPTY_FORM); setMutationError(''); invalidate() },
    onError: writeError,
  })
  const updatePrompt = useMutation({
    mutationFn: (d: PromptFormData) =>
      api.updatePrompt(d.name, d.scope, assemblePromptContent(d), detailHashRef.current),
    onSuccess: (r: { hash?: string }, vars) => {
      setDetailEditing(false); setMutationError('')
      // Only adopt the saved body if THIS prompt is still the selected one.
      // `content` is shared by the detail pane, so a save that completes after
      // the user moved on would otherwise show A's body under B's name — and
      // editing from there would save A's text into B.
      if (selectedRef.current === writingRef.current) {
        setContent(assemblePromptContent(vars))
        // The server hashed the bytes it wrote; adopting it means an immediate
        // re-edit presents the state this save created, not the one before it.
        // Same guard as `content`: the ref belongs to the selected prompt.
        detailHashRef.current = r.hash || ''
      }
      invalidate()
    },
    onError: writeError,
  })
  const deletePrompt = useMutation({
    mutationFn: (p: Prompt & { scope: PromptScope }) => api.deletePrompt(p.name, p.scope),
    onSuccess: () => { setSelectedKey(null); setMutationError(''); invalidate() },
    onError: writeError,
  })

  const promptKey = (p: Prompt) => `${p.source}:${p.package}/${p.name}`
  const pendingRef = useRef('')
  // Which prompt a save was started for, and which is selected right now. Refs
  // rather than state because a mutation callback closes over the render that
  // fired it and would otherwise compare against a stale selection.
  const writingRef = useRef<string | null>(null)
  // What the editor was seeded with, in assembled form — the zero-edit state.
  const editBaselineRef = useRef('')
  // Hash of the file state the detail pane was seeded from — the edit base a
  // PUT presents for compare-and-swap. A ref, not state: it never renders, and
  // a mutation callback must read the value belonging to the seeding read, not
  // to whichever render fired it.
  const detailHashRef = useRef('')
  // Set by a 409 save; cleared when a fresh read lands. While set, re-clicking
  // the selected row reloads, which is the recovery the conflict copy names.
  const conflictRef = useRef(false)
  const selectedRef = useRef<string | null>(null)
  selectedRef.current = selectedKey

  /** The write scope a prompt can be addressed by, or undefined when it has
   *  none. `source` is a free-form string on the wire; only the two user
   *  directories are addressable, so anything else (a package SOP, or a source
   *  this build does not know) must fall back to the unscoped read and offer no
   *  write actions rather than be cast into a scope it is not. */
  const scopeOf = (p: Prompt): PromptScope | undefined =>
    p.source === 'global' || p.source === 'local' ? p.source : undefined

  const sf = useCallback(
    (p: Prompt) =>
      !filter ||
      (p.name + ' ' + p.fullName + ' ' + p.description + ' ' + p.package)
        .toLowerCase()
        .includes(filter.toLowerCase()),
    [filter],
  )

  const packagePrompts = useMemo(() => prompts.filter(p => p.source === 'package'), [prompts])
  const userPrompts = useMemo(() => prompts.filter(p => p.source !== 'package'), [prompts])
  const filteredUser = useMemo(() => userPrompts.filter(sf), [userPrompts, sf])
  const filteredPackage = useMemo(() => packagePrompts.filter(sf), [packagePrompts, sf])
  const allFiltered = useMemo(() => [...filteredUser, ...filteredPackage], [filteredUser, filteredPackage])

  const grouped = useMemo(() => {
    const g: Record<string, Prompt[]> = {}
    for (const p of filteredPackage) (g[p.package || 'unknown'] ||= []).push(p)
    return Object.entries(g).sort(([a], [b]) => a.localeCompare(b))
  }, [filteredPackage])

  // Selection is derived from the UNFILTERED list so that typing in the filter
  // does not blank a detail pane whose prompt is merely hidden by the query.
  const selected = useMemo(
    () => prompts.find(p => promptKey(p) === selectedKey) ?? null,
    [prompts, selectedKey],
  )

  const load = useCallback(async (p: Prompt) => {
    const key = promptKey(p)
    pendingRef.current = key
    // Drop the previous prompt's copy BEFORE awaiting. Between the click and
    // the response the header already names the new prompt, so leaving the old
    // content editable would let Edit seed from it and Save write one prompt's
    // body under another's name. Editing re-enables only when this prompt's
    // own response lands.
    setContent('')
    setContentEditable(false)
    setReadOnlyReason(null)
    setFailedReadReport(undefined)
    // Named loading state, not inferred from empty content: without it the
    // header renders a disabled "Edit unavailable" for the whole fetch — a
    // label asserting a fact the system doesn't hold yet — and the content
    // box shows the prompt as empty. Both header actions and content wait
    // behind this flag until the response settles either way.
    setDetailLoading(true)
    // Scope-qualified when the prompt HAS an addressable scope: a global and a
    // project prompt can share a stem, and an unscoped read (and an unscoped
    // cache key) would hand back whichever the server found first — which a
    // following Save would then write under the OTHER one's scope. Anything
    // without a scope keeps the package-qualified path and the plain read.
    const scope = scopeOf(p)
    const detailPath = scope ? p.name : (p.package ? `${p.package}/${p.name}` : p.name)
    try {
      const d = await queryClient.fetchQuery({
        queryKey: ['prompts', 'detail', p.source, detailPath],
        queryFn: () => scope ? api.promptDetail(detailPath, scope) : api.promptDetail(detailPath),
        // Opt out of the client's global `staleTime: Infinity`, which is tuned
        // for data the server pushes invalidations for. Nothing pushes when a
        // prompt file changes underneath us — an editor, a git checkout, another
        // Kiro Crew window — so an indefinitely fresh cache entry would seed the
        // editor from a copy older than the file, and Save would write it back
        // over the newer content. Our own writes already invalidate (the
        // `['prompts']` prefix covers these keys); this covers the edits we
        // cannot see. Same rule as `redacted`/`lossy`: a copy that is not the
        // file must not become an edit base.
        staleTime: 0,
      })
      if (pendingRef.current !== key) return // stale response
      setContent(d.content || '')
      // The edit base a later PUT will present. From the same response that
      // seeds the pane, so hash and content cannot describe different states.
      detailHashRef.current = d.hash || ''
      // Whatever conflicted before, this pane now shows the file as it is.
      conflictRef.current = false
      // Either transformation disqualifies the copy as an edit base, and the
      // caption names which one it was: a redaction means the file really does
      // hold a credential-shaped token, a lossy decode means its bytes are not
      // UTF-8. Saying "filtered for safety" for the second would be a lie.
      setContentEditable(!d.redacted && !d.lossy)
      setReadOnlyReason(d.redacted ? 'redacted' : d.lossy ? 'lossy' : null)
      setDetailLoading(false)
    } catch (e) {
      if (pendingRef.current !== key) return
      setContent('')
      setContentEditable(false)
      setReadOnlyReason('failed')
      setFailedReadReport(e instanceof Error ? findReport(e.message) : undefined)
      setDetailLoading(false)
    }
  }, [queryClient])

  /** True when the open editor holds work the user has not saved.
   *
   *  Compared against a baseline captured when the editor OPENED, not against
   *  the file's own text: assemble canonicalizes what it writes (the blank line
   *  after the fence, the spacing of the description field), so a prompt whose
   *  on-disk shape differs from that would read as dirty with zero edits and
   *  pop a discard-confirm the user never earned. */
  const editDirty = useCallback(() =>
    detailEditing && assemblePromptContent(editForm) !== editBaselineRef.current,
  [detailEditing, editForm])

  /** True when the create form holds anything typed. Unlike editDirty there is
   *  no baseline to compare against — the form always opens empty, so any
   *  non-empty field IS typed work. Scope is excluded: flipping a radio is a
   *  choice, not content, and alone it is not worth an "are you sure". */
  const createDirty = () =>
    !!(createForm.name.trim() || createForm.description.trim() || createForm.body.trim())

  // The host page renders this tab conditionally (`{tab === 'prompts' &&
  // <PromptsTab />}`), so clicking another tab in the rail UNMOUNTS the pane and
  // takes the open editor with it. Every in-pane exit already asks before
  // destroying typed work — the editor's Cancel, the modal's Cancel and X, and
  // selecting a different row — but the rail click belongs to the shell, so
  // until it consults this guard it was the one exit that discarded a draft in
  // silence. Same copy as those confirms, because it is the same question.
  //
  // Deliberately `editDirty()` alone, NOT the create form: the create modal
  // renders a full-viewport backdrop above the rail, so while a create draft is
  // open no rail tab (and no mobile back bar) can be clicked at all — measured,
  // not assumed: a capture run driving the rail with the modal open cannot reach
  // the button, the backdrop takes the click. Adding `createDirty()` here would
  // guard an unreachable path, and unqualified it would be actively wrong, since
  // discarding a create leaves `createForm` holding the abandoned text (only
  // OPENING the modal resets it) and every later tab switch would ask about a
  // draft the user already threw away. If the modal ever stops covering the
  // rail, the create form needs this same guard.
  //
  // The second argument is the same dirtiness as a value, which is what arms the
  // browser Back guard: that gesture reaches no component, so it has to be armed
  // before the press rather than asked at it. Same predicate as the guard above,
  // so the two cannot disagree about whether there is a draft.
  useSidePanelLeaveGuard(
    () => !editDirty() || confirm(i18nT('pages.overview.promptsTab.discard_unsaved_changes')),
    editDirty(),
  )

  // The guard above is consulted by every exit that has been wired to ask: this
  // layout's own rail and mobile back bar, the global sidebar, the command
  // palette, the notification panel's jumps, and — through the stake published
  // alongside it — the browser's own Back/Forward button. A reload, a tab close,
  // or navigating the browser off the dashboard entirely destroys the same
  // draft, and `beforeunload` is the only thing the platform offers there — the
  // same idiom, for the same reason, as ArtifactDetailPage, PapyrusPage,
  // MdNotebook and ChatPage.
  //
  // Still NOT covered, and deliberately named rather than implied: an in-app
  // `navigate()` caller that neither asks nor runs inside `useGuardedLeave`.
  // Coverage remains opt-in per surface, and forgetting fails silently; the
  // structural retirement of that model (a data router so `useBlocker` becomes
  // available, or lifting the draft so no exit destroys it) is tracked in #8010.
  useEffect(() => {
    if (!editDirty()) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
    // `editDirty` is a useCallback over detailEditing + editForm, which is what
    // it reads; the baseline is a ref, so it cannot be a dependency and does not
    // need to be — it is only written when the editor OPENS, which flips
    // detailEditing in the same commit.
  }, [editDirty])

  const select = (p: Prompt) => {
    // A write in flight owns the panes it is about to update. Switching now
    // would let its completion land on a different prompt, so the row click is
    // ignored until it settles (writes here are a single small file).
    if (updatePrompt.isPending || deletePrompt.isPending) return
    const key = promptKey(p)
    // Re-clicking the row that is already selected is not a selection change:
    // it must not tear down the editor. Without this, the dirty guard below
    // short-circuits on `key !== selectedKey` and the draft is discarded with
    // no confirm — the exact loss the guard exists to prevent. On a narrow
    // viewport the click still means "show me the detail pane again".
    //
    // The exception is a read that FAILED, or a save that CONFLICTED: in both
    // the pane's own copy tells the user to try the prompt again, so a no-op
    // here is an instructed recovery that doesn't work. Never while the editor
    // is open, though — reloading under a live draft would swap in the new
    // hash beneath the old text, and the next Save would present a base it was
    // not written against, silently overwriting the very edit that conflicted.
    if (key === selectedKey) {
      openDetail()
      if (!detailEditing && (readOnlyReason === 'failed' || conflictRef.current)) void load(p)
      return
    }
    // Leaving a dirty editor destroys typed work, and the row targets around
    // it are large. The create modal already guards its body this way
    // (guardAccidentalDismiss), so the editor — the same kind of typed work —
    // asks too rather than discarding on one misclick.
    if (key !== selectedKey && editDirty()
        && !confirm(i18nT('pages.overview.promptsTab.discard_unsaved_changes'))) return
    setDetailEditing(false)
    setEditForm(EMPTY_FORM)
    setMutationError('')
    setPickerOpen(false)
    setSelectedKey(key)
    openDetail()
    void load(p)
  }

  // Keep a desktop detail pane populated: with both panes always on screen, an
  // empty right-hand side on first paint reads as a broken tab. Skipped while
  // editing so a re-render cannot yank the pane out from under a draft.
  useEffect(() => {
    if (detailEditing) return
    if (allFiltered.length === 0) { if (selectedKey !== null) setSelectedKey(null); return }
    if (!selectedKey || !allFiltered.some(p => promptKey(p) === selectedKey)) {
      const first = allFiltered[0]
      setSelectedKey(promptKey(first))
      void load(first)
    }
  }, [allFiltered, selectedKey, detailEditing, load])

  const renderRow = (p: Prompt) => {
    const pk = promptKey(p)
    const isSel = pk === selectedKey
    return (
      <div
        key={pk}
        role="option"
        aria-selected={isSel}
        tabIndex={0}
        onClick={() => select(p)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(p) } }}
        className={`flex flex-col gap-0.5 px-3 py-2.5 rounded-md cursor-pointer mb-1 transition-colors ${
          isSel ? 'list-selected bg-accent-subtle' : 'bg-bg-elevated hover:bg-bg-hover'
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[13px] font-semibold text-text font-mono truncate flex-1">@{p.name}</span>
          {p.source !== 'package' && <Badge variant="warn">{p.source}</Badge>}
        </div>
        {p.description && <span className="text-[11px] text-muted truncate">{p.description}</span>}
      </div>
    )
  }

  const detailScope = selected ? scopeOf(selected) : undefined

  // The three detail-pane operations, each built once. The variants below place
  // them; they never redefine them.
  const openEditor = () => {
    if (!selected || !detailScope) return
    setMutationError('')
    const seeded = parsePromptContent(content, selected.name, detailScope)
    editBaselineRef.current = assemblePromptContent(seeded)
    setEditForm(seeded)
    setDetailEditing(true)
  }
  const confirmDelete = () => {
    if (!selected || !detailScope) return
    if (confirm(i18nT('pages.overview.promptsTab.delete_confirm', { name: selected.name }))) {
      deletePrompt.mutate({ ...selected, scope: detailScope })
    }
  }
  const useInChatBtn = selected ? (
    <div className="relative">
      <Btn onClick={() => setPickerOpen(!pickerOpen)}><MessageSquare className="lucide-inline" /> {i18nT('pages.overview.promptsTab.use_in_chat')}</Btn>
      {pickerOpen && <SlotPicker prompt={selected} onClose={() => setPickerOpen(false)} />}
    </div>
  ) : null
  const editBtn = (
    <Btn disabled={!contentEditable} onClick={openEditor}>
      <Pencil size={13} className="shrink-0" />
      {contentEditable ? i18nT('pages.overview.promptsTab.edit') : i18nT('pages.overview.promptsTab.edit_unavailable')}
    </Btn>
  )
  const deleteBtn = (
    <Btn danger disabled={deletePrompt.isPending} onClick={confirmDelete}>
      <Trash2 size={13} className="shrink-0" />
      {i18nT('pages.overview.promptsTab.delete')}
    </Btn>
  )

  return (<>
    <Card>
      <CardTitle><ScrollText className="lucide-inline" /> {i18nT('pages.overview.promptsTab.prompts')} <InfoTip text={i18nT('pages.overview.promptsTab.saved_prompts_from', { registry: provider.labels.pluginRegistryName.toLowerCase() })} /> <span className="ml-auto"><Btn primary onClick={() => { setMutationError(''); setCreateForm(EMPTY_FORM); setCreating(true) }}>{i18nT('pages.overview.promptsTab.create_new_prompt')}</Btn></span></CardTitle>
      <p className="text-muted text-[13px] mb-3 leading-relaxed">
        {i18nT('pages.overview.promptsTab.invoke_in_chat')} <code className="text-[12px]">{i18nT('pages.overview.promptsTab.agent_sop_name')}</code> {i18nT('pages.overview.promptsTab.or')} <code className="text-[12px]">{i18nT('pages.overview.promptsTab.prompts_get_name')}</code>{i18nT('pages.overview.promptsTab.prompts_are_loaded_on_demand_they_don_t_consume')}
      </p>
      {prompts.length > 0 && (
        <div className="mb-3">
          <SearchInput placeholder={i18nT('pages.overview.promptsTab.filter_prompts')} value={filter} onChange={e => setFilter(e.target.value)} />
        </div>
      )}
      {loading && <p className="text-muted italic text-sm px-3 py-4">{i18nT('pages.overview.promptsTab.loading_prompts')}</p>}
      {error && <p className="text-danger text-sm px-3 py-4">{error.message || i18nT('pages.overview.promptsTab.failed_to_load_prompts')}</p>}

      {/* The empty state now names the affordance on this page rather than the
          filesystem path this feature replaces. */}
      {prompts.length === 0 && !loading && !error ? (
        <EmptyState
          icon={<ScrollText className="lucide-inline" />}
          title={i18nT('pages.overview.promptsTab.empty_title')}
          subtitle={i18nT('pages.overview.promptsTab.empty_subtitle')}
          action={<Btn primary onClick={() => { setMutationError(''); setCreateForm(EMPTY_FORM); setCreating(true) }}>{i18nT('pages.overview.promptsTab.create_new_prompt')}</Btn>}
        />
      ) : prompts.length > 0 && (
        /* List-detail: prompt list on the left, the selected prompt's content
         *  (or its editor) on the right — the shape SkillsTab uses. */
        <div className={PANE_SHELL_CLASS}>
          {showList && <div className={`${isMobile ? 'w-full' : 'w-[240px]'} shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md p-2`} role="listbox" aria-label={i18nT('pages.overview.promptsTab.prompts')}>
            {filteredUser.length > 0 && (
              <div className="text-[11px] text-muted font-semibold tracking-wider px-2 py-1.5 mb-1">
                {i18nT('pages.overview.promptsTab.user_prompts_count', { count: filteredUser.length })}
              </div>
            )}
            {filteredUser.map(renderRow)}
            {grouped.map(([pkg, items]) => (
              <div key={pkg} className="mt-2">
                <div className="text-[11px] text-aim font-semibold tracking-wider px-2 py-1.5 mb-1">{pkg.toUpperCase()}</div>
                {items.map(renderRow)}
              </div>
            ))}
            {allFiltered.length === 0 && <div className="text-muted/70 text-[12px] italic px-2 py-2">{i18nT('pages.overview.promptsTab.no_prompts_match_query', { query: filter })}</div>}
          </div>}

          {showDetail && <div className="flex-1 min-w-0 flex flex-col border border-border rounded-md bg-card overflow-hidden">
            {!selected ? (
              <div className="flex items-center justify-center h-full text-muted text-[13px]">{i18nT('pages.overview.promptsTab.select_a_prompt')}</div>
            ) : (
              <div className="flex flex-col h-full min-h-0">
                {/* Back is its own row rather than joining the action row: the
                    row already carries the send control on the left and the
                    edit/destroy pair on the right. */}
                {isMobile && (
                  <div className="px-4 pt-2.5 shrink-0">
                    <ListDetailBack label={i18nT('pages.overview.promptsTab.prompts')} onBack={closeDetail} />
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-2.5 border-b border-border shrink-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-mono font-bold text-text-strong truncate">@{selected.fullName}</span>
                    {selected.source === 'package'
                      ? <Badge variant="ok">{i18nT('pages.overview.promptsTab.package')}</Badge>
                      : <Badge variant="warn">{selected.source}</Badge>}
                    {/* Send sits with the identity it acts on, not with the
                        edit/destroy pair at the far end. Two groups of one and
                        two, so neither holds the three peer buttons
                        `max-two-buttons-per-row` exists to prevent — and the
                        split is what carries the ranking that rule says peer
                        buttons lack. `ArtifactPanel.tsx` ships the same shape.
                        Hidden while editing: there is nothing to send but an
                        unsaved draft. */}
                    {!detailEditing && useInChatBtn}
                  </div>
                  {detailEditing ? (
                    <div className="flex gap-2 shrink-0">
                      {/* Cancel gets the same dirty guard as switching rows: the
                          two destroy the same draft, and this one sits a pixel
                          from Save. */}
                      <Btn disabled={updatePrompt.isPending} onClick={() => {
                        if (editDirty() && !confirm(i18nT('pages.overview.promptsTab.discard_unsaved_changes'))) return
                        setDetailEditing(false); setMutationError('')
                      }}>{i18nT('pages.overview.promptsTab.cancel')}</Btn>
                      <Btn primary disabled={!editForm.body.trim() || updatePrompt.isPending} onClick={() => { writingRef.current = selectedKey; updatePrompt.mutate(editForm) }}>{i18nT('pages.overview.promptsTab.save')}</Btn>
                    </div>
                  ) : detailScope && !detailLoading && (
                    <div className="flex gap-2 shrink-0 items-center">
                      {editBtn}
                      {deleteBtn}
                    </div>
                  )}
                </div>
                <div className="px-4 py-1.5 border-b border-border shrink-0">
                  <code className="text-muted text-[12px] truncate block">{selected.path}</code>
                </div>
                {/* One error surface for the whole pane, so a failed DELETE —
                    which has no form of its own — reports somewhere instead of
                    leaving the user who just authorized it with silence. */}
                {mutationError && <p className="text-danger text-[12px] px-4 pt-2">{mutationError}</p>}
                <div className="flex-1 min-h-0 overflow-y-auto p-4">
                  {detailEditing ? (
                    <PromptForm data={editForm} onChange={setEditForm} hideIdentity />
                  ) : detailLoading ? (
                    /* Not an empty <pre>: an empty box says "this prompt has no
                       content", which is a claim about the file, not about the
                       fetch. */
                    <p className="text-muted text-[12px] italic">{i18nT('pages.overview.promptsTab.loading_content')}</p>
                  ) : (<>
                    {(readOnlyReason === 'redacted' || readOnlyReason === 'lossy') && (
                      <p className="text-muted text-[11px] mb-1 italic">
                        {readOnlyReason === 'redacted'
                          ? i18nT('pages.overview.promptsTab.read_only_redacted')
                          : i18nT('pages.overview.promptsTab.read_only_lossy')}
                      </p>
                    )}
                    {/* `failed` is the one reason that IS an error — its value
                        comes from `load()`'s catch — so it renders through
                        ErrorNotice per errors-use-error-notice, keeping the
                        journal's structured context and the agent hand-off.
                        `redacted`/`lossy` stay a caption: they arrive on the
                        SUCCESS path and describe the file's content, not a
                        failure. askAgent is safe here: this block renders only
                        while `detailEditing` is false, so no editor draft
                        lives in the subtree the hand-off unmounts, and the
                        create modal's backdrop covers the pane while it holds
                        typed work.

                        Only `failed` gets Retry, too: a redacted or lossy copy
                        is the server's answer about the file's CONTENT and
                        asking again returns the same answer, but a failed
                        fetch is an answer about the NETWORK, and the recovery
                        path already exists — re-clicking the selected row
                        re-invokes the load. Nothing taught that, so the pane
                        dead-ended until the user re-clicked by accident. The
                        button calls the same `load` the row re-click calls;
                        no new fetch path. */}
                    {readOnlyReason === 'failed' && (<>
                      <ErrorNotice
                        message={i18nT('pages.overview.promptsTab.read_only_failed')}
                        report={failedReadReport}
                        askAgent
                        className="mb-2"
                      />
                      <Btn className="mb-2 text-[12px]" onClick={() => void load(selected)}>
                        {i18nT('pages.overview.promptsTab.retry_read')}
                      </Btn>
                    </>)}
                    <pre className="bg-bg-elevated border border-border rounded-md p-3 font-mono text-[13px] text-text overflow-x-auto whitespace-pre-wrap leading-normal">{content}</pre>
                  </>)}
                </div>
              </div>
            )}
          </div>}
        </div>
      )}
    </Card>
    {/* Create Prompt Modal. guardAccidentalDismiss covers backdrop/Escape, but
        the footer Cancel and the X are EXPLICIT closes it does not reach — and
        they destroy the same typed work one misclick from the adjacent Create.
        So they take the same dirty confirm as the editor's Cancel: ask only
        when a field holds something, close silently when the form is empty. */}
    <Modal open={creating} onClose={() => {
      if (createPrompt.isPending) return
      if (createDirty() && !confirm(i18nT('pages.overview.promptsTab.discard_unsaved_changes'))) return
      setCreating(false); setMutationError('')
    }} title={i18nT('pages.overview.promptsTab.create_new_prompt')} maxWidth={560} guardAccidentalDismiss footer={<>
      <Btn disabled={createPrompt.isPending} onClick={() => {
        if (createDirty() && !confirm(i18nT('pages.overview.promptsTab.discard_unsaved_changes'))) return
        setCreating(false); setMutationError('')
      }}>{i18nT('pages.overview.promptsTab.cancel')}</Btn>
      {/* Gated on the rule the SERVER applies, not on `.trim()`: a name whose
          every character is outside a-z0-9 is non-empty here but empty there,
          and a multi-byte name can exceed the filename byte cap while looking
          short — both used to be sent as requests that could only 400. The
          form's hint says which rule the name broke, which is what keeps a
          disabled button from being a dead end. */}
      <Btn primary disabled={!createForm.name.trim() || promptNameProblem(createForm.name) !== null || !createForm.body.trim() || createPrompt.isPending} onClick={() => createPrompt.mutate(createForm)}>{i18nT('pages.overview.promptsTab.create')}</Btn>
    </>}>
      <PromptForm data={createForm} onChange={setCreateForm} />
      {mutationError && <p className="text-danger text-[12px] mt-2">{mutationError}</p>}
    </Modal>
  </>)
}
