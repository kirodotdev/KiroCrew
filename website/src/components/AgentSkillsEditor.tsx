import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Brain, ChevronDown, Lock, Plus, X } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import InfoTip from './InfoTip'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { isTouchDevice } from '../utils/isTouchDevice'

import { i18nT } from '../i18n/t'
import { compareText } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
/** A row from `GET /api/skills` — only the fields this editor needs. */
export interface CatalogSkill {
  key: string
  name: string
  description?: string
  source?: string
  /** Absolute path to the row's SKILL.md. */
  path?: string
}

/**
 * The readable part of a colliding copy's location: the directories ABOVE the skill's own
 * name, last two only.
 *
 * Two copies of one skill differ by where their bundle lives, so those segments are what a
 * person can act on. A digest is unique but says nothing about which copy is which, and a
 * full path is too long for a picker row.
 */
function pathParts(path: string | undefined, name: string): string[] {
  if (!path) return []
  // Backslashes too: a Windows row's path splits on nothing otherwise, so the whole path
  // becomes ONE segment and every colliding row renders the same undistinguishable label.
  const parts = path.split(/[/\\]/).filter(p => p && p !== name && !p.endsWith('.md'))
  // A trailing `skills` is the same for every root, so keeping it inside the window can
  // spend the whole disambiguator on a constant and render twins identically.
  while (parts.length > 1 && parts[parts.length - 1] === 'skills') parts.pop()
  return parts
}

function pathTail(path: string | undefined, name: string): string | null {
  const parts = pathParts(path, name)
  return parts.length ? parts.slice(-2).join('/') : null
}

/**
 * Labels for rows that share a name, WIDENED until they actually differ.
 *
 * A fixed two-segment window renders two roots identically whenever they diverge only
 * ABOVE it, which defeats the disambiguator on exactly the installs it exists for. So the
 * window grows until every label in the colliding group is distinct. A group the path cannot
 * separate at all gets NO label: the only remaining spelling is the qualified key, and a
 * 32-hex digest is not something a user can act on, so an omission is more honest.
 */
