/**
 * CrewApprovalsEditor — the New editor's Mate Details → Approvals surface.
 *
 * Scope by design: this edits ONLY auto-approve — of the capabilities the mate's
 * agent already provides, which ones run WITHOUT asking. Adding a tool / MCP
 * server / skill is a CUSTOM-AGENT concern (you cannot auto-approve a capability
 * the agent does not have), so there is deliberately no "add" affordance here;
 * this is purely the per-mate layer on top of the agent's capability set.
 *
 * It drives the crewCapabilitiesApi draft → preview → save path, so the
 * security-ceiling checks are the real ones — with a calm toggle list rather
 * than a full capability editor (no reload,
 * enrollment checkbox, parent/version badges, or nested category tablist).
 *
 * Auto-approve has two kinds, both of which skip the prompt:
 *   - allowedTools: built-in tool NAMES (e.g. fs_write)
 *   - autoApprove:  per-MCP-server verbs, keyed @server/verb
 * Each grantable item is a row with an on/off toggle. "On" means the mate runs it
 * unattended; "off" means it asks first. Inherited-from-template vs this-mate-local
 * is marked lightly.
 */
import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, Loader2 } from 'lucide-react'
import {
  capabilityKey, crewCapabilitiesApi, crewCapabilitiesKey,
  type CapabilityDraft, type CapabilityOperation, type CapabilityPreview, type CapabilityRow,
} from '../../api/crewCapabilities'
import { toolMeta } from './toolMeta'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import ErrorNotice from '../ErrorNotice'

/** How a grant participates in the server/verb hierarchy. */
type GrantKind =
  | 'tool'        // built-in tool: allowedTools, bare name, atomic, no children
  | 'server'      // MCP server-allow: allowedTools `@server`, parent of its verbs
  | 'verb'        // MCP auto-approve: autoApprove `@server/verb`, child of a server

/** A single grantable item, resolved for display. */
interface Grant {
  section: 'allowedTools' | 'autoApprove'
  kind: GrantKind
  id: string
  /** The MCP server this grant belongs to (set for `server` and `verb`). */
  server: string | null
  /** Current effective on/off (present = granted). */
  on: boolean
  /** inherited (from template) / local (this mate) / removed (this mate dropped). */
  state: CapabilityRow['state']
  label: string
  sublabel: string
}

/** Resolve a capability row into a display Grant. */
function toGrant(r: CapabilityRow): Grant {
  // The SECTION is authoritative from the row — never inferred from the id
  // shape. An allowedTools row whose id happens to look like `@server/tool`
  // must still toggle as allowedTools, else the off-write sends the wrong
  // section and the backend rejects it with approval_exclusion_overlaps.
  if (r.section === 'autoApprove' && r.id.startsWith('@') && r.id.includes('/')) {
    const [server, tool] = r.id.slice(1).split('/', 2)
    return { section: 'autoApprove', kind: 'verb', id: r.id, server, on: r.present, state: r.state, label: tool, sublabel: r.id }
  }
  // An `@server` allowedTools entry (no `/`) is the server-level allow — the
  // PARENT of its `@server/verb` auto-approve children. The dependency is real:
  // the backend rejects (`approval_exclusion_overlaps`) a draft that removes
  // `@server` while any `@server/verb` auto-approve survives, so the two must
  // move together. We render it as a collapsible parent, not a flat toggle.
  if (r.section === 'allowedTools' && r.id.startsWith('@') && !r.id.includes('/')) {
    const server = r.id.slice(1)
    return { section: 'allowedTools', kind: 'server', id: r.id, server, on: r.present, state: r.state, label: server, sublabel: r.id }
  }
  const m = toolMeta(r.id)
  return {
    section: r.section === 'autoApprove' ? 'autoApprove' : 'allowedTools',
    kind: 'tool',
    id: r.id,
    server: null,
    on: r.present,
    state: r.state,
    label: m.label || r.label || r.id,
    sublabel: r.id,
  }
}

