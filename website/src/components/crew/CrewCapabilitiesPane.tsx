import { useEffect, useLayoutEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { ApiError } from '../../api/apiError'
import { capabilityKey, crewCapabilitiesApi, crewCapabilitiesKey, type CapabilityBackendWarning, type CapabilityDraft, type CapabilityOperation, type CapabilityPreview, type CapabilityRef, type CapabilityRow, type CapabilitySection } from '../../api/crewCapabilities'
import { Badge, Btn, Checkbox, Input, PanelSectionHeader, SendBtn } from '../ui'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '../ui/dialog'
import ErrorNotice from '../ErrorNotice'
import CopyBranchButton from '../CopyBranchButton'
import SearchableSelect from '../SearchableSelect'
import SimpleSelect from '../SimpleSelect'
import Tablist from '../Tablist'
import CapabilityRowEditor from './CapabilityRowEditor'
import { capabilityLabels } from './capabilityLabels'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import { retentionValid, transportValue } from './capabilityTransport'
import { parseErrorCode } from '../../utils/errorReport'

const CATEGORIES = ['mcpServers', 'tools', 'autoApprove', 'skills'] as const
type Category = typeof CATEGORIES[number]
const CATEGORY_KEYS = { mcpServers: 'pages.agentsPage.mcp_servers', tools: 'crewCapabilities.tools', autoApprove: 'pages.agentsPage.auto_approved', skills: 'pages.agentsPage.skills' }

/** The 409 codes a Reload can resolve: the saved state moved under the draft.
 * The backend names every such code `stale_*` or `*_changed`. Every other 409
 * is a refusal of the draft itself, which a Reload repeats, so telling the user
 * to reload would leave them in a loop. A 409 with no code keeps the stale
 * copy, as before. */
const isStaleCode = (code: string) => code.startsWith('stale_') || code.endsWith('_changed')

/** The member's own agent file an `unreviewable_drift` refusal names: a
 * basename Crew chose, never a path or a value from the file. */
const DETACH_BLOCKED_COPY = {
  detach_tool_limits_unkept: 'crewCapabilities.detachToolLimits',
  detach_removal_unkept: 'crewCapabilities.detachRemoval',
} as const

/** Why a reviewed approval will not land, one line per backend reason. */
const WITHHELD_REASON_COPY = {
  governed: 'crewCapabilities.withheldGoverned',
  not_connected: 'crewCapabilities.withheldNotConnected',
  managed: 'crewCapabilities.withheldManaged',
  unexplained: 'crewCapabilities.withheldUnexplained',
} as const

/** A string field a refusal body carries: `file` (the agent file an
 * `unreviewable_drift` names) or `ref` (the draft entry an
 * `invalid_approval_ref` names). Empty when absent or unparsable. */
function refusalField(body: string | undefined, key: 'file' | 'ref'): string {
  try {
    const value = (JSON.parse(body || '{}') as Record<string, unknown>)[key]
    return typeof value === 'string' ? value : ''
  } catch {
    return ''
  }
}

/** A member chat on the backend the warning's setting names would refuse this
 * spec. A note, not an error: the save still lands and the setting can change
 * afterwards. Three cases:
 * - member DMs (`agent.member_acp_backend`) cannot load saved specs: no page
 *   edits that setting, so the note carries the command that does, on its own
 *   line with a copy button. kas is the member default and loads saved specs;
 *   if the spec also limits single tools, kas would refuse those next, so the
 *   note names that second fix too.
 * - only the default backend (`agent.acp_backend`) refuses: that setting moves
 *   every session, so the note names the affected entry points and offers no
 *   one-line switch.
 * - single-tool limits: the fix is on this page, in the MCP Servers tab, which
 *   the note opens. On the default backend it names the same entry points.
 * The copy opens "Saving works" so it reads true before and after the save.
 * Setting and backend ids stay untranslated. */
function BackendWarning({ warning, member, onOpenTab }: { warning: CapabilityBackendWarning | null | undefined; member: string; onOpenTab: () => void }) {
  const { t } = useTranslation()
  if (!warning) return null
  const values = { name: member, backend: warning.backend, servers: (warning.servers ?? []).join(', '), tab: t(CATEGORY_KEYS.mcpServers) }
  const harness = warning.code === 'harness_unsupported'
  const memberSetting = warning.setting === 'agent.member_acp_backend'
  const key = harness
    ? memberSetting ? 'crewCapabilities.backendUnsupported' : 'crewCapabilities.backendDefaultUnsupported'
    : memberSetting ? 'crewCapabilities.backendToolLimits' : 'crewCapabilities.backendDefaultToolLimits'
  const opensTab = !!values.servers && (!harness || memberSetting)
  return <div role="note" className="mt-2 break-words text-[13px] text-warn" data-testid="capability-backend-warning">
    <p>{t(key, values)}</p>
    {harness && memberSetting && <>
      {/* The copy icon stays visible: this is the only fix for the case. */}
      <code className="mt-1 inline-flex max-w-full rounded border border-border bg-bg px-2 py-1 font-mono text-[12px] text-text [&_svg]:opacity-70" translate="no" data-testid="capability-backend-command">
        <CopyBranchButton branch={`kirocrew config set ${warning.setting} kas`} label={t('crewCapabilities.backendCommandLabel')} />
      </code>
      {values.servers && <p className="mt-1">{t('crewCapabilities.backendAlsoLimits', values)}</p>}
    </>}
    {opensTab && <Btn className="mt-2" onClick={onOpenTab} data-testid="capability-backend-open-tab">{values.tab}</Btn>}
  </div>
}

/** Mounted for the entire editor opening, even when a different rail pane is
 * visible. Drafts and signed previews must not follow the active-pane lifetime. */
export default function CrewCapabilitiesPane({ member, members = [], hidden, onDirtyChange, onBusyChange, onSaved }: {
  member: string
  /** Declared crew names from the roster. The backend still decides which of
   * them share this exact Parent and are enrolled; the list only replaces
   * free-typed names with visible, spellable choices. */
  members?: string[]
  hidden: boolean
  onDirtyChange: (dirty: boolean) => void
  onBusyChange: (busy: boolean) => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const query = useQuery({ queryKey: crewCapabilitiesKey(member), queryFn: () => crewCapabilitiesApi.get(member), retry: false })
  const [discardOpen, setDiscardOpen] = useState(false)
  const [detachOpen, setDetachOpen] = useState(false)
  const [draft, setDraft] = useState<CapabilityDraft | null>(null)
  const [preview, setPreview] = useState<CapabilityPreview | null>(null)
  const [category, setCategory] = useState<Category>('mcpServers')
  const [reference, setReference] = useState('')
  const [approvalSection, setApprovalSection] = useState<'allowedTools' | 'autoApprove'>('allowedTools')
  const [connectionName, setConnectionName] = useState('')
  const models = useAvailableModels({ enabled: !hidden }).map(model => model.name)
  const [reviewedRequest, setReviewedRequest] = useState<CapabilityDraft | null>(null)
  const dirty = draft !== null || reference !== '' || connectionName !== ''
  const prepare = useMutation({ mutationFn: (body: CapabilityDraft) => crewCapabilitiesApi.preview(member, body), onSuccess: (view, body) => { setPreview(view); setReviewedRequest(body) } })
  const save = useMutation({
    mutationFn: (body: CapabilityDraft & { preview_token: string }) => crewCapabilitiesApi.save(member, body),
    onSuccess: async view => {
      qc.setQueryData(crewCapabilitiesKey(member), view)
      setDraft(null); setPreview(null); setReviewedRequest(null); setReference(''); setConnectionName('')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['agentDetail'] }),
        qc.invalidateQueries({ queryKey: ['agents-installed'] }),
        qc.invalidateQueries({ queryKey: ['kirocrew-agents'] }),
      ])
      onSaved()
    },
    onError: () => { setPreview(null); setReviewedRequest(null) },
  })
  // Stop following the Parent. Nothing is recomputed: the member keeps the
  // saved bytes it runs today as its own copy, so it is offered only on a
  // clean pane (no draft to lose) and refreshes the same views a save does.
  const detach = useMutation({
    mutationFn: (revision: string) => crewCapabilitiesApi.detach(member, revision, !!view?.detach_mcp_json_loss),
    onSuccess: async view => {
      qc.setQueryData(crewCapabilitiesKey(member), view)
      setDetachOpen(false)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['agentDetail'] }),
        qc.invalidateQueries({ queryKey: ['agents-installed'] }),
        qc.invalidateQueries({ queryKey: ['kirocrew-agents'] }),
      ])
      onSaved()
    },
    onError: () => setDetachOpen(false),
  })
  const busy = prepare.isPending || save.isPending || detach.isPending
  // Closing reads these in the parent. Publish before paint so paste followed
  // by Escape cannot reach a close handler that still considers the pane clean.
  useLayoutEffect(() => { onDirtyChange(dirty) }, [dirty, onDirtyChange])
  useLayoutEffect(() => { onBusyChange(busy) }, [busy, onBusyChange])
  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])
  const view = query.data
  const emptyDraft = (): CapabilityDraft => ({ revision: view!.revision, enroll: false, operations: [], accept_parent: [], accept_members: [] })
  const edit = (change: (current: CapabilityDraft) => CapabilityDraft) => {
    setDraft(current => change(current ?? emptyDraft()))
    setPreview(null); setReviewedRequest(null); prepare.reset(); save.reset(); detach.reset()
  }
  const changeRow = (operation: CapabilityOperation) => edit(current => ({ ...current, operations: [...current.operations.filter(op => capabilityKey(op) !== capabilityKey(operation)), operation] }))
  const discard = () => { setDraft(null); setPreview(null); setReviewedRequest(null); setReference(''); setConnectionName(''); prepare.reset(); save.reset(); detach.reset() }
  const error = query.error || prepare.error || save.error || detach.error
  // Save refuses a drifted file whose change sits in a setting this page cannot
  // show. The refusal shares the stale save's 409, so it is told apart by the
  // body code, not the status.
  const errorCode = error instanceof ApiError ? parseErrorCode(error.body) : undefined
  // After that refusal, Save cannot help: only the refusal's own instruction
  // speaks, and the Review/Save control stays off until the draft changes or Reload.
  const refused = errorCode === 'unreviewable_drift'
  const conflict = error instanceof ApiError && error.status === 409
  // Only a Review or Save 409 refuses a draft. The load itself can 409 too
  // (a missing parent, a shadowed template) when there may be no draft at all,
  // so those keep the load copy rather than the draft-refusal copy.
  const draftConflict = conflict && !query.error
  // Stop following is refused with no draft open, so its refusals name the save
  // that clears them instead of the draft advice a save refusal gets.
  const detachFailed = !!detach.error && error === detach.error
  const detachRefusal = detachFailed && errorCode && errorCode in DETACH_BLOCKED_COPY
    ? DETACH_BLOCKED_COPY[errorCode as keyof typeof DETACH_BLOCKED_COPY]
    : null
  const errorMessage = refused && error instanceof ApiError
    ? t('crewCapabilities.unreviewableDrift', { member, file: refusalField(error.body, 'file') })
    : detachRefusal
      ? t(detachRefusal, { name: member })
      : detachFailed && draftConflict && errorCode
        ? t('crewCapabilities.detachRefused')
      : draftConflict && errorCode === 'alternate_permissions_require_review'
        ? t('crewCapabilities.permissionsReview', { member })
        : draftConflict && errorCode && !isStaleCode(errorCode)
          ? t('crewCapabilities.refused', { code: errorCode })
          // A 400 is a refusal of the draft's own shape, which a Reload repeats.
          // Name the code (and the fix for the one owners type by hand) rather
          // than the generic "retry" copy, which loops.
          : !query.error && error instanceof ApiError && error.status === 400 && errorCode
            ? errorCode === 'invalid_approval_ref'
              // The refusal names the entry it rejected; with none, the generic
              // refusal copy still names the code.
              ? refusalField(error.body, 'ref')
                ? t('crewCapabilities.invalidApprovalRef', { ref: refusalField(error.body, 'ref') })
                : t('crewCapabilities.refused', { code: errorCode })
              : t('crewCapabilities.refused', { code: errorCode })
          : t(conflict ? 'crewCapabilities.stale' : error instanceof ApiError && [404, 405, 501].includes(error.status) ? 'crewCapabilities.unsupported' : 'crewCapabilities.failed')
  const enabled = !!view && view.schema_version === 1 && view.template.available && !query.isError && (view.mode === 'inherited' || draft?.enroll === true)
  // The member's agent file changed outside this page, so new chats refuse to
  // start. An empty draft rebuilds the file from the saved setup; Review shows
  // what that changes before anything is written.
  const drifted = !!view && view.mode === 'inherited' && view.runtime.status === 'failed' && view.runtime.error_code === 'materialization_changed'
  const request = draft ?? (drifted ? emptyDraft() : null)
  const operations = draft?.operations ?? []
  const invalidTransport = operations.some(op => {
    if (!retentionValid(op)) return true
    if (op.section !== 'mcpServers' || op.action !== 'set' || !('value' in op)) return false
    const value = transportValue(op.value)
    if (!value) return true
    if (['env', 'headers'].some(field => Object.keys(transportValue(value[field]) ?? {}).some(key => !key.trim()))) return true
    if (Object.keys(value).length === 1 && typeof value.disabled === 'boolean') return false
    return typeof (value.command ?? value.url) !== 'string' || !(value.command ?? value.url)
  })
  const rows: CapabilityRow[] = [...(view?.rows ?? [])]
  for (const op of operations) {
    if (!rows.some(row => capabilityKey(row) === capabilityKey(op))) rows.push({ ...op, label: (op.section === 'skills' ? view?.skills : view?.connections)?.find(item => item.id === op.id)?.label ?? op.id, state: 'local', present: true, value: op.action === 'set' && 'value' in op ? op.value : true })
  }
  const visibleSections: CapabilitySection[] = category === 'autoApprove' ? ['allowedTools', 'autoApprove'] : [category]
  const addReference = () => {
    const id = reference.trim()
    if (!id) return
    changeRow({ section: category === 'autoApprove' ? approvalSection : 'tools', id, action: 'set', value: true })
    setReference('')
  }
  const accept = (ref: CapabilityRef, checked: boolean) => edit(current => ({ ...current, accept_parent: checked ? [...current.accept_parent, ref] : current.accept_parent.filter(item => capabilityKey(item) !== capabilityKey(ref)) }))
  const otherMembers = [...new Set(members)].filter(name => name !== member)
  const toggleMember = (name: string, checked: boolean) => edit(current => ({ ...current, accept_members: checked ? [...new Set([...current.accept_members, name])] : current.accept_members.filter(item => item !== name) }))
  // The preview names every member the reviewed request covers, so a member
  // whose local override absorbs a change still gets a visible status line.
  const previewMembers = reviewedRequest ? [member, ...reviewedRequest.accept_members.filter(name => name !== member)] : [member]
  // Accepted conflict rows whose local override absorbed the Parent change on
  // THIS member: the reviewed selection says which rows were accepted, the
  // server view says which of them conflict, and the sanitized preview says
  // the projected row is still an override (local, or removed) with no
  // effective change. Only the reference and its state are named, never a
  // value, and never a peer member's rows: the response carries no
  // projection for them.
  const conflictKeys = new Set((view?.parent_changes ?? []).filter(change => change.conflict).map(capabilityKey))
  const keptRows: CapabilityRow[] = preview && reviewedRequest
    ? reviewedRequest.accept_parent.filter(ref => conflictKeys.has(capabilityKey(ref))
      && !preview.impact.some(item => item.member === member && capabilityKey(item) === capabilityKey(ref)))
      .flatMap(ref => preview.rows.filter(row => capabilityKey(row) === capabilityKey(ref)
        && ((row.state === 'local' && row.present) || (row.state === 'removed' && !row.present))))
    : []
  const enrollmentBlocksEditing = !!view && view.schema_version === 1 && view.template.available && view.mode !== 'inherited' && draft?.enroll !== true

  return (
    <div hidden={hidden} className={hidden ? 'hidden' : 'flex min-h-0 min-w-0 flex-1 flex-col'} data-testid="crew-capabilities-pane">
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4" data-testid="capability-scroll-region">
        <PanelSectionHeader label={t('crewCapabilities.title')} />
        {/* No hand-off: capability drafts and signed previews are not saved. */}
        {error && !refused && <ErrorNotice message={errorMessage} variant="inline" />}
        {/* Reload clears a refusal too: its copy sends the user here once the file is fixed. */}
        <Btn disabled={busy || query.isFetching} onClick={() => { setPreview(null); setReviewedRequest(null); prepare.reset(); save.reset(); detach.reset(); void query.refetch() }}>{t('crewCapabilities.reload')}</Btn>
        {query.isLoading && <p className="text-muted">{t('components.agentTemplateDetail.loading')}</p>}
        {view && view.schema_version !== 1 && <p className="text-muted">{t('crewCapabilities.unsupported')}</p>}
        {view?.schema_version === 1 && <>
          <div className="rounded-md border border-border bg-bg-accent p-3 text-[13px]">
            <p className="break-words">{t('crewCapabilities.parent', { name: view.template.name })}</p>
            <p>{t(capabilityLabels.source[view.template.source])} · {t(capabilityLabels.scope[view.template.scope])}</p>
            <Badge variant="muted">{t(capabilityLabels.mode[view.mode])}</Badge>
            <Badge variant="muted">{t(dirty ? 'crewCapabilities.unsavedDraft' : 'crewCapabilityEditing.saved')}</Badge>
            <details className="mt-2 text-[12px] text-muted">
              <summary>{t('crewCapabilityEditing.versions')}</summary>
              <p className="mt-2 break-all">{t('crewCapabilities.version', { revision: view.revision })}</p>
              <p className="mt-1 break-all">{t('crewCapabilities.savedVersion', { revision: view.runtime.saved_revision })}</p>
            </details>
            {/* No hand-off: validation and runtime errors must preserve the draft. */}
            {!view.template.available
              ? <ErrorNotice message={t('crewCapabilities.parentMissing')} variant="inline" />
              : view.runtime.status === 'failed'
                ? refused ? null : <ErrorNotice message={drifted ? t('crewCapabilities.driftHint', { member }) : t(view.runtime.error_code ? 'crewCapabilityEditing.sourceFailed' : 'crewCapabilities.runtime_failed')} variant="inline" />
                : <p role="status" className="mt-2">{t(capabilityLabels.runtime[view.runtime.status])}</p>}
            <p className="mt-1 text-muted">{t('crewCapabilities.newRuntime')}</p>
            {/* The preview carries its own copy for the reviewed draft. */}
            {!preview && <BackendWarning warning={view.backend_warning} member={member} onOpenTab={() => setCategory('mcpServers')} />}
            {view.mode === 'inherited' && <div className="mt-3">
              <Dialog open={detachOpen} onOpenChange={setDetachOpen}>
                <DialogTrigger asChild><Btn disabled={dirty || busy || !!view.detach_blocked || !!detachRefusal} aria-describedby={detachRefusal ? undefined : 'crew-capabilities-detach-help'} data-testid="capability-detach">{t('crewCapabilities.detach')}</Btn></DialogTrigger>
                <DialogContent maxWidth={440} className="z-[110]">
                  <DialogHeader><DialogTitle className="whitespace-normal">{t('crewCapabilities.detachTitle', { name: member })}</DialogTitle></DialogHeader>
                  <DialogBody><p className="m-0 break-words text-sm text-text">{t('crewCapabilities.detachBody', { name: member, parent: view.template.name, pane: t('pages.kiroCrewAgentsPage.built_from') })}</p>{view.detach_mcp_json_loss && <p className="mt-2 mb-0 break-words text-sm text-text" data-testid="capability-detach-mcp-json">{t('crewCapabilities.detachMcpJson', { name: member })}</p>}</DialogBody>
                  <DialogFooter className="flex-wrap">
                    <Btn onClick={() => setDetachOpen(false)}>{t('crewCapabilities.detachCancel')}</Btn>
                    {/* Primary, not danger: stopping is undoable (follow again any time) and drops nothing. */}
                    <Btn primary disabled={busy} data-testid="capability-detach-confirm" onClick={() => detach.mutate(view.revision)}>{t('crewCapabilities.detachConfirm')}</Btn>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
              {!detachRefusal && <p id="crew-capabilities-detach-help" className="mt-1 text-[12px] text-muted">{t(dirty ? 'crewCapabilities.detachDirty' : view.detach_blocked ? DETACH_BLOCKED_COPY[view.detach_blocked] : 'crewCapabilities.detachHelp', { name: member })}</p>}
            </div>}
            {view.mode !== 'inherited' && <div className="mt-3">
              <label htmlFor="crew-capabilities-enroll" className="flex items-start gap-2"><Checkbox id="crew-capabilities-enroll" aria-labelledby="crew-capabilities-enroll-label" aria-describedby="crew-capabilities-enroll-guarantee crew-capabilities-enroll-help" checked={draft?.enroll ?? false} disabled={busy || !view.template.available} onChange={e => edit(current => ({ ...current, enroll: e.target.checked }))} /><span className="min-w-0 break-words"><span id="crew-capabilities-enroll-label">{t('crewCapabilities.enroll')}</span><span id="crew-capabilities-enroll-guarantee" className="mt-1 block text-text">{t('crewCapabilities.enrollGuarantee')}</span></span></label>
              <p id="crew-capabilities-enroll-help" className="mt-1 text-[12px] text-muted" data-testid={enrollmentBlocksEditing ? 'capability-enroll-to-edit' : undefined}>{t('crewCapabilities.enrollHelp')}</p>
            </div>}
          </div>
          <div className="shrink-0 overflow-x-auto border-b border-border pb-2"><Tablist tabs={CATEGORIES.map(key => ({ key, label: t(CATEGORY_KEYS[key]) }))} value={category} onChange={setCategory} ariaLabel={t('crewCapabilities.title')} /></div>
          <fieldset disabled={!enabled || busy} className="min-w-0">
            {category === 'mcpServers' && <SearchableSelect aria-label={t('crewCapabilities.connection')} triggerFallback={t('crewCapabilities.connection')} value="" disabled={!enabled || busy} options={view.connections.map(item => ({ value: item.id, label: item.label, disabled: item.managed, sublabel: item.managed ? t('crewCapabilities.managed') : undefined }))} onChange={id => changeRow({ section: 'mcpServers', id, action: 'set', connection_id: id })} modal />}
            {category === 'mcpServers' && <div className="mt-3 flex flex-col gap-2">
              <label className="text-[13px]">{t('crewCapabilityEditing.connectionName')}<Input value={connectionName} onChange={e => { setConnectionName(e.target.value); setPreview(null); setReviewedRequest(null) }} /></label>
              <Btn disabled={!connectionName.trim() || rows.some(row => row.section === 'mcpServers' && row.id === connectionName.trim())} onClick={() => {
                changeRow({ section: 'mcpServers', id: connectionName.trim(), action: 'set', value: { command: '', args: [], env: {} } })
                setConnectionName('')
              }}>{t('crewCapabilityEditing.customConnection')}</Btn>
            </div>}
            {category === 'skills' && <SearchableSelect aria-label={t('crewCapabilities.skill')} triggerFallback={t('crewCapabilities.skill')} value="" disabled={!enabled || busy} options={view.skills.map(item => ({ value: item.id, label: item.label }))} onChange={id => changeRow({ section: 'skills', id, action: 'set', value: true })} modal />}
            {(category === 'tools' || category === 'autoApprove') && <div className="flex flex-col gap-2">
              <p className="text-[12px] text-muted">{t(category === 'tools' ? 'crewCapabilities.exposureHint' : 'crewCapabilities.approvalHint')}</p>
              {category === 'autoApprove' && <>
                <SimpleSelect aria-label={t('crewCapabilities.approvalKind')} options={['allowedTools', 'autoApprove']} optionLabels={[t('crewCapabilities.allowedTools'), t('crewCapabilities.serverApproval')]} value={approvalSection} onChange={v => setApprovalSection(v as 'allowedTools' | 'autoApprove')} />
                <p className="text-[12px] text-muted" aria-live="polite">{t(approvalSection === 'allowedTools' ? 'crewCapabilities.allowedToolsHint' : 'crewCapabilities.serverApprovalHint')}</p>
              </>}
              <label className="text-[13px]">{t('crewCapabilities.reference')}<Input value={reference} onChange={e => { setReference(e.target.value); setPreview(null); setReviewedRequest(null) }} /></label>
              <Btn disabled={!reference.trim()} onClick={addReference}>{t('crewCapabilities.add')}</Btn>
            </div>}
            {/* One legend for the per-row source select; rendered once, not per row. */}
            <p className="mt-3 text-[12px] text-muted" data-testid="capability-state-legend">{t('crewCapabilities.stateLegend')}</p>
            {visibleSections.map(section => {
              const sectionRows = rows.filter(row => row.section === section)
              return <div key={section}>
                {category === 'autoApprove' && sectionRows.length > 0 && <PanelSectionHeader label={t(section === 'allowedTools' ? 'crewCapabilities.allowedTools' : 'crewCapabilities.serverApproval')} />}
                {sectionRows.map(row => <CapabilityRowEditor key={capabilityKey(row)} row={row} models={models} operation={operations.find(op => capabilityKey(op) === capabilityKey(row))} disabled={!enabled || busy} onChange={changeRow} />)}
              </div>
            })}
          </fieldset>
          <details className="text-[13px]">
            <summary className="cursor-pointer text-accent">{t('crewCapabilities.advanced')}</summary>
            {rows.filter(row => ['prompt', 'model', 'resources'].includes(row.section)).map(row => <CapabilityRowEditor key={capabilityKey(row)} row={row} models={models} operation={operations.find(op => capabilityKey(op) === capabilityKey(row))} disabled={!enabled || busy} onChange={changeRow} />)}
          </details>
          {view.parent_changes.length > 0 && <details className="rounded border border-border p-3 text-[13px]">
            <summary className="cursor-pointer text-accent">{t('crewCapabilities.parentReview')}</summary>
            <p className="my-2 text-muted">{t('crewCapabilities.parentHint')}</p>
            {view.parent_changes.map(change => {
              const checked = draft?.accept_parent.some(ref => capabilityKey(ref) === capabilityKey(change)) ?? false
              const rowOperation = operations.find(op => capabilityKey(op) === capabilityKey(change))
              const source = rowOperation
                ? rowOperation.action === 'inherit' ? 'inherited' : rowOperation.action === 'remove' ? 'removed' : 'local'
                : view.rows.find(row => capabilityKey(row) === capabilityKey(change))?.state ?? 'inherited'
              // An explicit inherit already accepts this member's current Parent;
              // its checkbox only adds the update to the selected peers' requests.
              const explicitInherit = rowOperation?.action === 'inherit'
              const actionKey = explicitInherit ? 'crewCapabilities.acceptBatchOnly'
                : source === 'local' ? 'crewCapabilities.acceptDismissOverride'
                  : source === 'removed' ? 'crewCapabilities.acceptDismissRemoved'
                    : change.kind === 'removed' ? 'crewCapabilities.acceptTakeParentRemoval' : 'crewCapabilities.acceptTakeParent'
              const id = `parent-change-${encodeURIComponent(capabilityKey(change))}`
              return <label key={capabilityKey(change)} htmlFor={id} className="mb-2 flex items-start gap-2">
                <Checkbox id={id} aria-labelledby={`${id}-action ${id}-ref`} aria-describedby={explicitInherit ? `${id}-hint` : undefined} disabled={!enabled || busy} checked={checked} onChange={e => accept({ section: change.section, id: change.id }, e.target.checked)} />
                <span className="min-w-0 flex-1 break-words"><span id={`${id}-action`} className="mb-1 block" aria-live="polite">{t(actionKey)}</span><code id={`${id}-ref`} className="break-all" translate="no">{change.section}: {change.id}</code> <Badge variant="muted">{t(capabilityLabels.parentChange[change.kind])}</Badge>
                  {change.conflict && <> <Badge variant="warn">{t('crewCapabilities.conflict')}</Badge></>}
                  <span className="mt-1 block break-all text-muted">{t('components.agentTemplateDetail.change_was', { value: JSON.stringify(change.before) })}</span>
                  <code className="mt-1 block whitespace-pre-wrap break-all" translate="no">{JSON.stringify(change.after, null, 2)}</code>
                  {explicitInherit && <span id={`${id}-hint`} className="mt-1 block text-muted"><span className="block">{t('crewCapabilities.acceptBatchOnlyHint', { name: member })}</span><span className="block">{t('crewCapabilities.acceptBatchOnlyPeers')}</span></span>}
                </span>
              </label>
            })}
            <fieldset className="mt-2 min-w-0" disabled={!enabled || busy}>
              <legend className="text-[13px]">{t('crewCapabilities.members')}</legend>
              {otherMembers.length === 0 && <p className="text-[12px] text-muted">{t('crewCapabilities.noOtherMembers')}</p>}
              {otherMembers.map(name => <label key={name} className="mt-1 flex items-center gap-2 text-[13px]"><Checkbox checked={draft?.accept_members.includes(name) ?? false} onChange={e => toggleMember(name, e.target.checked)} /><span className="break-all">{name}</span></label>)}
              <p className="mt-1 text-[12px] text-muted">{t('crewCapabilities.membersHint')}</p>
            </fieldset>
          </details>}
          {draft && draft.revision !== view.revision && <p className="text-[13px] text-warn">{t('crewCapabilities.stale')}</p>}
          {draft && draft.revision !== view.revision && <Btn disabled={busy} onClick={() => edit(current => ({ ...current, revision: view.revision }))}>{t('crewCapabilities.rebase')}</Btn>}
          {preview && <section className="rounded border border-border bg-bg-accent p-3 text-[13px]" aria-label={t('crewCapabilities.previewHeading')}>
            <PanelSectionHeader label={t('crewCapabilities.previewHeading')} />
            {/* First, so the owner reads why before the per-member summary. A
                member whose whole draft is withheld gets a summary line that says
                so, instead of a bare "no effective value changes". The member
                name renders as code so a name like `default` reads as a name. */}
            {(preview.withheld_approvals ?? []).length > 0 && <div role="note" className="mb-2 break-words text-[13px] text-warn" data-testid="capability-preview-withheld">
              <p><Trans i18nKey="crewCapabilities.withheldIntro" values={{ name: member }} components={[<code key="n" translate="no" />]} /></p>
              <ul className="mt-1 list-disc pl-5">{(preview.withheld_approvals ?? []).map(item => <li key={capabilityKey(item)}>
                <code className="break-all" translate="no">{item.id}</code>: <Trans i18nKey={WITHHELD_REASON_COPY[item.reason]} values={{ name: member }} components={[<code key="n" translate="no" />]} />
              </li>)}</ul>
              {/* The not-connected fix lives on this page; open its tab like BackendWarning does. */}
              {(preview.withheld_approvals ?? []).some(item => item.reason === 'not_connected') && <Btn className="mt-2" onClick={() => setCategory('mcpServers')} data-testid="capability-withheld-open-tab">{t('crewCapabilities.withheldOpenTab')}</Btn>}
            </div>}
            <ul className="mb-2 space-y-1" data-testid="capability-preview-members">{previewMembers.map(name => {
              const count = preview.impact.filter(item => item.member === name).length
              return <li key={name} className="break-words">
                {count > 0 ? t('crewCapabilities.memberChanged', { name, count }) : name === member && (preview.withheld_approvals ?? []).length > 0 ? <Trans i18nKey="crewCapabilities.memberWithheldOnly" values={{ name }} components={[<code key="n" translate="no" />]} /> : t('crewCapabilities.memberUnchanged', { name })}
                {name === member && keptRows.length > 0 && <>
                  <span className="mt-1 block">{t('crewCapabilities.memberKeptRows', { name })}</span>
                  <ul className="mt-1 list-disc pl-5" data-testid="capability-preview-kept-rows">{keptRows.map(row => <li key={capabilityKey(row)}><code className="break-all" translate="no">{row.section}: {row.id}</code> <Badge variant="muted">{t(row.state === 'removed' ? 'crewCapabilities.staysRemoved' : capabilityLabels.state[row.state])}</Badge></li>)}</ul>
                </>}
              </li>
            })}</ul>
            <ul className="space-y-2">{preview.impact.map((item, index) => {
              // Only the current member has a projected row in this response.
              // Never substitute a local draft, which can contain new secrets.
              const projected = item.member === member ? preview.rows.find(row => capabilityKey(row) === capabilityKey(item)) : undefined
              return <li key={`${capabilityKey(item)}-${item.member}-${index}`} className="break-all">
                <code translate="no">{item.member}: {item.section}: {item.id}</code>
                <p>{t(capabilityLabels.change[item.change])}</p>
                {projected?.present && <pre className="mt-2 overflow-auto whitespace-pre-wrap break-all rounded border border-border bg-bg p-2 text-[12px]" translate="no">{JSON.stringify(projected.value, null, 2)}</pre>}
                {item.approval_expanded && <p className="text-warn">{t('crewCapabilities.expanded')}</p>}
              </li>
            })}</ul>
            <BackendWarning warning={preview.backend_warning} member={member} onOpenTab={() => setCategory('mcpServers')} />
            <p className="mt-2 text-muted">{t('crewCapabilities.newRuntime')}</p>
          </section>}
          {invalidTransport && <p className="text-[13px] text-warn">{t('crewCapabilityEditing.invalid')}</p>}
        </>}
      </div>
      <Dialog open={discardOpen} onOpenChange={setDiscardOpen}>
        {view?.schema_version === 1 && <div className="flex shrink-0 flex-wrap justify-end gap-2 border-t border-border bg-card px-5 py-3" data-testid="capability-save-footer">
          {/* A refusal disables Save, so its reason sits beside Save, not at the top of a scrolled pane. */}
          {/* No hand-off: the reviewed draft and signed preview are not saved. */}
          {refused && <div className="w-full"><ErrorNotice message={errorMessage} variant="inline" /></div>}
          <DialogTrigger asChild><Btn disabled={!dirty || busy}>{t('crewCapabilities.discard')}</Btn></DialogTrigger>
          <SendBtn disabled={!enabled || !request || busy || refused || invalidTransport || !!connectionName.trim() || !!reference.trim() || request.revision !== view.revision || (request.enroll === false && view.mode !== 'inherited')} onClick={() => {
            if (preview && reviewedRequest) save.mutate({ ...reviewedRequest, preview_token: preview.preview_token })
            else if (request) prepare.mutate(request)
          }}>{busy ? t('crewCapabilities.working') : preview ? t('crewCapabilities.save') : t('crewCapabilities.review')}</SendBtn>
        </div>}
        {/* Keep this Radix layer mounted inside the crew editor's DialogContent:
            Escape dismisses only the confirmation and focus returns to the draft. */}
        <DialogContent maxWidth={440} className="z-[110]">
          <DialogHeader><DialogTitle className="whitespace-normal">{t('crewCapabilities.discardDraftTitle')}</DialogTitle></DialogHeader>
          <DialogBody><p className="m-0 break-words text-sm text-text">{t('crewCapabilities.discardDraftBody', { name: member })}</p></DialogBody>
          <DialogFooter className="flex-wrap">
            <Btn onClick={() => setDiscardOpen(false)}>{t('pages.kiroCrewAgentsPage.keep_editing')}</Btn>
            <Btn danger disabled={busy} onClick={() => { discard(); setDiscardOpen(false) }}>{t('crewCapabilities.discardDraftConfirm')}</Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
