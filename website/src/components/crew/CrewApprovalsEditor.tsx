/**
 * CrewApprovalsEditor — the New editor's Mate Details → Approvals surface.
 *
 * Scope by design: this edits ONLY auto-approve — of the capabilities the mate's
 * agent already provides, which ones run WITHOUT asking. Adding a tool / MCP
 * server / skill is a CUSTOM-AGENT concern (you cannot auto-approve a capability
 * the agent does not have), so there is deliberately no "add" affordance here;
 * this is purely the per-mate layer on top of the agent's capability set.
 *
 * It drives the SAME crewCapabilitiesApi draft → preview → save path the classic
 * CrewCapabilitiesPane uses, so the security-ceiling checks are the real ones —
 * but with a calm toggle list instead of the dense classic chrome (no reload,
 * enrollment checkbox, parent/version badges, or nested category tablist).
 *
 * Auto-approve has two kinds, both of which skip the prompt:
 *   - allowedTools: built-in tool NAMES (e.g. fs_write)
 *   - autoApprove:  per-MCP-server verbs, keyed @server/verb
 * Each grantable item is a row with an on/off toggle. "On" means the mate runs it
 * unattended; "off" means it asks first. Inherited-from-template vs this-mate-local
 * is marked lightly.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import {
  capabilityKey, crewCapabilitiesApi, crewCapabilitiesKey,
  type CapabilityDraft, type CapabilityOperation, type CapabilityPreview, type CapabilityRow,
} from '../../api/crewCapabilities'
import { toolMeta } from './toolMeta'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../ErrorNotice'

/** A single grantable auto-approve item, resolved for display. */
interface Grant {
  section: 'allowedTools' | 'autoApprove'
  id: string
  /** For autoApprove grants, the MCP server that provides the tool. */
  server: string | null
  /** Current effective on/off (present = auto-approved). */
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
    return { section: 'autoApprove', id: r.id, server, on: r.present, state: r.state, label: tool, sublabel: tool }
  }
  const m = toolMeta(r.id)
  return {
    section: r.section === 'autoApprove' ? 'autoApprove' : 'allowedTools',
    id: r.id,
    server: null,
    on: r.present,
    state: r.state,
    label: m.label || r.label || r.id,
    sublabel: r.id,
  }
}

/** One auto-approve toggle row, shared by the Tools and MCP Servers sections. */
function ToggleRow({ g, on, changed, busy, onToggle }: {
  g: Grant; on: boolean; changed: boolean; busy: boolean; onToggle: () => void
}) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg-accent px-3 py-2">
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
          disabled={busy}
          onClick={onToggle}
          className={[
            'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus-ring',
            on ? 'bg-accent' : 'bg-border',
          ].join(' ')}
        >
          <span className={['inline-block h-4 w-4 rounded-full bg-bg transition-transform', on ? 'translate-x-4' : 'translate-x-0.5'].join(' ')} />
        </button>
      </div>
    </li>
  )
}

export default function CrewApprovalsEditor({
  member,
  onDirtyChange,
  onBusyChange,
  onSaved,
}: {
  member: string
  onDirtyChange?: (dirty: boolean) => void
  onBusyChange?: (busy: boolean) => void
  onSaved?: () => void
}) {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: crewCapabilitiesKey(member), queryFn: () => crewCapabilitiesApi.get(member), retry: false })
  const view = query.data

  // Pending on/off overrides keyed by capabilityKey; absent = unchanged from server.
  const [pending, setPending] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<CapabilityPreview | null>(null)

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
    return rows.map(toGrant).sort((a, b) => a.section === b.section ? a.label.localeCompare(b.label) : a.section === 'allowedTools' ? -1 : 1)
  }, [view])

  // Effective on/off = server state unless the user toggled it this session.
  const effectiveOn = (g: Grant) => {
    const key = capabilityKey({ section: g.section, id: g.id })
    return key in pending ? pending[key] : g.on
  }

  // A mate not in `inherited` mode must enroll before edits apply. We enroll
  // implicitly on first toggle — quietly, no checkbox — matching the classic
  // pane's gate without surfacing its machinery.
  const needsEnroll = !!view && view.mode !== 'inherited'

  const toggle = (g: Grant) => {
    const key = capabilityKey({ section: g.section, id: g.id })
    setPreview(null); prepare.reset(); save.reset()
    setPending(prev => {
      const next = { ...prev }
      const target = !effectiveOn(g)
      if (target === g.on) delete next[key]   // back to server state → not a change
      else next[key] = target
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

  // Split into the two auto-approve kinds, MCP grouped by server.
  const toolGrants = grants.filter(g => g.section === 'allowedTools')
  const mcpServers = Object.entries(
    grants.filter(g => g.section === 'autoApprove').reduce<Record<string, Grant[]>>((acc, g) => {
      const key = g.server ?? i18nT('crewApprovalsEditor.unknown_server')
      ;(acc[key] ??= []).push(g)
      return acc
    }, Object.create(null)),
  ).sort((a, b) => a[0].localeCompare(b[0]))

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

          {/* ── MCP Servers (grouped by server) ──────────────────────────── */}
          {mcpServers.length > 0 && (
            <section className="flex flex-col gap-2.5" data-testid="next-approvals-mcp">
              <h3 className="m-0 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-strong">{i18nT('crewApprovalsEditor.section_mcp')}</h3>
              {mcpServers.map(([server, serverGrants]) => (
                <div key={server} className="flex flex-col gap-1.5">
                  <span className="font-mono text-[11px] text-muted">{server}</span>
                  <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                    {serverGrants.map(g => {
                      const key = capabilityKey({ section: g.section, id: g.id })
                      return <ToggleRow key={key} g={g} on={effectiveOn(g)} changed={key in pending} busy={busy} onToggle={() => toggle(g)} />
                    })}
                  </ul>
                </div>
              ))}
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
