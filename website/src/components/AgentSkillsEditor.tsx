import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Brain, ChevronDown, Lock, Plus, X } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import InfoTip from './InfoTip'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { isTouchDevice } from '../utils/isTouchDevice'

import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
/** A row from `GET /api/skills` — only the fields this editor needs. */
export interface CatalogSkill {
  key: string
  name: string
  description?: string
  source?: string
}

interface Props {
  /** Agent template name (the `{name}` in `/api/agents/detail/{name}`). */
  agentName: string
  /** Catalog keys currently mapped via the agent's `skill://` resources. */
  skills: string[]
  /**
   * `skill://` URIs the catalog cannot express — wildcard patterns and paths
   * outside every known skill root. Shown read-only: the backend preserves them
   * across writes, so listing them here explains why an agent may load more
   * than the editable chips suggest.
   */
  unmanaged?: string[]
  /**
   * Called after a successful save with the agent the save was issued FOR and
   * its new key list. The name is passed back because a slow PATCH can resolve
   * after the user has selected a different agent — the caller must ignore a
   * response that no longer matches what is on screen, or agent A's skills land
   * on agent B and the next edit writes them to B's spec.
   */
  onChange: (agentName: string, skills: string[]) => void
  /**
   * Resolves the template the edit should actually be written to, called just
   * before each save. The Agent Template pane uses it for blueprint semantics:
   * editing from a crew forks a private copy first and returns the copy's
   * name, so the shared template file is never mutated. Omitted, the save
   * writes to `agentName` (the Agent Templates tab's direct-edit behavior).
   */
  beforeSave?: () => Promise<string>
  /**
   * A shared instant-save chain each save serializes onto. The owner can then
   * drain ONE promise before an action that snapshots the spec file (publish)
   * and know every queued edit has landed. Optional — omitted, saves run
   * unchained (the Agent Templates tab has no such action).
   */
  pendingChain?: React.MutableRefObject<Promise<unknown>>
  /** Reports whether a save is in flight, so the owner can fence publish. */
  onSavePending?: (pending: boolean) => void
}

/**
 * Add/remove the skills an agent template maps.
 *
 * Writes through `PATCH /api/agents/detail/{name}` with `{ skills: [...] }`,
 * which the backend materializes as kiro-cli-native `skill://` entries in the
 * agent's `resources`. Each edit saves immediately (same interaction model as
 * the model picker on this page) — there is no separate Save button to forget.
 *
 * The popup must stay a Radix Popover, not a portal to `document.body`: it
 * renders inside a modal dialog, whose pointer-events cut and FocusScope leave
 * anything outside its layer stack unclickable and unfocusable.
 */
