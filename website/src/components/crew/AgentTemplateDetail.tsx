/**
 * The agent template panel in the crew editor: the template SELECTOR is the
 * header bar of a panel that visibly contains the definition it names.
 *
 * Blueprint semantics (fork-on-first-edit) shape every state here:
 * - Shared/built-in/package template bound: header shows source + reach, the
 *   helper line says an edit gives this agent its own copy, and the first
 *   edit routes through `forkIfNeeded`.
 * - The crew's own copy bound: header shows the ORIGIN name (the copy's
 *   auto-derived filename is bookkeeping, not identity), a Customized tag,
 *   a change-count popover diffed live against the origin, and two actions —
 *   Reset (rebind to origin + remove the copy) and Save as new template
 *   (publish under a user-chosen name; the one place a template name is
 *   ever deliberately chosen).
 *
 * Switching away from a customized state and Reset both confirm first: the
 * fresh-eyes usability review found unscoped destruction was the #1 reason
 * users would avoid the control entirely.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Lock } from 'lucide-react'
import { api } from '../../api/client'
import AgentSkillsEditor from '../AgentSkillsEditor'
import ErrorNotice from '../ErrorNotice'
import SimpleSelect from '../SimpleSelect'
import InfoTip from '../InfoTip'
import { useConfirm } from '../ConfirmDialog'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover'
import { i18nT } from '../../i18n/t'
import { templateSourceBadge, type TemplateProvenance } from '../../lib/templateSource'

/** How many chips render before the list collapses behind a "+N more". */
const CHIP_CAP = 6
/** The detail endpoint truncates a long prompt; say so rather than implying full text. */
const PROMPT_CAP = 2000
/** Mirror of the backend's template-name rule (a filename is permanent identity). */
const TEMPLATE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/

interface InstalledAgent {
  name: string
  description?: string
  source?: string
  kirocrew_owned?: boolean
  model?: string
  skills?: string[]
  package?: string
  filename?: string
  forked_from?: string
  private_to?: string
}

interface TemplateDetail extends Partial<InstalledAgent> {
  name: string
  prompt?: string
  tools?: string[]
  allowedTools?: string[]
  mcpServers?: Record<string, { args?: string[] }>
  toolsSettings?: { execute_bash?: { deniedCommands?: string[] } }
  unmanaged_skills?: string[]
}

interface CrewRow {
  name: string
  kiro_agent: string
}

/** One diffable section of the definition, flattened to a comparable string. */
interface SectionDiff {
  key: string
  label: string
  now: string
  was: string
}

function sectionValues(d: TemplateDetail | undefined): Record<string, string> {
  return {
    model: d?.model || '',
    skills: (d?.skills || []).join(', '),
    tools: (d?.tools || []).join(', '),
    allowed: (d?.allowedTools || []).join(', '),
    mcp: Object.keys(d?.mcpServers || {}).join(', '),
    prompt: d?.prompt || '',
    guardrails: (d?.toolsSettings?.execute_bash?.deniedCommands || []).join(', '),
  }
}

const SECTION_LABEL_KEYS: Record<string, string> = {
  model: 'pages.kiroCrewAgentsPage.model',
  skills: 'pages.agentsPage.skills',
  tools: 'pages.agentsPage.tools_and_mcp',
  allowed: 'pages.agentsPage.auto_approved',
  mcp: 'pages.agentsPage.mcp_servers',
  prompt: 'pages.agentsPage.system_prompt',
  guardrails: 'pages.agentsPage.guardrails',
}

/** Section label with the list's true total, which a capped list cannot show.
 *  `changed` marks a customized field — only while the changes popover is
 *  open (detail on demand, never a permanent marker). */
function Label({ text, count, changed, children }: {
  text: string; count?: number | string; changed?: boolean; children?: React.ReactNode
}) {
  return (
    <div className="mb-1.5 mt-4 flex items-center gap-2">
      <span className="text-[10.5px] font-medium uppercase tracking-[.13em] text-muted">{text}</span>
      {count !== undefined && <span className="font-mono text-[10.5px] text-muted/70">{count}</span>}
      {changed && <span className="h-1.5 w-1.5 rounded-full bg-accent" aria-hidden="true" />}
      {children}
    </div>
  )
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="m-0 mt-1.5 text-[11px] leading-relaxed text-muted/80">{children}</p>
}