/** One toggle row, shared by the Tools list and the server verb children. */
function ToggleRow({ g, on, changed, busy, disabled, indent, onToggle }: {
  g: Grant; on: boolean; changed: boolean; busy: boolean; disabled?: boolean; indent?: boolean; onToggle: () => void
}) {
  return (
    <li className={[
      'flex items-center justify-between gap-3 rounded-md border border-border bg-bg-accent px-3 py-2',
      indent ? 'ml-4' : '',
      disabled ? 'opacity-50' : '',
    ].join(' ')}>
      <div className="flex min-w-0 flex-col">
        <span className="truncate text-[12.5px] text-text">{g.label}</span>
        <span className="truncate font-mono text-[11px] text-muted">{g.sublabel}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2.5">
        {changed && <span className="text-[10px] text-warn">{i18nT('crewApprovalsEditor.state_changed')}</span>}
        {!changed && g.state !== 'inherited' && (
          <span className="text-[10px] text-muted">{g.state === 'local' ? i18nT('crewApprovalsEditor.state_this_mate') : i18nT('crewApprovalsEditor.state_removed')}</span>
        )}
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label={i18nT('crewApprovalsEditor.auto_approve_aria', { label: g.label })}
          data-testid={`next-approval-toggle-${g.id}`}
          disabled={busy || disabled}
          onClick={onToggle}
          className={[
            'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus-ring disabled:cursor-not-allowed',
            on ? 'bg-accent' : 'bg-border',
          ].join(' ')}
        >
          <span className={['inline-block h-4 w-4 rounded-full bg-bg transition-transform', on ? 'translate-x-4' : 'translate-x-0.5'].join(' ')} />
        </button>
      </div>
    </li>
  )
}

/**
 * One MCP server as a collapsible parent row: the server-allow toggle (the
 * `@server` allowedTools grant) with its `@server/verb` auto-approve children
 * revealed on expand. The nesting IS the dependency — a verb cannot be
 * auto-approved while its server is off, and turning the server off cascades to
 * clear its verbs so the saved draft is coherent (never 409s).
 */