export default function AgentSkillsEditor({ agentName, skills, unmanaged = [], onChange, beforeSave, pendingChain, onSavePending }: Props) {
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const btnRef = useRef<HTMLButtonElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const { data: catalog = [] } = useQuery<CatalogSkill[]>({
    queryKey: ['skills-catalog'],
    queryFn: async () => {
      const rows = await api.skills()
      return Array.isArray(rows) ? (rows as CatalogSkill[]).filter(s => s?.key) : []
    },
    staleTime: 30_000,
  })

  const byKey = useMemo(() => {
    const m = new Map<string, CatalogSkill>()
    for (const s of catalog) m.set(s.key, s)
    return m
  }, [catalog])

  // Candidates = catalog minus what's already mapped, name-sorted for a stable
  // list regardless of the catalog's source-grouped order.
  const candidates = useMemo(
    () => catalog.filter(s => !skills.includes(s.key)).sort((a, b) => a.name.localeCompare(b.name)),
    [catalog, skills],
  )

  const filtered = useMemo(
    () => filter
      ? candidates.filter(s => s.name.toLowerCase().includes(filter.toLowerCase()))
      : candidates,
    [candidates, filter],
  )

  // Not in onOpenChange: Radix only calls that for closes it initiates itself,
  // so a filter typed before a select/Escape close would leak into the next open.
  useEffect(() => { if (!open) setFilter('') }, [open])

  const save = useMutation({
    // The agent name travels WITH the request so the response can be matched to
    // the agent it was issued for, not to whatever is selected when it lands.
    // `beforeSave` may redirect the write to a just-forked private copy; the
    // resolved target is what onChange reports, so the caller tracks the copy.
    mutationFn: async ({ agent, next }: { agent: string; next: string[] }) => {
      // Chained onto the caller's shared instant-save chain when one is
      // provided: an action that snapshots the file (publish) can then drain
      // ONE promise and know every queued edit — model pick or skill toggle —
      // has landed first.
      const run = (pendingChain?.current ?? Promise.resolve())
        .catch(() => undefined)
        .then(async () => {
          const target = beforeSave ? await beforeSave() : agent
          const res = await api.agentPatch(target, { skills: next })
          return { res: res as { skills?: string[] }, target }
        })
      if (pendingChain) pendingChain.current = run
      return run
    },
    onMutate: () => setError(''),
    onSuccess: ({ res, target }, { next }) => onChange(target, res?.skills ?? next),
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  })

  // Reported as an effect, not inline in render: the parent uses it to fence
  // actions (publish) that must not run over an in-flight skill save.
  useEffect(() => {
    onSavePending?.(save.isPending)
  }, [save.isPending, onSavePending])

  // Close only: the popover's FocusScope is still trapping when this runs, so the
  // focus return to the trigger is onCloseAutoFocus's job below.
  const close = useCallback(() => setOpen(false), [])

  const add = (key: string) => {
    close()
    save.mutate({ agent: agentName, next: [...skills, key] })
  }
  const remove = (key: string) =>
    save.mutate({ agent: agentName, next: skills.filter(k => k !== key) })

  // On WebKit a composition-cancel Escape arrives after compositionend with
  // isComposing already false, so the raw flags cannot identify it.
  const imeLatch = useDocumentImeLatch(open)

  // Window capture, because Radix hands the Escape listener from the host dialog
  // to this popover asynchronously: until it does, one Escape closes both and
  // takes the editor's unsaved pane edits with it. Scoped to keys from this popup
  // so other surfaces keep their own dismissal.
  useEffect(() => {
    if (!open) return
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      const t = e.target
      const inPopup = t instanceof Node
        && (dropdownRef.current?.contains(t) || btnRef.current?.contains(t))
      if (!inPopup) return
      if (!imeLatch.claimKey(e)) return
      e.preventDefault()
      close()
    }
    window.addEventListener('keydown', onEsc, { capture: true })
    return () => window.removeEventListener('keydown', onEsc, { capture: true })
  }, [open, close, imeLatch])

  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    hasFilterInput: true,
    filteredCount: filtered.length,
    onEnterSingleMatch: () => add(filtered[0].key),
    closeToTrigger: close,
  })

  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.agentSkillsEditor.skills')}</span>
        <InfoTip text={i18nT('components.agentSkillsEditor.skills_this_agent_template_loads_written_as_skil')} />
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {skills.map(key => {
          const skill = byKey.get(key)
          return (
            <span
              key={key}
              className="group inline-flex items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono bg-accent-subtle border border-accent/30 text-text"
              title={skill?.description || key}
            >
              <Brain className="lucide-inline" />
              {skill?.name || key}
              <button
                className="text-muted hover:text-danger-fg hover:bg-danger rounded-full px-0.5 transition-colors disabled:opacity-40"
                title={i18nT('components.agentSkillsEditor.remove', { name: skill?.name || key })}
                aria-label={i18nT('components.agentSkillsEditor.remove_skill', { name: skill?.name || key })}
                disabled={save.isPending}
                onClick={() => remove(key)}
              >
                <X className="lucide-inline" />
              </button>
            </span>
          )
        })}
        {unmanaged.map(uri => (
          <span
            key={uri}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-muted"
            title={i18nT('components.agentSkillsEditor.edit_agent_config_to_change_mapping', { path: uri })}
          >
            <Lock className="lucide-inline" />
            {uri}
          </span>
        ))}
        {/* `modal`: the host crew editor is a modal dialog, and only a modal popover
            takes over its scroll lock — without that the host cancels wheel events
            over the list. */}
        <Popover open={open} onOpenChange={setOpen} modal>
          <PopoverTrigger asChild>
            <Btn
              ref={btnRef}
              className="flex items-center gap-1 px-2 py-1 text-[12px]"
              disabled={save.isPending || candidates.length === 0}
            >
              <Plus className="lucide-inline" /> {i18nT('components.agentSkillsEditor.add_skill')}
              <span className="text-muted text-[10px]"><ChevronDown className="lucide-inline" /></span>
            </Btn>
          </PopoverTrigger>
          <PopoverContent
            ref={dropdownRef}
            align="start"
            // Needed for the touch focus() below to be more than a no-op.
            tabIndex={-1}
            // Radix gives this surface role="dialog", which needs its own name.
            aria-label={i18nT('components.agentSkillsEditor.available_skills')}
            onKeyDown={onListKeyDown}
            // On touch, keep the on-screen keyboard down but still move focus off
            // the trigger, which hideOthers() has aria-hidden.
            onOpenAutoFocus={e => {
              if (!isTouchDevice()) return
              e.preventDefault()
              dropdownRef.current?.focus()
            }}
            // The focus return, deferred to FocusScope teardown because the trap is
            // still live when `close` runs.
            onCloseAutoFocus={e => {
              e.preventDefault()
              btnRef.current?.focus()
            }}
            // Backstop for an Escape the window handler above declines to claim,
            // so the host dialog never takes the dismissal.
            onEscapeKeyDown={e => {
              if (!imeLatch.claimKey(e)) return
              e.preventDefault()
              close()
            }}
            collisionPadding={8}
            className="w-auto min-w-[280px] max-w-[min(380px,calc(100vw-16px))] max-h-[min(320px,var(--radix-popover-content-available-height))] p-0 flex flex-col overflow-hidden bg-card"
          >
            <div className="p-2 border-b border-border">
              <Input
                ref={inputRef}
                type="text"
                aria-label={i18nT('components.agentSkillsEditor.filter_skills')}
                placeholder={i18nT('components.agentSkillsEditor.type_to_filter')}
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="w-full px-2 py-1 text-[13px]"
              />
            </div>
            <div role="listbox" aria-label={i18nT('components.agentSkillsEditor.available_skills')} className="overflow-y-auto flex-1 min-h-0 p-1">
              {filtered.length === 0 ? (
                <div className="px-2 py-3 text-[12px] text-muted text-center">{i18nT('components.agentSkillsEditor.no_matching_skills')}</div>
              ) : filtered.map(s => (
                // Not `Btn`: its inline-flex base would put the name and
                // description side by side instead of stacked.
                <button
                  key={s.key}
                  role="option"
                  aria-selected={false}
                  tabIndex={-1}
                  className="w-full text-left px-2 py-1.5 rounded-md hover:bg-bg-hover focus-ring transition-colors"
                  onClick={() => add(s.key)}
                >
                  <span className="block text-[13px] font-mono text-text truncate">{s.name}</span>
                  {s.description && (
                    <span className="block text-[11px] text-muted truncate">{s.description}</span>
                  )}
                </button>
              ))}
            </div>
          </PopoverContent>
        </Popover>
      </div>
      {skills.length === 0 && unmanaged.length === 0 && (
        <div className="text-[11px] text-muted mt-1.5">
          {i18nT('components.agentSkillsEditor.no_skills_mapped_this_agent_uses_the_default_beh')}
        </div>
      )}
      {/* No hand-off: the notice sits beside unsaved form input, and the button
          navigates away — which would discard what the user typed. */}
      <ErrorNotice message={error} className="mt-1.5" />
    </div>
  )
}
