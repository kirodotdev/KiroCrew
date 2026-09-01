import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { Zap, FolderOpen, ChevronRight, TriangleAlert, Check } from 'lucide-react'
import Modal from './Modal'
import { Input, Btn } from './ui'
import ProjectPicker from './ProjectPicker'
import SimpleSelect from './SimpleSelect'
import { FOLDER_COLOR_PALETTE } from './folderColorCatalog'
import { useImeGuard } from '../hooks/useImeGuard'
import { resolveFolderProjectDir } from '../utils/folderAgent'
import { ChatFolder, ChatTag } from '../types'
import { i18nT } from '../i18n/t'

/** The folder fields this modal owns. */
export type FolderConfigField = 'name' | 'color' | 'projectDir' | 'defaultAgent' | 'tags'

export interface FolderConfigDraft {
  name: string
  /** Palette hex for the folder glyph tint; '' = default gray. */
  color: string
  projectDir: string
  defaultAgent: string
  /** Tag ids the folder carries; copied onto new chats filed into it. */
  tags: string[]
  /** Fields the USER actually edited, measured against what the modal opened
   *  with. The caller must build its PATCH from this rather than diffing the
   *  draft against live cache: a field another client changed while the modal
   *  was open differs from the draft without the user having touched it, and
   *  re-sending the stale value silently reverts it. */
  touched: FolderConfigField[]
}

interface Props {
  open: boolean
  onClose: () => void
  /** 'create' collects a new folder; 'edit' amends `folder`. */
  mode: 'create' | 'edit'
  /** create: parent folder id ('' = top level). Ignored when mode='edit'. */
  parentId?: string
  /** edit: the folder being amended. Required when mode='edit'. */
  folder?: ChatFolder
  /** Every folder — powers the read-only destination breadcrumb. */
  folders: ChatFolder[]
  installedAgents: { name: string }[]
  /** Global default agent, shown as what an empty agent choice falls back to. */
  globalDefaultAgent?: string
  /** The tag vocabulary, powering the folder-tag picker. Empty/absent hides the
   *  picker entirely — a folder can only carry tags that already exist. */
  availableTags?: ChatTag[]
  /** True when the chat-tags query FAILED (vs still loading) — renders an
   *  error line instead of asserting an in-progress state indefinitely. */
  availableTagsFailed?: boolean
  /** Retries the failed tag-vocabulary query in place — rendered as an inline
   *  Retry action on the error line so recovery never requires dismissing the
   *  modal (closing would discard a mid-draft form). */
  onRetryTags: () => void
  /** Resolves on a persisted save; REJECTS on failure so the modal can stay
   *  open with the draft intact and surface the reason. */
  onSubmit: (draft: FolderConfigDraft) => Promise<void>
}

/** Ancestor chain for `id`, outermost first. Cycle-guarded like the sidebar's
 *  own folder walks — a corrupt parent_id must not spin. */
function ancestorChain(folders: ChatFolder[], id: string | undefined): ChatFolder[] {
  const out: ChatFolder[] = []
  const seen = new Set<string>()
  let cur = id ? folders.find(f => f.id === id) : undefined
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    out.unshift(cur)
    cur = cur.parent_id ? folders.find(f => f.id === cur!.parent_id) : undefined
  }
  return out
}

const EMPTY: FolderConfigDraft = { name: '', color: '', projectDir: '', defaultAgent: '', tags: [], touched: [] }

/** Set-equality on two tag-id lists (order-insensitive): the picker toggles
 *  membership, so "changed?" is about which ids are present, not their order. */
function sameTags(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const s = new Set(a)
  return b.every(id => s.has(id))
}

/**
 * One modal for both "New folder" and "Folder settings".
 *
 * Consolidates what used to be four surfaces: the inline name-only create input,
 * the ⋯-menu default-agent select, the ⋯-menu emoji grid, and the ⋯-menu
 * "Link project directory" ProjectPicker launch.
 *
 * The parent folder is deliberately NOT an input. Every entry point already
 * fixes it (root lane, column lane, or a specific folder's "New subfolder"), so
 * offering a picker would let the user contradict where they clicked. It is
 * restated as a read-only breadcrumb instead, because a centred modal loses the
 * spatial cue the inline input got for free from its own indentation.
 */
