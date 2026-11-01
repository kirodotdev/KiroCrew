/**
 * CrewEditorNextDialog — the crewmate editor, rendered as a modal. This is the
 * single editing experience (the earlier Classic/New A/B toggle and the classic
 * dialog have been retired); every Edit entry point opens it.
 *
 * Driven by the CrewEditorController from useCrewEditor, so everything here
 * saves through the shared state machine and reuses the real field components.
 * The header shows the mate's identity; the X close button is rendered by
 * DialogContent itself.
 */
import { Loader2, ChevronRight, Wrench, Plug, BookOpen, ScrollText, type LucideIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter,
} from '../ui/dialog'
import { Btn, SendBtn } from '../ui'
import { i18nT } from '../../i18n/t'
import SimpleSelect from '../SimpleSelect'
import { toolMeta } from './toolMeta'
import CrewAvatar from '../CrewAvatar'
import CrewAvatarBuilder from '../CrewAvatarBuilder'
import CrewAvatarButton from './CrewAvatarButton'
import { retiredCueFrom, motionsFrom, soundsFrom } from '../../lib/crewAvatarState'
import { DisplayNameField, Field, WorkspaceField, WorkspaceModal, ModelField, EffortField, memberMemoryState } from './crewFields'
import ErrorNotice from '../ErrorNotice'
import { useDialogConfirm } from './useDialogConfirm'
import { effortLabel } from '../../lib/effort'
import { crewCapabilitiesApi, crewCapabilitiesKey, capabilityKey, type CapabilityRow, type CapabilityPreview, type CapabilityDraft } from '../../api/crewCapabilities'
import CrewApprovalsEditor from './CrewApprovalsEditor'
import WorkerKitPane from './WorkerKitPane'
import { templateDisplayName, mateAbilitiesLabel } from './builtFromName'
import { skillLabel } from './workerKitLabels'
import { crewDisplayName } from '../AgentSelector'
import CrewWakeSection from '../CrewWakeSection'
import CrewWebhookSection from '../CrewWebhookSection'
import MemoryDocCard from '../../pages/overview/MemoryDocCard'
import MemoryRecordsEditor from '../../pages/overview/MemoryRecordsEditor'
import MemoryBackupsCard from '../../pages/overview/MemoryBackupsCard'
import MemoryRetiredCard from '../../pages/overview/MemoryRetiredCard'
import MemoryCarveCard from '../../pages/overview/MemoryCarveCard'
import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import { type CrewEditorController } from './useCrewEditor'
import CrewEditorNextRail from './CrewEditorNextRail'
import { uiLocation } from '../../uiLocations/uiLocation'

export type NextSection = 'general' | 'builtFrom' | 'approvals' | 'schedules' | 'webhooks' | 'memory' | 'danger'
type MemoryTab = 'profile' | 'memories' | 'recovery'

/** Split a skill id into owner/namespace + skill name, and de-slugify the name
 *  into a readable Title Case label (hyphens/underscores → spaces). An id with no
 *  "/" is treated as a bare skill name with no owner. */
function parseSkillId(id: string): { owner: string | null; name: string; display: string } {
  const slash = id.lastIndexOf('/')
  const owner = slash >= 0 ? id.slice(0, slash) : null
  const name = slash >= 0 ? id.slice(slash + 1) : id
  const display = name
    .replace(/[-_]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, c => c.toUpperCase())
  return { owner, name, display: display || name }
}

/** The label a capability row shows in a band. */
function bandRowLabel(row: CapabilityRow): string {
  if (row.section === 'skills') {
    return row.label || (row.id.includes('://') ? skillLabel(row.id) : parseSkillId(row.id).display)
  }
  if (row.section === 'tools') return row.label || toolMeta(row.id).label
  if (row.section === 'prompt') return i18nT('crewEditorNext.band_directions')
  return row.label || row.id
}

/** A row's secondary line — a tool's description, an MCP server's host/command,
 *  or nothing. Keeps the primary label scannable while giving context. */
function bandRowSecondary(row: CapabilityRow): string {
  if (row.section === 'tools') return toolMeta(row.id).description
  if (row.section === 'mcpServers') {
    const v = row.value
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const t = v as Record<string, unknown>
      if (typeof t.url === 'string') { try { return new URL(t.url).host } catch { return t.url } }
      if (typeof t.command === 'string') return t.command.split('/').pop() || t.command
    }
  }
  return ''
}

/** The capability kinds a band shows, in order: heading, icon, and whether the
 *  group starts collapsed (Tools is the long one, so it does). Behaviour
 *  directions is the one prompt slot an add fills; it leads, as it shapes how
 *  the worker uses everything below it. */
const BAND_KINDS: {
  section: CapabilityRow['section']
  heading: () => string
  Icon: LucideIcon
  collapsed: boolean
}[] = [
  { section: 'prompt', heading: () => i18nT('crewEditorNext.band_directions'), Icon: ScrollText, collapsed: false },
  { section: 'tools', heading: () => i18nT('crewEditorNext.tab_tools'), Icon: Wrench, collapsed: true },
  { section: 'mcpServers', heading: () => i18nT('crewEditorNext.stat_mcp_servers'), Icon: Plug, collapsed: false },
  { section: 'skills', heading: () => i18nT('crewEditorNext.tab_skills'), Icon: BookOpen, collapsed: false },
]