function disambiguators(rows: { key: string; name: string; path?: string }[]): Map<string, string> {
  const byName = new Map<string, typeof rows>()
  for (const r of rows) {
    const group = byName.get(r.name)
    if (group) group.push(r)
    else byName.set(r.name, [r])
  }
  const out = new Map<string, string>()
  for (const group of byName.values()) {
    if (group.length < 2) continue
    const parts = group.map(r => pathParts(r.path, r.name))
    const widest = Math.max(0, ...parts.map(p => p.length))
    let labels: string[] = []
    for (let n = 2; n <= Math.max(2, widest); n++) {
      labels = parts.map(p => p.slice(-n).join('/'))
      if (new Set(labels).size === group.length) break
    }
    if (new Set(labels).size !== group.length) continue
    group.forEach((r, i) => {
      if (labels[i]) out.set(r.key, labels[i])
    })
  }
  return out
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
// From its code point, not a literal: the i18n gate reads a bare string here as user copy.
const ELLIPSIS = String.fromCharCode(0x2026)

const CHIP_WHERE_MAX = 28

function middleElide(text: string, max: number): string {
  if (text.length <= max) return text
  // Twins share a long PREFIX, so end-truncation hides the one part that tells them apart.
  const tailLen = Math.ceil((max - 1) / 2)
  return text.slice(0, max - 1 - tailLen) + ELLIPSIS + text.slice(text.length - tailLen)
}

export default function AgentSkillsEditor({ agentName, skills, unmanaged = [], onChange, beforeSave, pendingChain, onSavePending }: Props) {
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const btnRef = useRef<HTMLButtonElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const {
    data: catalog = [],
    isSuccess: catalogLoaded,
    isError: catalogFailed,
  } = useQuery<CatalogSkill[]>({
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

  // How many catalog rows share each display name. Only a name carried by more than
  // one row needs its qualifier shown, so an ordinary skill stays a plain label.
  // Counted over PACKAGE rows only: the qualifier exists for colliding bundles, so a
  // user's own copy sharing a crew skill's name is not an ambiguity and needs no tail.
  const nameCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const s of catalog) {
      if (s.source !== 'package') continue
      m.set(s.name, (m.get(s.name) ?? 0) + 1)
    }
    return m
  }, [catalog])

  // Derived over the WHOLE catalog, not per row, because widening a colliding window is a
  // property of the group -- and computed once so the chip and the picker row agree.
  const tailByKey = useMemo(
    () => disambiguators(catalog.filter(s => s.source === 'package')),
    [catalog]
  )

  // A tail is widened only until it differs from its twin, so eliding the middle can
  // collapse the two back to one string and defeat the disambiguator on the case it is for.
  // This is the SINGLE source both the chip and the picker read for an ambiguous qualifier,
  // so a twin can never be handed a qualifier that some other code path derived differently.
  const whereByKey = useMemo(() => {
    const tailsByName = new Map<string, string[]>()
    const pkg = catalog.filter(s => s.source === 'package')
    for (const s of pkg) {
      const tail = tailByKey.get(s.key)
      if (tail) tailsByName.set(s.name, [...(tailsByName.get(s.name) ?? []), tail])
    }
    const out = new Map<string, string>()
    for (const s of pkg) {
      const tail = tailByKey.get(s.key)
      if (!tail) {
        // The path could not separate this group, so there is no widened tail. Both copies
        // fall back to the SAME shared location -- an honest shared location, never a made-up
        // distinction. Omit it entirely when even that is empty.
        const shared = pathTail(s.path, s.name)
        if (shared) out.set(s.key, shared)
        continue
      }
      const elided = middleElide(tail, CHIP_WHERE_MAX)
      const group = tailsByName.get(s.name) ?? []
      const collapsed = group.filter(t => middleElide(t, CHIP_WHERE_MAX) === elided).length > 1
      out.set(s.key, collapsed ? tail : elided)
    }
    return out
  }, [catalog, tailByKey])

  // Empty until the catalog loads: with no rows every key looks unresolved, so a count
  // taken before then would report the whole mapping as missing.
  //
  // A `package/` key is EXEMPT: `GET /api/skills` sources package rows from the
  // capability manager, whose `list_skills()` is timeout-bounded and degrades to an
  // EMPTY package set with a normal 200 on timeout (no completeness signal reaches the
  // client). Marking a package mapping dead on that partial response is a false positive,
  // and the count line below then instructs the user to remove a live mapping. The other
  // sources (kirocrew, kiro-workspace) do not silently drop to empty, so a NON-package key
  // absent from a loaded catalog is a genuine dead mapping and is still flagged.
  const isPackageKey = (k: string) => k.startsWith('package/')
  const unresolvedKeys = useMemo(
    () => (catalogLoaded ? skills.filter(k => !isPackageKey(k) && !byKey.get(k)) : []),
    [catalogLoaded, skills, byKey]
  )
  const unresolvedCount = unresolvedKeys.length

  // Candidates = catalog minus what's already mapped, name-sorted for a stable
  // list regardless of the catalog's source-grouped order.
  //
  // Empty while the catalog is in an ERROR state: a failed background refetch keeps the
  // last-successful `data` cached, so without this the picker would stay enabled and offer
  // stale options while the notice claims the catalog could not load. Suppressing them here
  // also disables Add (length 0) and makes the listbox render the failure text.
  const candidates = useMemo(
    () => catalogFailed
      ? []
      : catalog.filter(s => !skills.includes(s.key)).sort((a, b) => compareText(a.name, b.name)),
    [catalog, skills, catalogFailed],
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

  // Both copies of a collision carry the same name, so a bare one names neither: the
  // picker row's disambiguator is rendered so the user can tell which copy they picked.
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
      {/* Hand-off decided OFF: this notice sits beside unsaved form input in the same pane,
          and the button navigates away, which would discard what the user typed. */}
      <ErrorNotice
        askAgent={false}
        testId="agent-skills-catalog-error"
        message={
          catalogFailed
            ? i18nT('components.agentSkillsEditor.could_not_load_the_skill_catalog')
            : ''
        }
      />
      <div className="flex flex-wrap items-center gap-1.5">
        {skills.map(key => {
          const skill = byKey.get(key)
          const label = skill?.name || key
          // No catalog row means the mapped copy is not installed NOW, and the ordinary
          // style made that dead mapping look healthy. Gated on the query having SUCCEEDED:
          // an empty catalog while loading or after a failure is not evidence of absence.
          // A `package/` key is exempt (see unresolvedKeys): the capability-manager source
          // degrades to an empty set with a 200 on timeout, so an absent package row is not
          // reliable evidence the copy is gone.
          const unresolved = catalogLoaded && !skill && !isPackageKey(key)
          const unresolvedNote = i18nT('components.agentSkillsEditor.mapping_unresolved')
          const ambiguous = skill
            ? skill.source === 'package' && (nameCounts.get(skill.name) ?? 0) > 1
            : false
          // The user picked by PATH, so the chip says the same thing the picker row did.
          // No digest fallback: it is unique but cannot be correlated back to that choice,
          // so "shared-skill deadbeef" names nothing the user could act on.
          // An AMBIGUOUS chip reads its qualifier EXCLUSIVELY from whereByKey, the single
          // collision-safe source (it keeps the full tail when eliding would collapse twins,
          // and carries the honest shared location when the path cannot separate them at all),
          // so two twins can never be handed qualifiers derived by differing code paths.
          const disambiguator = skill
            ? (ambiguous
                ? (whereByKey.get(skill.key) ?? null)
                : (tailByKey.get(skill.key) ?? pathTail(skill.path, skill.name)))
            : null
          return (
            <span
              key={key}
              className={`group inline-flex min-w-0 max-w-full items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono ${
                unresolved
                  ? 'bg-warn-subtle border border-warn text-warn-fg'
                  : 'bg-accent-subtle border border-accent/30 text-text'
              }`}
              title={
                unresolved
                  ? `${unresolvedNote}\n${key}`
                  : skill?.description
                    ? `${skill.description}\n${key}`
                    : `${label}\n${key}`
              }
              // A screen reader would otherwise spell the whole key, so the name carries
              // the readable label plus the short disambiguator the chip already shows.
              aria-label={
                [label, disambiguator, unresolved ? unresolvedNote : skill?.description]
                  .filter(Boolean)
                  .join(' ') || label
              }
            >
              {unresolved ? (
                <AlertTriangle className="lucide-inline text-warn-fg" />
              ) : (
                <Brain className="lucide-inline" />
              )}
              {/* The warn state is otherwise colour plus an icon carrying no text, and
                  ARIA cannot name a role-less span, so the note ships as real text. */}
              {unresolved && <span className="sr-only">{unresolvedNote}</span>}
              <span className="min-w-0 truncate">{label}</span>
              {ambiguous && disambiguator && (
                <span
                  // Same weight as the picker row's line: with two otherwise-identical chips
                  // this is the ONLY text saying which copy is bound.
                  className="inline-block max-w-full align-bottom break-all text-text text-[11px]"
                  title={skill?.path || i18nT('components.agentSkillsEditor.copy_identifier_hint')}
                >
                  {i18nT('components.agentSkillsEditor.copy_identifier_label', {
                    where: disambiguator,
                  })}
                </span>
              )}
              <button
                className="shrink-0 text-muted hover:text-danger-fg hover:bg-danger rounded-full px-0.5 transition-colors disabled:opacity-40"
                title={i18nT('components.agentSkillsEditor.remove', { name: label })}
                aria-label={i18nT('components.agentSkillsEditor.remove_skill', { name: label })}
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
                // No error string here: the catalog-load failure is already surfaced by the
                // ErrorNotice above (errors-use-error-notice), so the popover only ever shows
                // the neutral empty state and never becomes a second error box.
                <div className="px-2 py-3 text-[12px] text-muted text-center">{
                  i18nT('components.agentSkillsEditor.no_matching_skills')
                }</div>
              ) : filtered.map(s => {
                // A repeated PACKAGE name makes two rows visual twins, so the
                // disambiguator is rendered INSIDE the button, where it is announced.
                const twin = s.source === 'package' && (nameCounts.get(s.name) ?? 0) > 1
                // No raw-key fallback: the chip refuses one too, because a 32-hex digest
                // is not something a user can act on. Better no line than an opaque one.
                // Read the SAME collision-safe map the chip uses (whereByKey keeps the full
                // tail when elision would collapse twins), so two colliding rows never render
                // an identical qualifier; omit the line when the map has no entry.
                const tail = twin ? (whereByKey.get(s.key) ?? null) : null
                return (
                  // Not `Btn`: its inline-flex base would put the name and
                  // description side by side instead of stacked.
                  <button
                    key={s.key}
                    role="option"
                    aria-selected={false}
                    tabIndex={-1}
                    title={s.path ? `${s.path}\n${s.key}` : s.key}
                    className="w-full text-left px-2 py-1.5 rounded-md hover:bg-bg-hover focus-ring transition-colors"
                    onClick={() => add(s.key)}
                  >
                    <span className="block text-[13px] font-mono text-text truncate">{s.name}</span>
                    {s.description && (
                      <span className="block text-[11px] text-muted truncate">{s.description}</span>
                    )}
                    {/* Twins share name AND description, so this line is the ONLY thing that
                        tells them apart -- it must not be the faintest text on the row. */}
                    {tail && (
                      <span className="block text-[11px] font-mono text-text truncate">
                        {i18nT('components.agentSkillsEditor.copy_identifier_label', {
                          where: tail,
                        })}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          </PopoverContent>
        </Popover>
      </div>
      {skills.length === 0 && unmanaged.length === 0 && (
        <div className="text-[11px] text-muted mt-1.5">
          {i18nT('components.agentSkillsEditor.no_skills_mapped_this_agent_uses_the_default_beh')}
        </div>
      )}
      {/* Visible, not only a `title` and an aria-label: a sighted keyboard or touch user
          otherwise sees a yellow chip and a triangle and is told nothing. */}
      {unresolvedCount > 0 && (
        <div className="text-[11px] text-warn-fg mt-1.5">
          {i18nT('components.agentSkillsEditor.mapping_unresolved_count', {
            count: unresolvedCount,
          })}
        </div>
      )}
      {/* No hand-off: the notice sits beside unsaved form input, and the button
          navigates away — which would discard what the user typed. */}
      <ErrorNotice message={error} className="mt-1.5" />
    </div>
  )
}