function ServerRow({ server, parent, verbs, parentOn, busy, serverChanged, globCovered, effectiveOn, pendingHas, onToggleServer, onToggleVerb }: {
  server: string
  parent: Grant | null
  verbs: Grant[]
  parentOn: boolean
  busy: boolean
  serverChanged: boolean
  /** The server is covered by an `@server` glob AND enumerates verbs. The
   *  backend treats glob + per-verb as mutually overlapping, so the only legal
   *  edit is all-on / all-off (the server toggle). Per-verb toggles are shown
   *  read-only — carving one out 409s (approval_exclusion_overlaps). */
  globCovered: boolean
  effectiveOn: (g: Grant) => boolean
  pendingHas: (g: Grant) => boolean
  onToggleServer: () => void
  onToggleVerb: (g: Grant) => void
}) {
  const [open, setOpen] = useState(false)
  const onCount = verbs.filter(effectiveOn).length
  const hasVerbs = verbs.length > 0
  // A parent `@server` grant that is ON auto-approves the WHOLE server, whether
  // or not individual verbs are also listed. So "the whole server is
  // auto-approved" is the honest subtitle both for the glob-covered case (verbs
  // present) and for a bare `@server` grant with no listed verbs — otherwise
  // the latter wrongly reads "No tools auto-approved" while the switch is on
  // and every tool runs unattended.
  const wholeServerOn = globCovered || (!!parent && parentOn && !hasVerbs)
  // Subtitle: when the whole server is auto-approved, say so (its verbs are not
  // individually editable here); otherwise the auto-approved count, or a plain
  // "nothing auto-approved" line — "0 of 0 auto-approved" reads as a bug.
  const subtitle = wholeServerOn
    ? i18nT('crewApprovalsEditor.server_glob_covered')
    : hasVerbs
      ? i18nT('crewApprovalsEditor.server_verb_count', { on: onCount, total: verbs.length })
      : i18nT('crewApprovalsEditor.server_no_verbs')
  return (
    <div className="flex flex-col gap-1.5" data-testid={`next-approvals-server-${server}`}>
      <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg-accent px-3 py-2">
        {hasVerbs ? (
          <button
            type="button"
            onClick={() => setOpen(o => !o)}
            aria-expanded={open}
            data-testid={`next-approvals-server-expand-${server}`}
            className="flex min-w-0 flex-1 items-center gap-2 text-left focus-ring"
          >
            <ChevronRight size={14} className={['lucide-inline shrink-0 text-muted transition-transform', open ? 'rotate-90' : ''].join(' ')} />
            <span className="flex min-w-0 flex-col">
              <span className="truncate font-mono text-[12.5px] text-text">{server}</span>
              <span className="truncate text-[11px] text-muted">{subtitle}</span>
            </span>
          </button>
        ) : (
          // No auto-approved verbs → nothing to expand; render a static label
          // (indented to align with the servers that do carry a chevron).
          <span className="flex min-w-0 flex-1 flex-col pl-[22px]">
            <span className="truncate font-mono text-[12.5px] text-text">{server}</span>
            <span className="truncate text-[11px] text-muted">{subtitle}</span>
          </span>
        )}
        <div className="flex shrink-0 items-center gap-2.5">
          {serverChanged && <span className="text-[10px] text-warn">{i18nT('crewApprovalsEditor.state_changed')}</span>}
          {/* The server-allow toggle — the one legal edit for a glob-covered
              server (all-on / all-off). Interactive whenever a parent grant
              exists; disabled only when there is no `@server` grant to flip. */}
          <button
            type="button"
            role="switch"
            aria-checked={parentOn}
            aria-label={i18nT('crewApprovalsEditor.server_allow_aria', { server })}
            data-testid={`next-approvals-server-toggle-${server}`}
            disabled={busy || !parent}
            onClick={onToggleServer}
            className={[
              'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus-ring disabled:cursor-not-allowed',
              parentOn ? 'bg-accent' : 'bg-border',
            ].join(' ')}
          >
            <span className={['inline-block h-4 w-4 rounded-full bg-bg transition-transform', parentOn ? 'translate-x-4' : 'translate-x-0.5'].join(' ')} />
          </button>
        </div>
      </div>
      {open && hasVerbs && (
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
          {verbs.map(v =>
            globCovered ? (
              // Glob-covered: the verb is auto-approved by the server-wide glob
              // and cannot be carved out here (the backend rejects it), so show
              // it read-only — name only, no toggle.
              <li
                key={capabilityKey({ section: v.section, id: v.id })}
                className="ml-4 flex items-center justify-between gap-3 rounded-md border border-border bg-bg-accent px-3 py-2 opacity-70"
                data-testid={`next-approval-readonly-${v.id}`}
              >
                <div className="flex min-w-0 flex-col">
                  <span className="truncate text-[12.5px] text-text">{v.label}</span>
                  <span className="truncate font-mono text-[11px] text-muted">{v.sublabel}</span>
                </div>
              </li>
            ) : (
              <ToggleRow
                key={capabilityKey({ section: v.section, id: v.id })}
                g={v}
                on={effectiveOn(v)}
                changed={pendingHas(v)}
                busy={busy}
                // A verb cannot be auto-approved while its server is off.
                disabled={!parentOn}
                indent
                onToggle={() => onToggleVerb(v)}
              />
            ),
          )}
        </ul>
      )}
    </div>
  )
}