/** One collapsible kind group (Tools / MCP servers / Skills) inside a band. */
function BandKindGroup({
  testId, section, heading, Icon, startCollapsed, items, onRemove, removeBusy,
}: {
  testId: string
  section: CapabilityRow['section']
  heading: string
  Icon: LucideIcon
  startCollapsed: boolean
  items: CapabilityRow[]
  onRemove?: (row: CapabilityRow) => void
  removeBusy?: string | null
}) {
  const [open, setOpen] = useState(!startCollapsed)
  const bodyId = `band-${testId}-${section}`
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-controls={bodyId}
        data-testid={`next-band-${testId}-group-${section}`}
        className="flex w-full items-center gap-2 bg-bg-accent px-2.5 py-1.5 text-left focus-ring hover:bg-bg-hover"
      >
        <ChevronRight size={13} className={`lucide-inline shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
        <Icon size={13} className="lucide-inline shrink-0 text-muted" aria-hidden="true" />
        <span className="text-[12px] font-medium text-text">{heading}</span>
        <span className="text-[11px] text-muted">{items.length}</span>
      </button>
      {open && (
        <ul id={bodyId} className="m-0 flex list-none flex-col divide-y divide-border border-t border-border p-0">
          {items.map(r => {
            const busy = removeBusy === capabilityKey(r)
            const secondary = bandRowSecondary(r)
            return (
              <li
                key={`${r.section}:${r.id}`}
                className="flex min-w-0 items-center justify-between gap-3 px-2.5 py-1 pl-[2.1rem]"
                data-testid={`next-band-${testId}-row`}
              >
                {r.section === 'prompt' ? (
                  // The directions text itself: read-only, scrollable when long.
                  <p
                    className="m-0 max-h-40 min-w-0 flex-1 overflow-y-auto whitespace-pre-wrap py-0.5 text-[12px] text-text"
                    data-testid={`next-band-${testId}-directions`}
                  >
                    {typeof r.value === 'string' ? r.value : ''}
                  </p>
                ) : (
                  <div className="flex min-w-0 flex-1 items-baseline gap-2">
                    <span className="min-w-0 flex-1 truncate text-[12px] text-text" title={r.id}>{bandRowLabel(r)}</span>
                    {secondary && <span className="min-w-0 flex-1 truncate text-[11px] text-muted">{secondary}</span>}
                  </div>
                )}
                {/* Only a row naming a stored editor row is removable: a
                    selector derived from a server, or a skill the editor does
                    not store, has nothing a remove could drop. */}
                {onRemove && r.remove && (
                  <button
                    type="button"
                    onClick={() => onRemove(r)}
                    disabled={busy}
                    data-testid={`next-band-remove-${r.section}-${r.id}`}
                    className="shrink-0 rounded px-2 py-0.5 text-[11px] text-danger-text hover:bg-danger-subtle focus-ring disabled:opacity-50"
                  >
                    {busy ? i18nT('crewEditorNext.band_removing') : i18nT('crewEditorNext.band_remove')}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/**
 * One band of the read-only capabilities view: a heading and, under it, the
 * crewmate's tools / MCP servers / skills as collapsible kind groups. Each group
 * shows a count so the whole band scans at a glance; rows are compact (name +
 * inline description). `onRemove` adds a per-row Remove control (the "Added"
 * band). Approvals and the rest are configured elsewhere.
 */
function CapabilityBand({
  testId, heading, note, rows, onRemove, removeBusy, emptyText, hideWhenEmpty,
}: {
  testId: string
  heading: string
  note: string
  rows: CapabilityRow[]
  onRemove?: (row: CapabilityRow) => void
  removeBusy?: string | null
  emptyText?: string
  hideWhenEmpty?: boolean
}) {
  const kinds = BAND_KINDS
    .map(k => ({ ...k, items: rows.filter(r => r.section === k.section) }))
    .filter(k => k.items.length > 0)
  // A "comes with" band that is empty (the mate's file carries everything, so
  // nothing comes only from the base) renders nothing rather than a dead "None"
  // box, which reads as broken. The "Added" band keeps its empty state (its
  // emptyText tells the owner how to add).
  if (hideWhenEmpty && kinds.length === 0) return null
  return (
    <section className="flex flex-col gap-2 rounded-md border border-border bg-bg-elevated p-3" data-testid={`next-band-${testId}`}>
      <div className="flex items-baseline gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-strong">{heading}</span>
        <span className="text-[11px] text-muted">{note}</span>
      </div>
      {kinds.length === 0 ? (
        <span className="text-[12px] text-muted" data-testid={`next-band-${testId}-empty`}>{emptyText ?? i18nT('crewEditorNext.band_none')}</span>
      ) : (
        kinds.map(kind => (
          <BandKindGroup
            key={kind.section}
            testId={testId}
            section={kind.section}
            heading={kind.heading()}
            Icon={kind.Icon}
            startCollapsed={kind.collapsed}
            items={kind.items}
            onRemove={onRemove}
            removeBusy={removeBusy}
          />
        ))
      )}
    </section>
  )
}


export default function CrewEditorNextDialog({
  ctl,
  initialSection,
  onSectionChange,
}: {
  ctl: CrewEditorController
  /** The section to show when the editor opens on a crewmate (a deep link's
   *  landing). Applied once per opening; the user's own navigation wins after. */
  initialSection?: NextSection
  /** Told which section is showing while the editor is open, and `null` once it
   *  closes, so a host can report guide facts about it. */
  onSectionChange?: (section: NextSection | null) => void
}) {
  // Which sidebar section is showing. Local; hook declared before any early
  // return so hook order stays stable.
  const [section, setSection] = useState<NextSection>('general')
  // Land on the requested section once per opening of a crewmate. Keyed on the
  // edited name so reopening the same crewmate from a fresh deep link lands
  // again, while a section the user picks afterwards is never overridden.
  const landedFor = useRef<string | null>(null)
  useEffect(() => {
    if (!ctl.open) { landedFor.current = null; return }
    if (landedFor.current === ctl.editing) return
    landedFor.current = ctl.editing
    if (initialSection) setSection(initialSection)
  }, [ctl.open, ctl.editing, initialSection])
  // The capability row currently being removed (its capabilityKey), and any
  // failure. Removal is the one editable action in the capabilities view.
  const [removeBusy, setRemoveBusy] = useState<string | null>(null)
  const [removeError, setRemoveError] = useState<string | null>(null)
  // Which Memory sub-tab is showing.
  const [memoryTab, setMemoryTab] = useState<MemoryTab>('profile')
  // Approvals draft state is OWNED HERE, not inside CrewApprovalsEditor, so an
  // unsaved approvals draft survives the pane unmounting when the user switches
  // section/tab — matching the classic editor's always-mounted capabilities
  // pane. Keyed by capabilityKey; `preview` is the reviewed-but-unsaved result.
  const [approvalsPending, setApprovalsPending] = useState<Record<string, boolean>>({})
  const [approvalsPreview, setApprovalsPreview] = useState<CapabilityPreview | null>(null)
  // The approvals draft is per-crewmate; when the edited target changes, drop
  // any draft from the previous one (the classic pane got this free from its
  // `key={editing}` remount — the lifted state must reset explicitly).
  useEffect(() => {
    setApprovalsPending({})
    setApprovalsPreview(null)
  }, [ctl.editing])
  // Read-only capability view of the agent template this crewmate is built on.
  // Same source the classic Capabilities pane reads; here it is display-only.
  const capsQuery = useQuery({
    queryKey: ctl.open ? crewCapabilitiesKey(ctl.editing) : ['crew-capabilities', '__closed__'],
    queryFn: () => crewCapabilitiesApi.get(ctl.editing),
    enabled: ctl.open,
    retry: false,
  })
  // A crewmate that owns its config file is bound to that file, not to a
  // template: its capabilities view shows what it can do (comes-with plus
  // added), and the General switcher (which would repoint the binding away from
  // the file) gives way to Advanced > Add abilities.
  const ownsFile = capsQuery.data?.mode === 'mate'
  // A crewmate that owns its file: its file holds only additions, so Built
  // From's summary AND its lists read what its worker carries, one set.
  const builtFromRows = (ownsFile ? capsQuery.data?.worker?.rows : null) ?? capsQuery.data?.rows ?? []
  // A mate owns its file and is labelled generically; any other binding is named
  // by its shared template. There is no per-template provenance to read.
  const builtFromName = ownsFile ? mateAbilitiesLabel() : templateDisplayName(ctl.kiroAgent)
  const { confirm, confirmDialog } = useDialogConfirm()
  const queryClient = useQueryClient()

  // Remove one added capability row from the crewmate's file: confirm first,
  // naming the ability and how to add it back, then preview a single
  // `remove` op and save it. Cancelling sends nothing. Added rows are the only
  // editable thing here; comes-with (inherited) rows never offer a remove.
  // Removal uses the displayed revision; a stale view requires a fresh confirm.
  const removeCapability = async (row: CapabilityRow) => {
    const revision = capsQuery.data?.revision
    if (!row.remove || !revision) return
    const ok = await confirm({
      title: i18nT('crewEditorNext.band_remove_confirm_title', { name: bandRowLabel(row) }),
      body: i18nT('crewEditorNext.band_remove_confirm_body'),
      confirmLabel: i18nT('crewEditorNext.band_remove'),
    })
    if (!ok) return
    const key = capabilityKey(row)
    setRemoveBusy(key)
    setRemoveError(null)
    try {
      const draft: CapabilityDraft = {
        revision,
        enroll: false,
        // The stored row the server names, which can differ from what the
        // band displays (a non-catalog skill is stored under `resources`).
        operations: [{ section: row.remove.section, id: row.remove.id, action: 'remove' }],
        accept_parent: [],
        accept_members: [],
      }
      const previewed = await crewCapabilitiesApi.preview(ctl.editing, draft)
      await crewCapabilitiesApi.save(ctl.editing, { ...draft, preview_token: previewed.preview_token })
      await queryClient.invalidateQueries({ queryKey: crewCapabilitiesKey(ctl.editing) })
    } catch (e) {
      if (apiErrorCode(e) === 'stale_revision') {
        await queryClient.invalidateQueries({ queryKey: crewCapabilitiesKey(ctl.editing) })
        setRemoveError(i18nT('crewEditorNext.band_remove_stale'))
        return
      }
      setRemoveError(e instanceof Error ? e.message : i18nT('crewEditorNext.load_error'))
    } finally {
      setRemoveBusy(null)
    }
  }

  // The section a host sees: what the rail shows, which falls back to General
  // when the remembered section is absent from this crewmate's rail (only the
  // Danger zone ever is, for the default crewmate). Same rule as
  // `effectiveSection` below, computed before the early return so the effect
  // keeps a stable hook order.
  const shownSection: NextSection | null = !ctl.open
    ? null
    : section === 'danger' && ctl.isDefaultCrew ? 'general' : section
  useEffect(() => { onSectionChange?.(shownSection) }, [onSectionChange, shownSection])

  if (!ctl.open) {
    if (ctl.loading) {
      return (
        <Dialog open onOpenChange={next => { if (!next) ctl.requestClose() }}>
          <DialogContent maxWidth={620} aria-busy data-testid="crew-editor-next-loading">
            <DialogTitle className="sr-only">{i18nT('pages.chatPage.loading')}</DialogTitle>
            <div className="flex items-center justify-center gap-2 py-10 text-[13px] text-muted">
              <Loader2 size={16} className="lucide-inline animate-spin" />
              {i18nT('pages.chatPage.loading')}
            </div>
          </DialogContent>
        </Dialog>
      )
    }
    return null
  }

  const {
    editing, editingAgent, displayName, setDisplayName,
    kiroAgent, kiroAgentOptions, templateFieldLabel, persistTemplateSwitch, templateSwitchError,
    templateProvenance, capabilityDirty, capabilityBusy, capabilityLoading, capabilityReadFailed,
    editAvatar, openAvatarBuilder, avatarBuilderOpen, applyAvatar, closeAvatarBuilder, onAvatarImageError,
    sheetBusy, dirtyPanes, schedDraft, requestClose, save, resolved,
    error, sheetHint, editorOptionsError,
    childDirtyHandlers, setCapabilityBusy, memoryStore, onCapabilitiesSaved, kirocrewCfg, memoryDirty,
    workspace, setWorkspace, workspaceOptions,
    wsModalOpen, openWsModal, closeWsModal, onWorkspaceCreated, setWsDirty, onPaneSaveChain,
    editModel, setEditModel, editEffort, setEditEffort,
    modelOptions, modelsDegraded, retryModels, retryingModels, pinHarness,
    resolvedError, effortCapable, effortLevels, effortModel,
    memberId, isDefaultCrew, guardedNavigate,
    setSchedDraft, setSchedSaving, requestCancelDraft,
    confirmDelete, setConfirmDelete, deleteCrew, confirmRef,
    discardAsk, setDiscardAsk, discardTakesSheet, askSchedOnly, schedSaving, discardForce, confirmDiscard,
  } = ctl

  // A nested child panel (approvals, a memory doc, the memory records editor)
  // owns its OWN unsaved text; the host cannot discard it on the child's
  // behalf. Switching section/subtab unmounts that panel, which would destroy
  // the draft and clear its dirty flag with no confirmation. So navigation is
  // blocked while such a draft is live (the unsaved note tells the user to
  // save or discard it in place first) and while a save is in flight — the
  // schedule draft keeps its own prompt via the controller's discardAsk.
  // Navigation is blocked only while a draft that would be LOST on unmount is
  // live. The approvals draft is now owned by the dialog (lifted state), so it
  // survives navigation and no longer blocks it; the schedule draft and the
  // memory doc-card drafts are still pane-local (MemoryDocCard keeps its draft
  // in local state and clears its dirty key on unmount), so they keep the
  // block until unmount would otherwise discard them silently. The footer Save
  // still refuses to persist either child draft (see its own guard below).
  const navBlocked = sheetBusy || schedDraft || memoryDirty
  const guardNav = (perform: () => void) => {
    if (navBlocked) return
    perform()
  }
  const selectSection = (key: NextSection) => guardNav(() => setSection(key))

  // Switching "Built from" rebinds the mate. If the current binding is this
  // mate's OWN private copy (private_to === editing), the switch silently drops
  // that customized copy — and it is filtered out of the options, so it cannot
  // be re-selected. Mirror the classic editor: confirm (discard_and_switch)
  // before leaving an own-copy binding; the plain switch runs straight through.
  const onTemplateSwitch = (v: string) => {
    if (v === kiroAgent) return
    const leavingOwnCopy = templateProvenance[kiroAgent]?.private_to === editing
    if (leavingOwnCopy) {
      void (async () => {
        const ok = await confirm({
          title: i18nT('crewEditorNext.switch_own_copy_title'),
          body: i18nT('crewEditorNext.switch_own_copy_body'),
          confirmLabel: i18nT('components.agentTemplateDetail.discard_and_switch'),
        })
        if (ok) persistTemplateSwitch(v)
      })()
      return
    }
    persistTemplateSwitch(v)
  }
  // The classic selector was read-only while the capability view was dirty,
  // busy, loading or failed — a switch then would discard an in-flight edit or
  // act on an unknown state. Keep that guard.
  const templateSwitchLocked = sheetBusy || capabilityDirty || capabilityBusy || capabilityLoading || capabilityReadFailed

  // "Built from" options: keep the current template visible even if it is not
  // in the installed list (parity with the classic selector's `opts`).
  const templateOptions = kiroAgent && !kiroAgentOptions.includes(kiroAgent)
    ? [kiroAgent, ...kiroAgentOptions]
    : kiroAgentOptions

  const caps = capsQuery.data

  const sections: { key: NextSection; label: string }[] = [
    { key: 'general', label: i18nT('crewEditorNext.section_general') },
    { key: 'builtFrom', label: i18nT('crewEditorNext.section_capabilities') },
    { key: 'approvals', label: i18nT('crewEditorNext.tab_approvals') },
    { key: 'schedules', label: i18nT('crewEditorNext.tab_schedules') },
    { key: 'webhooks', label: i18nT('crewEditorNext.tab_webhooks') },
    { key: 'memory', label: i18nT('crewEditorNext.section_memory') },
    // The default crewmate cannot be deleted — the backend rejects it with 409
    // — so the classic editor hid its Danger Zone. Keep that guard.
    ...(isDefaultCrew ? [] : [{ key: 'danger' as NextSection, label: i18nT('crewEditorNext.section_danger') }]),
  ]

  // The dialog stays mounted across opens and `section` is not reset per mate,
  // so a section selected for a previous mate (e.g. 'danger') can be absent from
  // THIS mate's rail (the default mate has no Danger Zone) — which would render
  // no pane and no selected tab, a blank body. Fall back to the first section.
  const effectiveSection: NextSection = sections.some(s => s.key === section) ? section : 'general'

  // privateMemory is NOT always true: the default mate's store is the global
  // legacy store, where MemoryRecordsEditor must run in global (not private)
  // mode. Resolve the real state and pass private only when it is private V2.
  // Null while the config read is in flight, so a broken-binding diagnostic
  // never flashes before the stores are known.
  const memoryState = kirocrewCfg ? memberMemoryState(editing, memoryStore, kirocrewCfg.memory_stores) : null
  const memoryIsPrivate = memoryState === 'private'
  const memoryBroken = memoryState === 'ownership_mismatch' || memoryState === 'unavailable'
  // A binding that breaks while a memory draft is open (another session or
  // the CLI re-points the store) keeps the editors mounted but read-only, so
  // the typed text is never discarded; a clean one shows only the diagnostic.
  const hideMemoryEditors = memoryBroken && !memoryDirty
  const memoryStatus = memoryState === null
    ? ''
    : memoryState === 'private'
      ? i18nT('pages.kiroCrewAgentsPage.private_memory_owned')
      : memoryState === 'legacy'
        ? (editing === 'default' ? i18nT('pages.kiroCrewAgentsPage.global_memory_v1') : i18nT('pages.kiroCrewAgentsPage.private_memory_legacy'))
        : `${i18nT(memoryState === 'ownership_mismatch' ? 'pages.kiroCrewAgentsPage.memory_binding_mismatch' : 'pages.kiroCrewAgentsPage.memory_binding_unavailable')} ${i18nT('pages.kiroCrewAgentsPage.memory_binding_diagnostic', { command: 'kirocrew doctor' })}`

  const memoryTabs: { key: MemoryTab; label: string }[] = [
    { key: 'profile', label: i18nT('crewEditorNext.tab_profile') },
    { key: 'memories', label: i18nT('memoryV2.tab_memories') },
    { key: 'recovery', label: i18nT('crewEditorNext.tab_recovery') },
  ]

  return (
    <>
      <Dialog open onOpenChange={next => { if (!next) requestClose() }}>
        <DialogContent
          maxWidth={1200}
          style={{ height: 'min(90vh, 760px)' }}
          aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: displayName.trim() || editing })}
          data-testid="crew-editor-next-dialog"
        >
          {/* Header: the mate's identity (avatar + name). The X lives in
              DialogContent's corner. */}
          <DialogHeader>
            <div className="flex w-full items-center gap-3 pr-8">
              <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={30} onImageError={onAvatarImageError} />
              <DialogTitle className="flex-1">
                {displayName.trim() || editing}
              </DialogTitle>
            </div>
          </DialogHeader>

          {/* A failed options read (installed templates / workspaces / config)
              must not pass the ['kirocrew'] / ['default'] fallbacks off as the
              real lists — a Save would then persist a binding the operator never
              chose. askAgent=false: the hand-off navigates to /chat and would
              discard this editor's unsaved edits. */}
          <ErrorNotice
            className="mx-5 mt-1"
            title={i18nT('pages.kiroCrewAgentsPage.editor_options_load_failed')}
            message={editorOptionsError ? (editorOptionsError instanceof Error ? editorOptionsError.message : String(editorOptionsError)) : null}
            askAgent={false}
            testId="next-editor-options-load-error"
          />

          <div className="flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
            {/* Sidebar. The two registered sections are the ones a guide
                opens on the way to a control drawn only there: Model and Edit
                avatar sit on General, and Delete is in the Danger zone. */}
            <CrewEditorNextRail
              sections={sections}
              value={effectiveSection}
              onSelect={selectSection}
              locked={navBlocked}
              dangerKey="danger"
              ariaLabel={i18nT("crewEditorNext.sections_aria")}
              lockedHint={i18nT("crewEditorNext.section_locked_hint")}
              segments={[
                { key: 'general', label: i18nT('crewEditorNext.section_general'), ...uiLocation('agents.section-model') },
                { key: 'danger', label: i18nT('crewEditorNext.section_danger'), ...uiLocation('agents.section-danger') },
              ]}
            />
            {/* Panel */}
            <div
              role="tabpanel"
              className="flex min-h-0 min-w-0 flex-1 flex-col gap-5 overflow-y-auto px-6 py-5"
              data-testid="crew-editor-next"
            >
              {/* Panel heading — names the selected section */}
              <h2 className="m-0 text-[15px] font-semibold text-text" data-testid="next-panel-title">
                {sections.find(s => s.key === effectiveSection)?.label}
              </h2>

              {effectiveSection === 'general' && (
                <fieldset disabled={sheetBusy} className="flex flex-col gap-6 border-0 p-0 m-0 min-w-0" style={{ maxWidth: 520 }}>
                  {/* Avatar row — the name now lives in the header, so this is
                      just the avatar + its edit button. */}
                  <div className="flex items-center gap-4">
                    <CrewAvatarButton size={56} onEdit={openAvatarBuilder} disabled={sheetBusy} data-testid="next-avatar-button">
                      <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={56} onImageError={onAvatarImageError} />
                    </CrewAvatarButton>
                    <div className="flex flex-col gap-1">
                      <Btn onClick={openAvatarBuilder} disabled={sheetBusy} data-testid="next-open-avatar-builder" {...uiLocation('agents.edit-avatar')}>
                        {i18nT('components.avatarBuilder.edit_avatar')}
                      </Btn>
                      {editAvatar && (
                        <span className="text-[11px] text-muted">{i18nT('components.avatarBuilder.customized_note')}</span>
                      )}
                    </div>
                  </div>

                  {/* Single clean column — each field is itself: label on top,
                      control, hint below. No side-by-side columns, no row
                      wrappers wrapping components that already carry their own
                      label/hint. This is the layout these fields are built for. */}
                  <DisplayNameField value={displayName} onChange={setDisplayName} fallback={editing} />

                  {ownsFile ? (
                    <Field label={i18nT('crewEditorNext.section_capabilities')} hint={i18nT('crewEditorNext.built_from_mate_hint')}>
                      <div className="flex items-center justify-between gap-3" data-testid="next-built-from-owned">
                        <span className="text-[13px] text-text">{builtFromName}</span>
                        <Btn onClick={() => guardNav(() => setSection('builtFrom'))} disabled={navBlocked} data-testid="next-open-advanced">
                          {i18nT('crewEditorNext.open_capabilities')}
                        </Btn>
                      </div>
                    </Field>
                  ) : (
                  <Field label={templateFieldLabel} hint={i18nT('crewEditorNext.built_from_hint')}>
                    <SimpleSelect
                      options={templateOptions}
                      optionLabels={templateOptions.map(templateDisplayName)}
                      value={kiroAgent}
                      onChange={onTemplateSwitch}
                      disabled={templateSwitchLocked}
                      aria-label={templateFieldLabel}
                    />
                    {/* A refused repoint (foreign private copy, stale binding,
                        unverifiable lineage) must say why — otherwise the
                        dropdown silently snaps back and the user is left
                        guessing. The controller already produces the reason.
                        No hand-off: the hand-off navigates to /chat and would
                        discard the open editor's unsaved crew fields (display
                        name, workspace, model, color) and any approvals/memory
                        draft, so keep it off here. */}
                    <ErrorNotice
                      message={templateSwitchError}
                      variant="inline"
                      askAgent={false}
                      testId="next-template-switch-error"
                    />
                  </Field>
                  )}

                  <WorkspaceField
                    options={workspaceOptions}
                    value={workspace}
                    onChange={setWorkspace}
                    onNewWorkspace={openWsModal}
                    subject="agent"
                    hint={(!workspace || workspace === 'default')
                      ? i18nT('crewEditorNext.workspace_hint_default')
                      : i18nT('crewEditorNext.workspace_hint_custom')}
                  />

                  {/* Model lives on General (moved from the Behavior section's
                      Model tab): it is a core identity field alongside template
                      and workspace, not a behavior toggle. The underlying model
                      state and save path are unchanged. */}
                  <div className="flex flex-col gap-3" data-testid="next-general-model">
                    <p className="text-[12px] text-muted">
                      {i18nT('components.crew.crewEditorNextDialog.model_frame')}
                    </p>
                    <ModelField
                      options={modelOptions}
                      value={editModel}
                      onChange={setEditModel}
                      servedAs={editModel === editingAgent?.model ? resolved?.pin_served_as : undefined}
                      harness={pinHarness}
                    />
                    {/* Model list resolved DEGRADED (transport failure → auto-only/cached list).
                        No hand-off: this is an advisory read-degradation, not a
                        failure, and the hand-off would discard the open editor's
                        unsaved crew/approvals/memory drafts. */}
                    {modelsDegraded && (
                      <div className="mt-1 flex flex-wrap items-center gap-2">
                        <ErrorNotice
                          className="min-w-[12rem] flex-1"
                          // Another harness's list keeps no last-good copy, so a failure leaves only Inherited.
                          message={resolved?.models_backend === undefined
                            ? i18nT('pages.chatSidebar.model_list_failed')
                            : i18nT('pages.kiroCrewAgentsPage.harness_models_failed', { harness: pinHarness })}
                          askAgent={false}
                          testId="next-model-degraded"
                        />
                        <Btn type="button" className="shrink-0" onClick={retryModels} disabled={retryingModels}>
                          {i18nT('pages.chatSidebar.retry')}
                        </Btn>
                      </div>
                    )}
                    {(effortCapable || !!editEffort) && (
                      <EffortField value={editEffort} onChange={setEditEffort} levels={effortLevels} />
                    )}
                    {!effortCapable && !!editEffort && (
                      <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                        {resolved?.effort_supported === false
                          ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_by_this_backend', { harness: pinHarness })
                          : effortModel
                          ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_on_this_model', { model: effortModel })
                          : i18nT('pages.kiroCrewAgentsPage.effort_pin_needs_a_model')}
                      </div>
                    )}
                    {/* No hand-off: a model-resolution read failure leaves the
                        editor's unsaved crew/approvals/memory drafts in place; a
                        hand-off would navigate away and discard them. */}
                    <ErrorNotice
                      message={resolvedError ? (resolvedError instanceof Error ? resolvedError.message : String(resolvedError)) : null}
                      askAgent={false}
                      testId="next-model-resolved-error"
                    />
                    {resolved && (
                      <div className="flex flex-col gap-1 rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                        {/* A scoped pin the harness serves under no id stays unsent. A foreign
                            pin can instead resolve to a lower tier, which the readout must name. */}
                        {editModel === editingAgent?.model && resolved.pin_served_as === '' && (!resolved.model || resolved.model === editModel) ? (
                          <div className="text-text" data-testid="next-model-pin-unserved">
                            {i18nT('pages.kiroCrewAgentsPage.pin_unserved_resolves_to', { model: editModel, harness: pinHarness })}
                          </div>
                        ) : (
                          <div>
                            <span className="text-text">
                              {i18nT('pages.kiroCrewAgentsPage.resolves_to', { model: resolved.model || i18nT('pages.kiroCrewAgentsPage.inherited') })}
                            </span>
                            {' — '}
                            {resolved.pinned && resolved.model === editingAgent?.model
                              ? i18nT('pages.kiroCrewAgentsPage.pinned_on_this_crew')
                              : resolved.model
                                ? i18nT('pages.kiroCrewAgentsPage.inherited_from_the_agent_template')
                                : i18nT('pages.kiroCrewAgentsPage.no_pin_anywhere_the_backend_chooses')}
                          </div>
                        )}
                        {(effortCapable || !editEffort) && (
                          <div>
                            {effortCapable ? (
                              <>
                                <span className="text-text">
                                  {i18nT('pages.kiroCrewAgentsPage.effort_resolves_to', {
                                    effort: resolved.reasoning_effort
                                      ? effortLabel(resolved.reasoning_effort)
                                      : i18nT('lib.effort.default'),
                                  })}
                                </span>
                                {' — '}
                                {resolved.effort_pinned
                                  ? i18nT('pages.kiroCrewAgentsPage.pinned_on_this_crew')
                                  : resolved.reasoning_effort
                                    ? i18nT('pages.kiroCrewAgentsPage.effort_inherited_from_the_global_default')
                                    : i18nT('pages.kiroCrewAgentsPage.no_effort_pin_the_model_decides')}
                              </>
                            ) : effortModel ? (
                              i18nT('pages.kiroCrewAgentsPage.effort_unavailable_on_this_model', { model: effortModel })
                            ) : (
                              i18nT('pages.kiroCrewAgentsPage.effort_needs_a_model')
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </fieldset>
              )}

              {effectiveSection === 'builtFrom' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-builtFrom">
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    {capsQuery.isLoading && (
                      <div className="flex items-center gap-2 text-[12px] text-muted">
                        <Loader2 size={14} className="lucide-inline animate-spin" />
                        {i18nT('crewEditorNext.loading_template')}
                      </div>
                    )}
                    {capsQuery.error && (
                      // No hand-off: the editor is a modal holding the user's
                      // unsaved edits in its other sections; the hand-off would
                      // navigate away and discard them.
                      <ErrorNotice
                        message={capsQuery.error instanceof Error ? capsQuery.error.message : i18nT('crewEditorNext.load_error')}
                        variant="inline"
                        askAgent={false}
                        testId="next-builtfrom-error"
                      />
                    )}
                    {caps && (
                      <div className="flex flex-col gap-4" data-testid="next-builtfrom-details" style={{ maxWidth: 560 }}>
                        <div className="flex items-baseline gap-3">
                          <span className="text-[13px] text-text" data-testid="next-builtfrom-name">
                            {i18nT('crewEditorNext.capabilities_headline', { name: displayName.trim() || editing })}
                          </span>
                        </div>
                        <p className="m-0 text-[12px] leading-relaxed text-muted">
                          {i18nT('crewEditorNext.capabilities_body')}
                        </p>

                        {/* Band 1 — comes with (the worker base). Read-only rows. */}
                        <CapabilityBand
                          testId="comes-with"
                          heading={i18nT('crewEditorNext.band_comes_with', { name: builtFromName })}
                          note={i18nT('crewEditorNext.band_comes_with_note')}
                          rows={builtFromRows.filter(r => r.present && r.state === 'inherited')}
                          hideWhenEmpty
                        />

                        {/* Band 2 — added. Removable rows (the only editable thing here;
                            approvals are configured in the Approvals section). */}
                        <CapabilityBand
                          testId="added"
                          heading={i18nT('crewEditorNext.band_added', { name: displayName.trim() || editing })}
                          note={i18nT('crewEditorNext.band_added_note')}
                          rows={builtFromRows.filter(r => r.present && r.state === 'local')}
                          onRemove={ownsFile ? removeCapability : undefined}
                          removeBusy={removeBusy}
                          emptyText={i18nT('crewEditorNext.band_added_empty')}
                        />

                        {/* No hand-off on the two notices below: the editor modal
                            holds the user's unsaved edits in its other sections,
                            which the hand-off navigation would discard. */}
                        {removeError && (
                          <ErrorNotice message={removeError} variant="inline" askAgent={false} testId="next-builtfrom-remove-error" />
                        )}

                        {!caps.template.available && (
                          <ErrorNotice
                            message={i18nT('crewEditorNext.template_unavailable', { name: caps.template.name })}
                            variant="inline"
                            askAgent={false}
                            testId="next-builtfrom-unavailable"
                          />
                        )}

                        {/* Add abilities lives on the same page as the bands it
                            feeds — a mate owns a worker file to extend. */}
                        {ownsFile && (
                          <div className="border-t border-border pt-4">
                            <WorkerKitPane
                              member={editing}
                              name={displayName.trim() || (editingAgent ? crewDisplayName(editingAgent) : editing)}
                              templates={kiroAgentOptions}
                              onOpenApprovals={() => selectSection('approvals')}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {effectiveSection === 'approvals' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-approvals">
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    <fieldset disabled={sheetBusy} className="flex min-h-0 flex-col border-0 p-0 m-0 min-w-0" data-testid="next-behavior-approvals">
                      <CrewApprovalsEditor
                        member={editing}
                        pending={approvalsPending}
                        setPending={setApprovalsPending}
                        preview={approvalsPreview}
                        setPreview={setApprovalsPreview}
                        onDirtyChange={childDirtyHandlers.approvals}
                        onBusyChange={setCapabilityBusy}
                        onSaved={onCapabilitiesSaved}
                      />
                    </fieldset>
                  </div>
                </div>
              )}

              {effectiveSection === 'schedules' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-schedules">
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-behavior-schedules">
                      <CrewWakeSection
                        crew={editing}
                        memberId={memberId}
                        agentTemplate={kiroAgent}
                        isDefaultCrew={isDefaultCrew}
                        hideKindBadge
                        heading={i18nT('crewEditorNext.schedules_heading')}
                        blurb={i18nT('crewEditorNext.schedules_blurb')}
                        onDraftChange={setSchedDraft}
                        onSavingChange={setSchedSaving}
                        onRequestCancel={requestCancelDraft}
                        onNavigateAway={guardedNavigate}
                      />
                    </fieldset>
                  </div>
                </div>
              )}

              {effectiveSection === 'webhooks' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-webhooks">
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    <fieldset disabled={sheetBusy} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-behavior-webhooks">
                      <p className="text-[12px] text-muted">
                        {i18nT('crewEditorNext.webhooks_frame')}
                      </p>
                      <CrewWebhookSection crew={editing} onNavigateAway={guardedNavigate} />
                    </fieldset>
                  </div>
                </div>
              )}

              {effectiveSection === 'memory' && (
                <div className="flex min-h-0 flex-1 flex-col" data-testid="next-pane-memory">
                  {memoryStatus && (
                    <p
                      className={`m-0 mb-3 break-words text-[12px] ${memoryBroken ? 'text-danger' : 'text-muted'}`}
                      data-testid="next-memory-status"
                    >
                      {memoryStatus}
                    </p>
                  )}
                  {!hideMemoryEditors && (<>
                  {/* Memory sub-tabs — mirrors the global V2 memory surface. */}
                  <div
                    role="tablist"
                    aria-label={i18nT("crewEditorNext.memory_sections_aria")}
                    className="flex shrink-0 gap-1 border-b border-border"
                  >
                    {memoryTabs.map(t => {
                      const isActive = t.key === memoryTab
                      return (
                        <button
                          key={t.key}
                          type="button"
                          role="tab"
                          aria-selected={isActive}
                          data-testid={`next-memory-tab-${t.key}`}
                          onClick={() => guardNav(() => setMemoryTab(t.key))}
                          disabled={!isActive && navBlocked}
                          className={[
                            'relative -mb-px px-3 py-2 text-[12.5px] focus-ring disabled:opacity-50 disabled:cursor-not-allowed',
                            isActive
                              ? 'border-b-2 border-accent font-medium text-text'
                              : 'border-b-2 border-transparent text-muted hover:text-text',
                          ].join(' ')}
                        >
                          {t.label}
                        </button>
                      )
                    })}
                  </div>

                  <div className="min-h-0 flex-1 overflow-y-auto pt-4">
                    {!memoryStore ? (
                      <div className="rounded-md border border-border bg-bg-accent px-3 py-2 text-[12px] text-muted">
                        {i18nT('crewEditorNext.memory_empty')}
                      </div>
                    ) : (
                      <>
                        {memoryTab === 'profile' && (
                          <fieldset disabled={sheetBusy || memoryBroken} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-memory-profile">
                            <p className="text-[12px] text-muted">
                              {i18nT('crewEditorNext.memory_profile_frame')}
                            </p>
                            <MemoryDocCard
                              key={`prefs-${memoryStore}`}
                              docKey="preferences"
                              store={memoryStore}
                              title={i18nT('pages.overview.memoryTab.preferences')}
                              rows={7}
                              placeholder={i18nT('pages.overview.memoryTab.loading')}
                              read={api.memoryPreferences}
                              write={api.saveMemoryPreferences}
                              onDirtyChange={childDirtyHandlers['memory-preferences']}
                              saveLabel={i18nT('pages.overview.memoryTab.save_profile')}
                            />
                            <MemoryDocCard
                              key={`projects-${memoryStore}`}
                              docKey="projects"
                              store={memoryStore}
                              title={i18nT('pages.overview.memoryTab.projects')}
                              rows={7}
                              placeholder={i18nT('pages.overview.memoryTab.loading')}
                              read={api.memoryProjects}
                              write={api.saveMemoryProjects}
                              onDirtyChange={childDirtyHandlers['memory-projects']}
                              saveLabel={i18nT('pages.overview.memoryTab.save_profile')}
                            />
                          </fieldset>
                        )}
                        {memoryTab === 'memories' && (
                          <fieldset disabled={sheetBusy || memoryBroken} className="border-0 p-0 m-0 min-w-0" data-testid="next-memory-memories">
                            <MemoryRecordsEditor
                              key={`memories-${memoryStore}`}
                              store={memoryStore}
                              privateMemory={memoryIsPrivate}
                              onDirtyChange={childDirtyHandlers['memory-records']}
                              onRecovery={() => guardNav(() => setMemoryTab('recovery'))}
                            />
                          </fieldset>
                        )}
                        {memoryTab === 'recovery' && (
                          <fieldset disabled={sheetBusy || memoryBroken} className="flex flex-col gap-4 border-0 p-0 m-0 min-w-0" data-testid="next-memory-recovery">
                            <MemoryBackupsCard key={`backups-${memoryStore}`} store={memoryStore} privateMemory={memoryIsPrivate} />
                            <MemoryRetiredCard key={`retired-${memoryStore}`} store={memoryStore} privateMemory={memoryIsPrivate} />
                            <MemoryCarveCard key={`carve-${memoryStore}`} store={memoryStore} />
                          </fieldset>
                        )}
                      </>
                    )}
                  </div>
                  </>)}
                </div>
              )}

              {effectiveSection === 'danger' && !isDefaultCrew && (
                <div className="flex min-h-0 flex-1 flex-col gap-4" data-testid="next-pane-danger">
                  <div className="flex max-w-[560px] flex-col gap-4">
                    <p className="m-0 text-[12px] leading-relaxed text-muted">
                      {i18nT('pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its')}
                    </p>
                  <div className="flex flex-col items-start gap-3 rounded-md border border-danger-subtle bg-danger-subtle p-3">
                    <div className="flex flex-col gap-0.5">
                      <span className="text-[12.5px] font-medium text-text-strong">
                        {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                      </span>
                      {confirmDelete && (
                        <span className="text-[11.5px] leading-relaxed text-muted">
                          {i18nT('pages.kiroCrewAgentsPage.delete_crew_named_confirm', { name: editing })}
                        </span>
                      )}
                    </div>
                    <div ref={confirmRef} className="flex items-center gap-2">
                      {confirmDelete ? (
                        <>
                          <Btn onClick={() => setConfirmDelete(false)} data-testid="next-cancel-delete-crew">
                            {i18nT('pages.kiroCrewAgentsPage.cancel')}
                          </Btn>
                          <Btn danger onClick={deleteCrew} disabled={sheetBusy} data-testid="next-confirm-delete-crew">
                            {i18nT('pages.kiroCrewAgentsPage.yes_delete_it')}
                          </Btn>
                        </>
                      ) : (
                        <Btn danger onClick={() => setConfirmDelete(true)} disabled={sheetBusy} data-testid="next-delete-crew" {...uiLocation('agents.delete')}>
                          {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                        </Btn>
                      )}
                    </div>
                  </div>
                  </div>
                </div>
              )}
            </div>
          </div>

          <DialogFooter>
            {/* Left-aligned feedback: a failed-save error, a blocking hint
                (e.g. finish the schedule draft first), or an unsaved-changes
                note — mutually exclusive, error first.
                No hand-off: a failed save means the user's edits are still in
                the open panes; the hand-off would navigate away and discard
                them, so the user must stay to retry. */}
            <ErrorNotice message={error} variant="inline" className="mr-auto" askAgent={false} testId="next-sheet-error" />
            {sheetHint && !error && (
              <span className="mr-auto text-[12px] text-danger" data-testid="next-sheet-hint">{sheetHint}</span>
            )}
            {dirtyPanes.size > 0 && !error && !sheetHint && (
              <span className="mr-auto text-[11.5px] text-muted" data-testid="next-unsaved-note">
                {/* When the only dirty panes are child-owned (approvals/memory
                    'capabilities' or 'schedules'), the footer Save is disabled
                    because those save through their OWN pane control — so the
                    note must point there instead of competing with a dead
                    "Save changes" button. */}
                {[...dirtyPanes].every(p => p === 'capabilities' || p === 'schedules')
                  ? i18nT('components.crewEditor.unsaved_in_pane')
                  : i18nT('components.crewEditor.unsaved_changes')}
              </span>
            )}
            <Btn onClick={requestClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            <SendBtn
              onClick={save}
              /* Footer Save persists only the crew-level fields; approvals and
                 memory ('capabilities') and the schedule draft ('schedules')
                 save through their OWN controls. Enabling Save on those would
                 run the roster-only save and close, silently dropping the
                 child draft. So Save is disabled whenever a child/capability
                 draft is live (it saves through its own control) OR there is no
                 crew-level dirt to persist, while the unsaved note and Cancel
                 confirm still see the full set. */
              disabled={sheetBusy || schedDraft || dirtyPanes.has('capabilities') || [...dirtyPanes].every(p => p === 'capabilities' || p === 'schedules')}
            >{i18nT('pages.kiroCrewAgentsPage.save_changes')}</SendBtn>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {editing && (
        <CrewAvatarBuilder
          open={avatarBuilderOpen}
          name={editing}
          value={editAvatar}
          retiredCue={retiredCueFrom(editingAgent?.avatar)}
          savedReactions={{
            motions: motionsFrom(editingAgent?.avatar) !== null,
            sounds: soundsFrom(editingAgent?.avatar) !== null,
          }}
          onCancel={closeAvatarBuilder}
          onSave={applyAvatar}
        />
      )}

      <WorkspaceModal
        open={wsModalOpen}
        workspaceOptions={workspaceOptions}
        onCreated={onWorkspaceCreated}
        onClose={closeWsModal}
        onDirtyChange={setWsDirty}
        onSaveChain={onPaneSaveChain}
      />

      {/* Discard-confirm prompt — driven by the SAME controller state the
          classic dialog uses (discardAsk / confirmDiscard). Without it a dirty
          close would set discardAsk='close' on the controller with no UI,
          leaving the editor stuck open and silently swallowing the Cancel. */}
      <Dialog open={discardAsk !== null} onOpenChange={next => { if (!next) setDiscardAsk(null) }}>
        <DialogContent
          maxWidth={440}
          className="z-[110]"
          aria-label={askSchedOnly
            ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
            : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
        >
          <DialogHeader>
            <DialogTitle className="whitespace-normal">
              {askSchedOnly
                ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
                : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
            </DialogTitle>
          </DialogHeader>
          <DialogBody>
            <p className="m-0 text-sm text-text">
              {schedDraft
                ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule_body')
                : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_body', { name: editing })}
            </p>
            {schedDraft && discardTakesSheet && (
              <p className="mb-0 mt-2 text-sm text-text" data-testid="next-sched-discard-also-crew">
                {i18nT('pages.kiroCrewAgentsPage.discard_also_crew_edits')}
              </p>
            )}
            {schedSaving && (
              <p className="mb-0 mt-2 text-[12px] text-muted" data-testid="next-sched-discard-saving-note">
                {discardForce
                  ? i18nT('pages.kiroCrewAgentsPage.discard_anyway_note')
                  : i18nT('pages.kiroCrewAgentsPage.discard_locked_while_saving')}
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => setDiscardAsk(null)} data-testid="crew-sched-discard-keep">
              {i18nT('pages.kiroCrewAgentsPage.keep_editing')}
            </Btn>
            <Btn
              danger
              onClick={confirmDiscard}
              disabled={schedSaving && !discardForce}
              title={schedSaving ? i18nT('components.jobForm.saving') : undefined}
              data-testid="crew-sched-discard-confirm"
            >
              {askSchedOnly
                ? i18nT('pages.kiroCrewAgentsPage.discard_schedule_confirm')
                : i18nT('pages.kiroCrewAgentsPage.discard_confirm')}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </>
  )
}