export default function FolderConfigModal({
  open, onClose, mode, parentId, folder, folders, installedAgents, globalDefaultAgent, availableTags, availableTagsFailed, onRetryTags, onSubmit,
}: Props) {
  const [draft, setDraft] = useState<FolderConfigDraft>(EMPTY)
  const [pickerOpen, setPickerOpen] = useState(false)
  // The backend rejects a free-typed project_dir (not absolute / not an existing
  // directory / sensitive path) with a 400. Submit used to be fire-and-forget,
  // so a rejection closed the modal and threw the whole draft away with no
  // feedback. Hold the modal open until the save actually lands.
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState('')
  // What the draft looked like when the modal opened — the baseline for
  // "has the user actually typed something worth protecting?".
  const seedRef = useRef<FolderConfigDraft>(EMPTY)
  const browseRef = useRef<HTMLButtonElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  // A folder name is prime IME territory (the sidebar's inline input it replaces
  // guarded this too). Without the guard, the Enter that COMMITS a Chinese /
  // Japanese / Korean composition also submits the form — creating a folder
  // named after a half-typed word.
  const ime = useImeGuard()

  // Re-seed whenever the modal opens (or retargets to a DIFFERENT folder) so a
  // previous session's draft never leaks into the next open.
  //
  // Keyed on the folder's ID, never the object identity: `folder` is a fresh
  // object every time the chat-folders cache changes, and a rejected edit
  // produces three such changes in a row (optimistic write -> rollback ->
  // invalidate). Depending on identity re-ran this effect mid-failure and
  // re-seeded from the persisted folder, erasing the very draft the
  // keep-open-on-error fix exists to preserve.
  const folderRef = useRef(folder)
  folderRef.current = folder
  // Read through a ref for the same reason as `folderRef`: the vocabulary must
  // not be an effect dependency, or a tag edit elsewhere would re-seed and
  // erase an open draft.
  const availableTagsRef = useRef(availableTags)
  availableTagsRef.current = availableTags
  const seedKey = mode === 'edit' ? folder?.id : ''
  useEffect(() => {
    if (!open) return
    const f = folderRef.current
    // Seed only ids that exist in the current vocabulary — but ONLY when the
    // vocabulary is actually known. `availableTags` is undefined while the
    // tags query is unresolved; filtering against that as if it were an empty
    // vocabulary would seed a partial list on a cold load, and the next save
    // would silently delete the folder's existing tags. Unknown vocabulary
    // keeps the raw ids; the submit-time prune below (which runs once the
    // vocabulary has resolved) still clears genuinely dangling ids, so a save
    // never 400s over a reference the picker cannot display.
    const known = Array.isArray(availableTagsRef.current)
    const vocab = new Set((availableTagsRef.current ?? []).map(t => t.id))
    const seeded: FolderConfigDraft = mode === 'edit' && f
      ? {
        name: f.name ?? '',
        color: f.color ?? '',
        projectDir: f.project_dir ?? '',
        defaultAgent: f.default_agent ?? '',
        tags: Array.isArray(f.tags) ? (known ? f.tags.filter(t => vocab.has(t)) : [...f.tags]) : [],
        touched: [],
      }
      : EMPTY
    setDraft(seeded)
    seedRef.current = seeded
    setPickerOpen(false)
    setSaving(false); setSaveErr('')
  }, [open, mode, seedKey])

  // Focus the name field on open. rAF + preventScroll for the same reason the
  // sidebar's inline inputs need it: these open from a Radix menu, whose teardown
  // otherwise wins the focus race and yanks the scroll container sideways.
  useEffect(() => {
    if (!open) return
    const raf = requestAnimationFrame(() => nameRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(raf)
  }, [open])

  // Destination: for create, the parent chain plus a "new folder" leaf. For edit,
  // the folder's own path with itself as the leaf.
  const chain = useMemo(
    () => ancestorChain(folders, mode === 'edit' ? folder?.parent_id : parentId),
    [folders, mode, folder?.parent_id, parentId]
  )

  // An empty project directory means "inherit", and inheritance is real:
  // resolveFolderProjectDir walks up ancestors. So show what WOULD be inherited
  // as placeholder text rather than pre-filling it — pre-filling would write a
  // duplicate explicit value and silently break the link to the ancestor.
  const inheritedDir = useMemo(() => {
    const from = mode === 'edit' ? folder?.parent_id : parentId
    return from ? resolveFolderProjectDir(folders, from) : undefined
  }, [folders, mode, folder?.parent_id, parentId])

  const trimmedName = draft.name.trim()
  const canSubmit = trimmedName.length > 0

  // A folder can reference an agent that is no longer installed (uninstalled or
  // renamed). Without an option for it the select falls back to showing the
  // first entry — "None" — and Save would then write default_agent:'' and
  // silently destroy the folder's configuration. Keep the orphan selectable so
  // it round-trips, flagged so the user knows why it isn't running.
  const orphanAgent = draft.defaultAgent && !installedAgents.some(a => a.name === draft.defaultAgent)
    ? draft.defaultAgent
    : ''

  // Values and display labels as two PARALLEL arrays, orphan first so it keeps
  // the position its <option> held. The '' ("None" / "Inherit (x)") row is
  // SimpleSelect's `clearLabel` rather than a member of these arrays.
  const agentNames = installedAgents.map(a => a.name)
  const agentOptions = orphanAgent ? [orphanAgent, ...agentNames] : agentNames
  const agentOptionLabels = orphanAgent
    ? [i18nT('components.folderConfigModal.agent_not_installed', { agent: orphanAgent }), ...agentNames]
    : agentNames

  const submit = useCallback(async () => {
    if (!canSubmit || saving) return
    const seeded = seedRef.current
    // THE tag-payload invariant: `tags` enters the PATCH only when the user
    // actually toggled a chip (draft differs from what this modal seeded).
    // A rename-only save must omit `tags` entirely — sending any list would
    // overwrite tags another client added to the folder while this modal sat
    // open. No client-side dangling-id prune is needed: the folder endpoint
    // silently filters unknown ids exactly like the slot-tags endpoint it
    // mirrors, so a stale reference is shed by the server on save and can
    // never 400 the folder.
    const tagsEdited = !sameTags(draft.tags, seeded.tags)
    const edited: FolderConfigField[] = []
    if (trimmedName !== seeded.name) edited.push('name')
    if (draft.color !== seeded.color) edited.push('color')
    if (draft.projectDir !== seeded.projectDir) edited.push('projectDir')
    if (draft.defaultAgent !== seeded.defaultAgent) edited.push('defaultAgent')
    if (tagsEdited) edited.push('tags')
    setSaving(true); setSaveErr('')
    try {
      await onSubmit({ ...draft, name: trimmedName, touched: edited })
    } catch (e) {
      // Stay open, keep every field, and say why.
      setSaveErr(e instanceof Error && e.message ? e.message : i18nT('components.folderConfigModal.save_failed'))
    } finally {
      setSaving(false)
    }
  }, [canSubmit, saving, draft, trimmedName, onSubmit])


  // The inline input this replaced held ONE field; the modal holds four, so an
  // accidental backdrop graze now costs real work. Guard the accidental paths
  // while the draft differs from what it opened with — Cancel and X still close.
  const seed = seedRef.current
  const touched: FolderConfigField[] = []
  if (draft.name !== seed.name) touched.push('name')
  if (draft.color !== seed.color) touched.push('color')
  if (draft.projectDir !== seed.projectDir) touched.push('projectDir')
  if (draft.defaultAgent !== seed.defaultAgent) touched.push('defaultAgent')
  if (!sameTags(draft.tags, seed.tags)) touched.push('tags')
  const isDirty = touched.length > 0

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        guardAccidentalDismiss={isDirty || saving}
        maxWidth={480}
        title={mode === 'create' ? i18nT('components.folderConfigModal.new_folder') : i18nT('components.folderConfigModal.folder_settings')}
        footer={
          <>
            <span className="mr-auto text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.enter_to_submit')}</span>
            <Btn onClick={onClose} disabled={saving}>{i18nT('components.folderConfigModal.cancel')}</Btn>
            <Btn primary disabled={!canSubmit || saving} data-testid="folder-config-submit" onClick={submit}>
              {mode === 'create' ? i18nT('components.folderConfigModal.create_folder') : i18nT('components.folderConfigModal.save_changes')}
            </Btn>
          </>
        }
      >
        <div className="flex flex-col gap-4">
          {saveErr && (
            <div data-testid="folder-config-error" role="alert"
              className="flex items-start gap-2 text-[11.5px] text-text bg-danger-subtle border border-danger rounded-lg px-3 py-2">
              <TriangleAlert size={13} className="shrink-0 mt-[1px] text-danger" />
              <span className="min-w-0 break-words">{saveErr}</span>
            </div>
          )}

          {/* Read-only destination. Not an input: the entry point already fixed it. */}
          <div data-testid="folder-config-destination" className="flex items-center gap-1.5 flex-wrap text-[11.5px] text-muted bg-bg-accent border border-border rounded-lg px-3 py-2">
            <span className="text-text font-medium">{i18nT('components.folderConfigModal.top_level')}</span>
            {chain.map(f => (
              <span key={f.id} className="flex items-center gap-1.5">
                <ChevronRight size={11} className="text-muted-strong shrink-0" />
                <span className="text-text font-medium truncate max-w-[140px]">{f.name}</span>
              </span>
            ))}
            <ChevronRight size={11} className="text-muted-strong shrink-0" />
            <span className="text-accent font-semibold truncate max-w-[160px]">
              {trimmedName || (mode === 'create'
                ? i18nT('components.folderConfigModal.new_folder_leaf')
                : folder?.name)}
            </span>
          </div>

          {/* Name. The folder's identity mark is a palette color, applied to
           *  the swatch row below — there is no per-folder icon to preview,
           *  so the name input owns the full width. */}
          <label htmlFor="folder-config-name-input" className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.name')}</span>
            <Input
              ref={nameRef}
              id="folder-config-name-input"
              className="w-full"
              data-testid="folder-config-name"
              placeholder={i18nT('components.folderConfigModal.name_placeholder')}
              value={draft.name}
              onChange={e => setDraft(d => ({ ...d, name: e.target.value }))}
              {...ime.bindComposition()}
              onKeyDown={e => { if (e.key === 'Enter' && ime.claimEnter(e)) submit() }}
            />
          </label>

          {/* Color — always visible, compact. Leading "no color" swatch
           *  doubles as the remove affordance, so there is no separate reset
           *  control. */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.color')}</span>
            <div className="flex items-center gap-1 flex-wrap">
              <button
                type="button"
                data-testid="folder-config-color-reset"
                title={i18nT('components.folderConfigModal.no_color')}
                aria-label={i18nT('components.folderConfigModal.no_color')}
                aria-pressed={!draft.color}
                onClick={() => setDraft(d => ({ ...d, color: '' }))}
                className={`relative w-5 h-5 rounded-full cursor-pointer border overflow-hidden bg-bg-elevated border-border-strong ${!draft.color ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
              >
                {/* diagonal slash = the universal "none" cell */}
                <span aria-hidden className="absolute left-1/2 top-1/2 w-[26px] h-px bg-danger -translate-x-1/2 -translate-y-1/2 rotate-45" />
              </button>
              {FOLDER_COLOR_PALETTE.map(({ value, label }) => {
                const name = label()
                return (
                  <button
                    key={value}
                    type="button"
                    title={i18nT('components.folderConfigModal.set_color_to_name', { name })}
                    aria-label={i18nT('components.folderConfigModal.set_color_to_name', { name })}
                    aria-pressed={draft.color === value}
                    onClick={() => setDraft(d => ({ ...d, color: value }))}
                    className={`w-5 h-5 rounded-full cursor-pointer border transition-transform hover:scale-110 ${draft.color === value ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
                    style={{ background: `color-mix(in srgb, ${value} 30%, var(--bg-elevated))`, borderColor: value }}
                  />
                )
              })}
            </div>
          </div>

          {/* Tags — chips from the tag vocabulary, copied onto every new chat
           *  filed into this folder. Three vocabulary states, three renders:
           *  UNKNOWN (undefined, query unresolved/failed) renders nothing —
           *  showing the "create tags" hint would falsely tell a user who HAS
           *  tags that none exist; KNOWN-EMPTY shows the onboarding hint
           *  rather than vanishing — a hidden section makes the feature
           *  undiscoverable from the one place it lives; KNOWN-NON-EMPTY
           *  renders the picker. UNRESOLVED (query still loading) keeps the
           *  section heading with a muted placeholder instead of nothing, so
           *  the feature never silently vanishes and the layout does not
           *  shift when the vocabulary resolves after open. FAILED renders an
           *  error line, not the loading hint — a dead query must not assert
           *  an in-progress state indefinitely. */}
          {availableTags === undefined ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.tags')}</span>
              {availableTagsFailed ? (
                <span data-testid="folder-config-tags-error" className="text-[11px] text-danger">
                  {i18nT('components.folderConfigModal.tags_error_hint')}
                  <button
                    type="button"
                    data-testid="folder-config-tags-retry"
                    onClick={onRetryTags}
                    className="ml-1.5 underline underline-offset-2 text-danger hover:opacity-80"
                  >
                    {i18nT('components.folderConfigModal.tags_retry')}
                  </button>
                </span>
              ) : (
                <span data-testid="folder-config-tags-loading" className="text-[11px] text-muted-strong">
                  {i18nT('components.folderConfigModal.tags_loading_hint')}
                </span>
              )}
            </div>
          ) : availableTags.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.tags')}</span>
              <div data-testid="folder-config-tags" className="flex items-center gap-1.5 flex-wrap">
                {availableTags.map(tag => {
                  const selected = draft.tags.includes(tag.id)
                  return (
                    <label
                      key={tag.id}
                      htmlFor={`folder-config-tag-input-${tag.id}`}
                      data-testid={`folder-config-tag-${tag.id}`}
                      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11.5px] cursor-pointer transition-transform hover:scale-105 focus-within:ring-2 focus-within:ring-accent focus-within:ring-offset-1 focus-within:ring-offset-bg ${selected ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
                      style={{
                        background: selected
                          ? `color-mix(in srgb, ${tag.color} 30%, var(--bg-elevated))`
                          : 'var(--bg-elevated)',
                        borderColor: tag.color,
                        color: 'var(--text)',
                      }}
                    >
                      {/* A hidden checkbox, not a <button>: the chips are a
                       *  multi-select choice control, and rendering them as
                       *  sibling buttons would read as an unbounded action row
                       *  (AUTOSDE max-two-buttons-per-row). The label supplies
                       *  the accessible name; checked state carries selection. */}
                      <input
                        type="checkbox"
                        id={`folder-config-tag-input-${tag.id}`}
                        aria-label={tag.name}
                        className="sr-only"
                        checked={selected}
                        onChange={() => setDraft(d => ({
                          ...d,
                          tags: selected ? d.tags.filter(t => t !== tag.id) : [...d.tags, tag.id],
                        }))}
                      />
                      <span aria-hidden className="w-2 h-2 rounded-full shrink-0" style={{ background: tag.color }} />
                      <span className="truncate max-w-[140px]">{tag.name}</span>
                      {/* Same "tag is on" glyph SlotTagPopover uses: selection
                       *  must not hinge on a 1px ring-width difference from the
                       *  keyboard-focus ring of the same accent color. */}
                      {selected && <span aria-hidden className="text-accent"><Check size={11} /></span>}
                    </label>
                  )
                })}
              </div>
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.tags_hint')}</span>
            </div>
          ) : (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.tags')}</span>
              <span data-testid="folder-config-tags-empty" className="text-[11px] text-muted-strong">
                {i18nT('components.folderConfigModal.tags_empty_hint')}
              </span>
            </div>
          )}

          {/* Project directory */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.project_directory')}</span>
            <div className="flex gap-2">
              <Input
                className="flex-1 min-w-0 font-mono text-[12px]"
                data-testid="folder-config-project-dir"
                aria-label={i18nT('components.folderConfigModal.project_directory')}
                placeholder={inheritedDir
                  ? i18nT('components.folderConfigModal.inherited_placeholder', { path: inheritedDir })
                  : i18nT('components.folderConfigModal.project_dir_placeholder')}
                value={draft.projectDir}
                onChange={e => setDraft(d => ({ ...d, projectDir: e.target.value }))}
                {...ime.bindComposition()}
                onKeyDown={e => { if (e.key === 'Enter' && ime.claimEnter(e)) submit() }}
              />
              <Btn ref={browseRef} data-testid="folder-config-browse" onClick={() => setPickerOpen(true)}>
                <FolderOpen size={13} /> {i18nT('components.folderConfigModal.browse')}
              </Btn>
            </div>
            {!draft.projectDir && inheritedDir ? (
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.inherited_dir')}</span>
            ) : (
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.project_dir_hint')}</span>
            )}
          </div>

          {/* Default agent. SimpleSelect renders a <button>, not a <select>, so
           *  this block is a plain div like the project-directory one above and
           *  the heading's own key doubles as the control's aria-label — an
           *  external <label htmlFor> cannot associate with it. Its popup
           *  portals at z-[9999], above the modal's z-[101], the same way
           *  ProjectPicker's does below. */}
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-[11.5px] font-semibold text-muted">
              <Zap size={12} className="shrink-0" /> {i18nT('components.folderConfigModal.default_agent')}
            </span>
            <SimpleSelect
              aria-label={i18nT('components.folderConfigModal.default_agent')}
              options={agentOptions}
              optionLabels={agentOptionLabels}
              clearLabel={globalDefaultAgent
                ? i18nT('components.folderConfigModal.inherit_named', { agent: globalDefaultAgent })
                : i18nT('components.folderConfigModal.none')}
              value={draft.defaultAgent}
              onChange={v => setDraft(d => ({ ...d, defaultAgent: v }))}
            />
            <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.default_agent_hint')}</span>
          </div>
        </div>
      </Modal>

      {/* Portals at z-[9999], above the modal's z-[101], and anchors to the
       *  Browse button. Reused rather than reimplemented so folder-directory
       *  picking stays identical to every other project-directory picker. */}
      {pickerOpen && (
        <ProjectPicker
          open={true}
          onOpenChange={o => { if (!o) setPickerOpen(false) }}
          anchorRef={browseRef}
          onSelect={path => { setDraft(d => ({ ...d, projectDir: path })); setPickerOpen(false) }}
        />
      )}
    </>
  )
}