/** Chip list capped at CHIP_CAP, with the remainder behind a toggle. */
function Chips({ items, tone }: { items: string[]; tone?: 'ok' | 'aim' | 'danger' }) {
  const [open, setOpen] = useState(false)
  const hidden = items.length - CHIP_CAP
  const shown = open ? items : items.slice(0, CHIP_CAP)
  const cls =
    tone === 'ok'
      ? 'bg-ok/10 border-ok/30 text-ok'
      : tone === 'aim'
        ? 'bg-aim-subtle border-aim/30 text-aim'
        : tone === 'danger'
          ? 'bg-danger/10 border-danger/30 text-danger/80'
          : 'bg-bg-elevated border-border text-text'
  return (
    <>
      <div className="flex flex-wrap gap-1.5">
        {shown.map(s => (
          <span key={s} className={`rounded-md border px-2 py-0.5 font-mono text-[11.5px] ${cls}`}>{s}</span>
        ))}
      </div>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="mt-1.5 cursor-pointer border-0 bg-transparent p-0 text-[11px] text-accent"
        >
          {open
            ? i18nT('components.agentTemplateDetail.show_fewer')
            : i18nT('components.agentTemplateDetail.show_more', { count: hidden })}
        </button>
      )}
    </>
  )
}

export default function AgentTemplateDetail({
  template, models, crew, onForked, options, onSelect, provenance, fieldLabel,
}: {
  /** The crew's current binding (the copy's name when customized). */
  template: string
  models: string[]
  /** The crew this pane is edited FROM. Present, edits get blueprint semantics. */
  crew?: string
  /** Called with the private copy's name after a fork — the parent editor must
   *  rebind its state or a later Save would silently undo the fork. */
  onForked?: (template: string) => void
  /** Selectable template catalog (private copies already filtered by the page). */
  options: string[]
  /** Rebinds the parent editor's template state (commits on the editor's Save). */
  onSelect: (v: string) => void
  provenance?: Record<string, TemplateProvenance>
  /** Accessible name for the selector (the provider's field label). */
  fieldLabel: string
}) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [changesOpen, setChangesOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [publishName, setPublishName] = useState('')
  const [publishError, setPublishError] = useState('')

  const { data: detail, isLoading, isError } = useQuery<TemplateDetail>({
    queryKey: ['agentDetail', template],
    queryFn: () => api.agentDetail(template),
    enabled: !!template,
  })
  // `agentsInstalled` answers a plain ARRAY, not `{agents}` — and it carries the
  // provenance this panel states (source, package, filename, fork lineage).
  const { data: installed } = useQuery<InstalledAgent[]>({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
  })
  const { data: crewsData } = useQuery<{ agents?: CrewRow[] }>({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
  })

  const listed = (Array.isArray(installed) ? installed : []).find(a => a.name === template)
  const isOwnCopy = !!crew && listed?.private_to === crew
  const origin = isOwnCopy ? listed?.forked_from || '' : ''

  // The origin's spec, for the live diff behind the change-count pill. Origin
  // may have been deleted since the fork; the pill then simply hides.
  const { data: originDetail } = useQuery<TemplateDetail>({
    queryKey: ['agentDetail', origin],
    queryFn: () => api.agentDetail(origin),
    enabled: !!origin,
  })

  const changes: SectionDiff[] = useMemo(() => {
    if (!isOwnCopy || !detail || !originDetail) return []
    const now = sectionValues(detail)
    const was = sectionValues(originDetail)
    return Object.keys(SECTION_LABEL_KEYS)
      .filter(k => now[k] !== was[k])
      .map(k => ({ key: k, label: i18nT(SECTION_LABEL_KEYS[k]), now: now[k], was: was[k] }))
  }, [isOwnCopy, detail, originDetail])
  const changedKeys = useMemo(() => new Set(changes.map(c => c.key)), [changes])

  // Blueprint semantics: resolve every edit's write target. Already the crew's
  // own copy -> write through unchanged. Shared -> fork first (auto-named, no
  // user step), rebind the parent's dropdown, write the edit to the copy. Two
  // quick edits before the provenance refresh share ONE in-flight fork call:
  // a second api.agentFork would race the rebind and lose to stale_binding.
  const forkInFlight = useRef<{ tpl: string; p: Promise<string> } | null>(null)
  // A resolved cache outlives its fork: after Reset the template prop returns
  // to the origin stem the cache is keyed on while the fork file is deleted,
  // so any binding change drops the cache and the next edit forks fresh.
  useEffect(() => {
    forkInFlight.current = null
  }, [template, crew])
  const forkIfNeeded = (): Promise<string> => {
    if (!crew || isOwnCopy) return Promise.resolve(template)
    if (forkInFlight.current?.tpl === template) return forkInFlight.current.p
    const p = (async () => {
      const r = (await api.agentFork(template, crew)) as { template?: string }
      const next = r?.template || template
      onForked?.(next)
      qc.invalidateQueries({ queryKey: ['agents-installed'] })
      qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
      return next
    })()
    // A failed fork must not poison later edits with a rejected cached promise.
    forkInFlight.current = {
      tpl: template,
      p: p.catch(err => {
        forkInFlight.current = null
        throw err
      }),
    }
    return forkInFlight.current.p
  }

  const patchModel = useMutation({
    mutationFn: async (model: string) => {
      const target = await forkIfNeeded()
      return api.agentPatch(target, { model })
    },
    // Prefix-scoped: a fork changes which agentDetail key is live mid-flight.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agentDetail'] }),
  })

  // The header hint claims "saved as you go", so a failed save must render:
  // silence here means the user walks away believing a save that never landed.
  const [actionError, setActionError] = useState('')

  const afterRebind = (next: string) => {
    onSelect(next)
    qc.invalidateQueries({ queryKey: ['agents-installed'] })
    qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    qc.invalidateQueries({ queryKey: ['agentDetail'] })
  }

  /** Switching away from a customized state discards the copy's edits — the
   *  review's finding: warn with the concrete consequence, never silently. */
  const handleSelect = async (v: string) => {
    if (!v || v === template) return
    if (!isOwnCopy) {
      onSelect(v)
      return
    }
    const ok = await confirm({
      title: i18nT('components.agentTemplateDetail.switch_confirm_title', { name: v }),
      body: i18nT('components.agentTemplateDetail.switch_confirm_body', {
        origin, fields: changes.map(c => c.label).join(', ') || i18nT('components.agentTemplateDetail.change_not_set'),
      }),
      confirmLabel: i18nT('components.agentTemplateDetail.discard_and_switch'),
    })
    if (ok) onSelect(v)
  }

  /** Rebind to the origin and remove the superseded copy. Two calls on
   *  existing endpoints; the delete is best-effort (an orphaned copy is
   *  hidden everywhere and harmless, so its failure must not block reset). */
  const resetToOrigin = async () => {
    if (!crew || !origin) return
    const ok = await confirm({
      title: i18nT('components.agentTemplateDetail.reset_confirm_title', { name: origin }),
      body: i18nT('components.agentTemplateDetail.reset_confirm_body', {
        name: origin, fields: changes.map(c => c.label).join(', '),
      }),
      confirmLabel: i18nT('components.agentTemplateDetail.discard_my_changes'),
    })
    if (!ok) return
    try {
      await api.updateKirocrewAgent(crew, { kiro_agent: origin })
    } catch {
      // Rebind is the reset: if it fails nothing changed, so say so.
      setActionError(i18nT('components.agentTemplateDetail.save_failed'))
      return
    }
    setActionError('')
    await api.agentDelete(template).catch(() => undefined)
    afterRebind(origin)
  }

  const submitPublish = async () => {
    if (!crew) return
    const name = publishName.trim()
    if (!TEMPLATE_NAME_RE.test(name)) {
      setPublishError(i18nT('components.agentTemplateDetail.publish_name_rule'))
      return
    }
    try {
      const r = (await api.agentPublish(template, crew, name)) as { template?: string }
      setPublishOpen(false)
      setPublishName('')
      setPublishError('')
      afterRebind(r?.template || name)
    } catch (e) {
      setPublishError(e instanceof Error ? e.message : String(e))
    }
  }

  const usedBy = (crewsData?.agents || []).filter(c => c.kiro_agent === template)
  const sourceLabel = templateSourceBadge(listed)
  // Re-add only the value the select DISPLAYS when the catalog lacks it (a
  // legacy/unknown binding). An own-copy header shows the origin, so the
  // copy's bookkeeping filename must never appear as a selectable row.
  const shown = isOwnCopy ? origin : template
  const opts = !shown || options.includes(shown) ? options : [shown, ...options]
  const tools = detail?.tools || []
  const allowed = detail?.allowedTools || []
  const mcp = Object.keys(detail?.mcpServers || {})
  const denied = detail?.toolsSettings?.execute_bash?.deniedCommands || []
  const prompt = detail?.prompt || ''
  const promptIsFile = prompt.startsWith('file://')

  return (
    <div className="mt-1 rounded-[10px] border border-border-strong">
      {/* The selector IS the panel's header: everything inside the panel is
          what the selected template defines, so containment states the scope
          no sentence has to. Sticky so a long definition never leaves the
          reader guessing which template — or whose copy — they are in. */}
      <div className="sticky top-0 z-[3] flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-t-[10px] border-b border-border-strong bg-bg-elevated px-3.5 py-2">
        <span className="text-[10px] uppercase tracking-[.12em] text-muted">
          {i18nT('components.agentTemplateDetail.template_prefix')}
        </span>
        <span className="border-b border-dashed border-border-strong">
          <SimpleSelect
            options={opts}
            optionBadges={opts.map(o => {
              const p = provenance?.[o]
              const label = templateSourceBadge(p)
              return label ? { label, source: p?.source ?? '' } : undefined
            })}
            labelsInListOnly
            // The header shows the ORIGIN for a customized copy: the copy's
            // filename is bookkeeping, not something the user chose or knows.
            value={isOwnCopy ? origin : template}
            onChange={handleSelect}
            triggerFallback={i18nT('pages.kiroCrewAgentsPage.select_an_agent_template')}
            aria-label={fieldLabel}
            className="h-auto min-w-0 border-0 bg-transparent px-1 py-0.5 text-[13.5px] font-semibold shadow-none"
            contentClassName="min-w-[300px]"
          />
        </span>
        {isOwnCopy && (
          <span className="rounded border border-accent/40 bg-accent/10 px-1.5 py-px text-[10px] text-accent">
            {i18nT('components.agentTemplateDetail.customized')}
          </span>
        )}
        {isOwnCopy && changes.length > 0 && (
          <Popover open={changesOpen} onOpenChange={setChangesOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                className="inline-flex cursor-pointer items-center gap-0.5 rounded-full border border-accent/40 bg-accent/10 px-2 py-px text-[10.5px] text-accent"
              >
                {i18nT('components.agentTemplateDetail.changes_count', { count: changes.length })}
                <ChevronDown className="h-3 w-3" aria-hidden />
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" sideOffset={6} className="w-[300px] bg-card p-3 text-[11.5px] shadow-lg">
              {changes.map(c => (
                <div key={c.key} className="flex items-baseline justify-between gap-3 py-0.5">
                  <span className="font-semibold">{c.label}</span>
                  <span className="min-w-0 text-right">
                    <span className="break-all">{c.now || i18nT('components.agentTemplateDetail.change_not_set')}</span>{' '}
                    <span className="text-[10.5px] text-muted/70">
                      {i18nT('components.agentTemplateDetail.change_was', {
                        value: c.was || i18nT('components.agentTemplateDetail.change_not_set'),
                      })}
                    </span>
                  </span>
                </div>
              ))}
              {/* Recovery lives with the list it undoes — and keeps the header
                  row at two action controls. */}
              <button
                type="button"
                onClick={resetToOrigin}
                className="mt-2 cursor-pointer border-0 bg-transparent p-0 text-[11.5px] text-muted hover:text-text"
              >
                {i18nT('components.agentTemplateDetail.reset_my_changes')}
              </button>
            </PopoverContent>
          </Popover>
        )}
        {!isOwnCopy && sourceLabel && (
          <span className="rounded border border-border-strong px-1.5 py-px text-[10px] text-muted">
            {sourceLabel}
          </span>
        )}
        {!isOwnCopy && !!template && (
          <span className="text-[11px] text-muted/80">
            {usedBy.length > 1
              ? i18nT('components.agentTemplateDetail.used_by_count', { count: usedBy.length })
              : i18nT('components.agentTemplateDetail.used_by_this_agent_only')}
          </span>
        )}
        {isOwnCopy && (
          <span className="ml-auto flex items-center gap-3.5 whitespace-nowrap">
            <button
              type="button"
              onClick={() => setPublishOpen(true)}
              className="cursor-pointer border-0 bg-transparent p-0 text-[11.5px] text-accent"
            >
              {i18nT('components.agentTemplateDetail.save_as_new_template')}
            </button>
          </span>
        )}
      </div>

      <div className="px-4 pb-4">
        {/* One mental model in one sentence: where the definition comes from
            and how far an edit reaches. The file name is bookkeeping — behind
            the info tip, not in the reading line. */}
        <Hint>
          {isOwnCopy
            ? i18nT('components.agentTemplateDetail.based_on_hint', { name: origin })
            : template
              ? i18nT('components.agentTemplateDetail.edits_make_own_copy')
              : i18nT('pages.kiroCrewAgentsPage.the_agent_definition_it_boots_from_tools_mcp_ser')}
          {listed?.filename && (
            <span className="ml-1 inline-flex translate-y-px align-middle">
              <InfoTip
                text={i18nT('components.agentTemplateDetail.config_file_info', { filename: listed.filename })}
              />
            </span>
          )}
        </Hint>

        {/* No hand-off (no askAgent): a failed instant-save is fixed by
            re-clicking the same control, and the hand-off would close the
            sheet and discard the user's in-progress customization context. */}
        {actionError && <ErrorNotice variant="inline" className="mt-2" message={actionError} />}

        {!template ? null : isLoading ? (
          <p className="m-0 mt-4 text-[12px] italic text-muted">{i18nT('components.agentTemplateDetail.loading')}</p>
        ) : isError || !detail ? (
          <ErrorNotice
            className="mt-4"
            message={i18nT('components.agentTemplateDetail.could_not_load')}
            askAgent
          />
        ) : (
          <>
            <Label text={i18nT('pages.kiroCrewAgentsPage.model')} changed={changesOpen && changedKeys.has('model')} />
            <div className="max-w-[320px]">
              <SimpleSelect
                // The pinned value leads the list when the advertised set does
                // not carry it: a template pinned to a model this account no
                // longer sees would otherwise lie "no model pinned".
                options={
                  detail.model && !models.includes(detail.model)
                    ? ['', detail.model, ...models]
                    : ['', ...models]
                }
                value={detail.model || ''}
                onChange={m =>
                  patchModel.mutate(m, {
                    onError: () => setActionError(i18nT('components.agentTemplateDetail.save_failed')),
                    onSuccess: () => setActionError(''),
                  })
                }
                clearLabel={i18nT('components.agentTemplateDetail.no_model_pinned')}
                aria-label={i18nT('pages.kiroCrewAgentsPage.model')}
              />
            </div>
            <Hint>{i18nT('components.agentTemplateDetail.model_hint')}</Hint>

            {/* No Label here: AgentSkillsEditor renders its own "Skills" heading. */}
            {detail.skills === undefined ? (
              <>
                <Label text={i18nT('pages.agentsPage.skills')} />
                {/* askAgent: a read failure with no draft to lose — same
                    decision as the detail-load notice above. */}
                <ErrorNotice
                  variant="inline"
                  askAgent
                  message={i18nT('pages.agentsPage.could_not_load_this_agent_s_configuration_skills')}
                />
              </>
            ) : (
              <div className="mt-4">
                <AgentSkillsEditor
                  agentName={template}
                  skills={detail.skills}
                  unmanaged={detail.unmanaged_skills}
                  beforeSave={crew ? forkIfNeeded : undefined}
                  onChange={() => qc.invalidateQueries({ queryKey: ['agentDetail'] })}
                />
              </div>
            )}

            <Label text={i18nT('pages.agentsPage.tools_and_mcp')} count={tools.length} changed={changesOpen && changedKeys.has('tools')} />
            {tools.length ? <Chips items={tools} /> : <Hint>{i18nT('pages.agentsPage.not_set_for_this_template')}</Hint>}

            <Label text={i18nT('pages.agentsPage.auto_approved')} count={allowed.length} changed={changesOpen && changedKeys.has('allowed')} />
            {allowed.length ? <Chips items={allowed} tone="ok" /> : <Hint>{i18nT('pages.agentsPage.not_set_for_this_template')}</Hint>}
            <Hint>{i18nT('components.agentTemplateDetail.auto_approved_hint')}</Hint>

            <Label text={i18nT('pages.agentsPage.mcp_servers')} count={mcp.length} changed={changesOpen && changedKeys.has('mcp')} />
            {mcp.length ? <Chips items={mcp} tone="aim" /> : <Hint>{i18nT('pages.agentsPage.not_set_for_this_template')}</Hint>}

            <Label
              text={i18nT('pages.agentsPage.system_prompt')}
              count={promptIsFile ? undefined : prompt.length}
              changed={changesOpen && changedKeys.has('prompt')}
            />
            {!prompt ? (
              <Hint>{i18nT('pages.agentsPage.not_set_for_this_template')}</Hint>
            ) : promptIsFile ? (
              <div className="rounded-md border border-border bg-bg-elevated px-3 py-2.5">
                <p className="m-0 text-[11.5px] leading-relaxed text-muted">
                  {i18nT('components.agentTemplateDetail.prompt_lives_in_a_file')}
                </p>
                <p className="m-0 mt-1 break-all font-mono text-[11.5px] text-accent">{prompt}</p>
              </div>
            ) : (
              <>
                <pre className="m-0 max-h-[180px] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg-elevated p-3 font-mono text-[12px] leading-relaxed text-text">
                  {prompt}
                </pre>
                {prompt.length >= PROMPT_CAP && (
                  <Hint>{i18nT('components.agentTemplateDetail.prompt_truncated', { count: PROMPT_CAP })}</Hint>
                )}
              </>
            )}

            <Label text={i18nT('pages.agentsPage.guardrails')} count={denied.length} changed={changesOpen && changedKeys.has('guardrails')} />
            {denied.length ? (
              <>
                <Hint>{i18nT('components.agentTemplateDetail.guardrails_hint')}</Hint>
                <p className="m-0 mb-1.5 flex items-center gap-1.5 text-[11px] text-muted">
                  <Lock className="lucide-inline text-danger" aria-hidden="true" />
                  {i18nT('pages.agentsPage.comes_with_the_template_read_only_here')}
                </p>
                <Chips items={denied} tone="danger" />
              </>
            ) : (
              <Hint>{i18nT('pages.agentsPage.not_set_for_this_template')}</Hint>
            )}
          </>
        )}
      </div>

      {confirmDialog}

      <Dialog open={publishOpen} onOpenChange={o => { setPublishOpen(o); if (!o) setPublishError('') }}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>{i18nT('components.agentTemplateDetail.publish_title')}</DialogTitle>
          </DialogHeader>
          <p className="m-0 text-[12px] leading-relaxed text-muted">
            {i18nT('components.agentTemplateDetail.publish_hint')}
          </p>
          <label className="mt-2 block text-[10.5px] font-medium uppercase tracking-[.13em] text-muted">
            {i18nT('components.agentTemplateDetail.publish_name_label')}
            <input
              aria-label={i18nT('components.agentTemplateDetail.publish_name_label')}
              value={publishName}
              onChange={e => { setPublishName(e.target.value); setPublishError('') }}
              onKeyDown={e => { if (e.key === 'Enter') void submitPublish() }}
              className="mt-1.5 block w-full rounded-md border border-border-strong bg-bg-elevated px-2.5 py-1.5 font-mono text-[12.5px] text-text outline-none focus:border-accent"
            />
          </label>
          {/* No askAgent: the hand-off unmounts the dialog and destroys the
              unsaved publish-name draft — exactly the case the prop's opt-in
              default exists to protect. */}
          <ErrorNotice variant="inline" className="mt-1" message={publishError} />
          <div className="mt-3 flex justify-end gap-2.5">
            <button
              type="button"
              onClick={() => setPublishOpen(false)}
              className="cursor-pointer rounded-md border border-border-strong bg-bg-elevated px-3.5 py-1.5 text-[12px] text-text"
            >
              {i18nT('components.confirmDialog.cancel')}
            </button>
            <button
              type="button"
              onClick={() => void submitPublish()}
              disabled={!publishName.trim()}
              className="cursor-pointer rounded-md border border-accent/50 bg-accent/15 px-3.5 py-1.5 text-[12px] text-accent disabled:cursor-default disabled:opacity-50"
            >
              {i18nT('components.agentTemplateDetail.publish_action')}
            </button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