export default function CrewApprovalsEditor({
  member,
  pending,
  setPending,
  preview,
  setPreview,
  onDirtyChange,
  onBusyChange,
  onSaved,
}: {
  member: string
  /** The draft state is OWNED BY THE DIALOG (lifted up), not local to this pane,
   *  so an unsaved approvals draft survives the pane unmounting when the user
   *  navigates to another section/tab — matching the classic editor, which kept
   *  the capabilities pane mounted. Keyed by capabilityKey; absent = unchanged. */
  pending: Record<string, boolean>
  setPending: Dispatch<SetStateAction<Record<string, boolean>>>
  preview: CapabilityPreview | null
  setPreview: Dispatch<SetStateAction<CapabilityPreview | null>>
  onDirtyChange?: (dirty: boolean) => void
  onBusyChange?: (busy: boolean) => void
  onSaved?: () => void
}) {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: crewCapabilitiesKey(member), queryFn: () => crewCapabilitiesApi.get(member), retry: false })
  const view = query.data

  const prepare = useMutation({
    mutationFn: (body: CapabilityDraft) => crewCapabilitiesApi.preview(member, body),
    onSuccess: setPreview,
  })
  const save = useMutation({
    mutationFn: (body: CapabilityDraft & { preview_token: string }) => crewCapabilitiesApi.save(member, body),
    onSuccess: async next => {
      qc.setQueryData(crewCapabilitiesKey(member), next)
      setPending({}); setPreview(null)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['agentDetail'] }),
        qc.invalidateQueries({ queryKey: ['agents-installed'] }),
        qc.invalidateQueries({ queryKey: ['kirocrew-agents'] }),
      ])
      // A first approvals save gives the mate a new private crew-<hex> binding,
      // so the dialog's kiroAgent (seeded once at open) now holds the OLD parent
      // template. Tell the host to re-seed from the refetched agent, else Cancel
      // prompts a phantom 'template' discard and footer Save PUTs the stale
      // kiro_agent, which the backend rejects — blocking the user's other edits.
      onSaved?.()
    },
    onError: () => setPreview(null),
  })

  const busy = prepare.isPending || save.isPending
  const dirty = Object.keys(pending).length > 0
  // Publish dirty/busy up so the dialog can guard close / disable its footer.
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => { onBusyChange?.(busy) }, [busy, onBusyChange])

  // The grantable set: every allowedTools + autoApprove row the view knows about.
  const grants: Grant[] = useMemo(() => {
    const rows = (view?.rows ?? []).filter(r => r.section === 'allowedTools' || r.section === 'autoApprove')
    return rows.map(toGrant).sort((a, b) => a.section === b.section ? compareText(a.label, b.label) : a.section === 'allowedTools' ? -1 : 1)
  }, [view])

  // Effective on/off = server state unless the user toggled it this session.
  const effectiveOn = (g: Grant) => {
    const key = capabilityKey({ section: g.section, id: g.id })
    return key in pending ? pending[key] : g.on
  }
  const pendingHas = (g: Grant) => capabilityKey({ section: g.section, id: g.id }) in pending

  // A mate not in `inherited` mode must enroll before edits apply. We enroll
  // implicitly on first toggle — quietly, no checkbox — matching the classic
  // pane's gate without surfacing its machinery.
  const needsEnroll = !!view && view.mode !== 'inherited'

  // Set/clear a single grant's pending override, mutating a draft map in place.
  // Returns to server state (deletes the key) when the target equals `g.on`.
  const applyToggle = (draft: Record<string, boolean>, g: Grant, target: boolean) => {
    const key = capabilityKey({ section: g.section, id: g.id })
    if (target === g.on) delete draft[key]
    else draft[key] = target
  }

  const toggle = (g: Grant) => {
    setPreview(null); prepare.reset(); save.reset()
    setPending(prev => {
      const next = { ...prev }
      applyToggle(next, g, !effectiveOn(g))
      return next
    })
  }

  // Toggling an MCP server parent cascades to its verbs, SYMMETRICALLY. The
  // backend treats the `@server` glob and its `@server/verb` entries as mutually
  // overlapping: a draft that sets/removes one while the other disagrees 409s
  // (approval_exclusion_overlaps). So the server toggle must move the glob AND
  // every verb together, in one draft — OFF clears them all, ON restores them
  // all to server state. Build from the live grant set so inherited verbs the
  // user never touched move too.
  const toggleServer = (parent: Grant, verbs: Grant[]) => {
    setPreview(null); prepare.reset(); save.reset()
    const target = !effectiveOn(parent)
    setPending(prev => {
      const next = { ...prev }
      applyToggle(next, parent, target)
      // Same target for every verb — OFF removes them, ON restores them (which,
      // for inherited-present verbs, clears the pending remove back to state).
      for (const v of verbs) applyToggle(next, v, target)
      return next
    })
  }

  const buildDraft = (): CapabilityDraft => {
    const operations: CapabilityOperation[] = Object.entries(pending).map(([key, on]) => {
      const [section, id] = JSON.parse(key) as ['allowedTools' | 'autoApprove', string]
      return on ? { section, id, action: 'set', value: true } : { section, id, action: 'remove' }
    })
    return { revision: view!.revision, enroll: needsEnroll, operations, accept_parent: [], accept_members: [] }
  }

  const onReview = () => prepare.mutate(buildDraft())
  const onSave = () => { if (preview) save.mutate({ ...buildDraft(), preview_token: preview.preview_token }) }
  const onDiscard = () => { setPending({}); setPreview(null); prepare.reset(); save.reset() }

  if (query.isLoading) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-muted" data-testid="next-approvals-loading">
        <Loader2 size={14} className="lucide-inline animate-spin" />
        {i18nT('crewApprovalsEditor.loading')}
      </div>
    )
  }
  if (query.error || !view || view.schema_version !== 1) {
    // No hand-off: the approvals read failing leaves no unsaved draft to lose,
    // and the hand-off would navigate away from the open editor, discarding the
    // enclosing crew fields — so keep it off explicitly.
    return <ErrorNotice message={i18nT('crewApprovalsEditor.load_error')} askAgent={false} testId="next-approvals-error" />
  }
  if (!view.template.available) {
    // No hand-off: a removed/unresolvable parent template is a read-side
    // resolution failure, and the hand-off would navigate away from the open
    // editor, discarding its unsaved crew fields and any approvals draft.
    return <ErrorNotice message={i18nT('crewApprovalsEditor.agent_unavailable')} askAgent={false} testId="next-approvals-unavailable" />
  }

  const onCount = grants.filter(effectiveOn).length
  const expanded = preview?.impact.some(i => i.approval_expanded)

  // Built-in tools stay a flat list: atomic allowedTools entries with no server.
  const toolGrants = grants.filter(g => g.kind === 'tool')

  // MCP servers: pair each server's `@server` allowedTools parent with its
  // `@server/verb` autoApprove children. A server may surface with only a
  // parent (no auto-approved verbs yet) or only verbs (allowed implicitly, no
  // explicit `@server` row) — key the group by server name and tolerate either.
  const serverGroups = (() => {
    const groups: Record<string, { parent: Grant | null; verbs: Grant[] }> = Object.create(null)
    const slot = (name: string) => (groups[name] ??= { parent: null, verbs: [] })
    for (const g of grants) {
      if (g.kind === 'server' && g.server) slot(g.server).parent = g
      else if (g.kind === 'verb' && g.server) slot(g.server).verbs.push(g)
    }
    return Object.entries(groups)
      .map(([server, grp]) => ({ server, ...grp }))
      .sort((a, b) => compareText(a.server, b.server))
  })()

  // Surface a saved-spec runtime failure the GET returns with a 200 + available
  // template (e.g. error_code='materialization_changed'); the classic pane did
  // this and without it the editor shows normal toggles over a mate that cannot
  // start. Non-blocking banner — the toggles stay usable below it.
  const runtimeFailed = view.runtime.status === 'failed'
  const runtimeDrifted = view.mode === 'inherited' && runtimeFailed && view.runtime.error_code === 'materialization_changed'
  const runtimeFailMessage = runtimeDrifted
    ? i18nT('crewApprovalsEditor.driftHint', { member })
    : i18nT(view.runtime.error_code ? 'crewApprovalsEditor.sourceFailed' : 'crewApprovalsEditor.runtime_failed')

  return (
    <div className="flex min-h-0 flex-col gap-4" data-testid="next-approvals">
      <p className="text-[12px] text-muted">
        {i18nT('crewApprovalsEditor.intro', { member })}
      </p>

      {/* No hand-off: a runtime-failed saved spec is a warning the user acts on
          here (fix the drift / reload), and the hand-off would discard their
          open approval draft. */}
      {runtimeFailed && (
        <ErrorNotice message={runtimeFailMessage} variant="inline" askAgent={false} testId="next-approvals-runtime-failed" />
      )}

      <div className="flex items-center justify-between gap-3">
        <span className="text-[11px] uppercase tracking-wide text-muted">{i18nT('crewApprovalsEditor.count', { on: onCount, total: grants.length })}</span>
        {dirty && <span className="text-[11px] text-warn">{i18nT('crewApprovalsEditor.unsaved')}</span>}
      </div>

      {grants.length === 0 ? (
        <div className="rounded-md border border-border bg-bg-accent px-3 py-2 text-[12px] text-muted">
          {i18nT('crewApprovalsEditor.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          {/* ── Tools (built-in) ─────────────────────────────────────────── */}
          {toolGrants.length > 0 && (
            <section className="flex flex-col gap-1.5" data-testid="next-approvals-tools">
              <h3 className="m-0 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-strong">{i18nT('crewApprovalsEditor.section_tools')}</h3>
              <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                {toolGrants.map(g => {
                  const key = capabilityKey({ section: g.section, id: g.id })
                  return <ToggleRow key={key} g={g} on={effectiveOn(g)} changed={key in pending} busy={busy} onToggle={() => toggle(g)} />
                })}
              </ul>
            </section>
          )}

          {/* ── MCP Servers (collapsible: server-allow parent + verb children) ─ */}
          {serverGroups.length > 0 && (
            <section className="flex flex-col gap-2.5" data-testid="next-approvals-mcp">
              <h3 className="m-0 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-strong">{i18nT('crewApprovalsEditor.section_mcp')}</h3>
              {serverGroups.map(({ server, parent, verbs }) => {
                // Parent "on" = server allowed. With an explicit `@server` grant
                // its effective state drives it; without one the server is
                // implicitly allowed (parent toggle is non-interactive).
                const parentOn = parent ? effectiveOn(parent) : true
                // Glob-covered = an `@server` glob (parent) that is ON AND
                // enumerated verbs. The backend treats the two as mutually
                // overlapping, so per-verb carve-out 409s; only the whole-server
                // toggle is legal. When the parent is OFF (switched off, or its
                // `@srv` row is `removed`/absent) the glob no longer covers the
                // verbs: the subtitle must not claim "auto-approved as a whole"
                // and the verbs become individually carve-able again.
                const globCovered = !!parent && parentOn && verbs.length > 0
                return (
                  <ServerRow
                    key={server}
                    server={server}
                    parent={parent}
                    verbs={verbs}
                    parentOn={parentOn}
                    busy={busy}
                    serverChanged={!!parent && pendingHas(parent)}
                    globCovered={globCovered}
                    effectiveOn={effectiveOn}
                    pendingHas={pendingHas}
                    onToggleServer={() => { if (parent) toggleServer(parent, verbs) }}
                    onToggleVerb={toggle}
                  />
                )
              })}
            </section>
          )}
        </div>
      )}

      {/* Review surfaces a ceiling-expansion warning before anything is written. */}
      {expanded && (
        <div className="rounded-md border border-warn/40 bg-bg-accent px-3 py-2 text-[12px] text-warn" data-testid="next-approvals-expanded">
          {i18nT('crewApprovalsEditor.expansion_warning')}
        </div>
      )}
      {(prepare.error || save.error) && (
        /* No hand-off: the toggles the user set stay on screen (dirty draft is
           preserved) so they can retry; surfaced through the shared ErrorNotice. */
        <ErrorNotice
          message={prepare.error ? i18nT('crewApprovalsEditor.preview_failed') : i18nT('crewApprovalsEditor.save_failed')}
          variant="inline"
          askAgent={false}
          testId="next-approvals-op-error"
        />
      )}

      {(dirty || runtimeDrifted) && (
        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-border pt-3">
          {dirty && (
            <button
              type="button"
              onClick={onDiscard}
              disabled={busy}
              className="rounded-md border border-border px-3 py-1.5 text-[12.5px] text-text focus-ring hover:bg-bg-hover"
            >
              {i18nT('crewApprovalsEditor.discard')}
            </button>
          )}
          <button
            type="button"
            data-testid="next-approvals-save"
            onClick={preview ? onSave : onReview}
            disabled={busy || (!!preview && preview.revision !== view.revision)}
            className="rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-bg focus-ring disabled:opacity-50"
          >
            {busy ? i18nT('crewApprovalsEditor.btn_working') : preview ? i18nT('crewApprovalsEditor.btn_save') : i18nT('crewApprovalsEditor.btn_review')}
          </button>
        </div>
      )}
    </div>
  )
}
