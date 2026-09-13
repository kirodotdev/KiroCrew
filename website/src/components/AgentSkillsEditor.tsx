import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { AlertTriangle, Brain, ChevronDown, FileCog, Plus, X } from 'lucide-react'
import { api } from '../api/client'
import { useConfirm } from './ConfirmDialog'
import { Btn, Input } from './ui'
import InfoTip from './InfoTip'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'

import { i18nT } from '../i18n/t'
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
 * `package/<digest>:<rel>` — the qualifier is what makes one of several colliding copies
 * addressable. It is read here to recover the READABLE half of a mapped key: a key whose
 * copy is no longer enumerated has no catalog row, so the raw key would render as a bare
 * 32-hex digest.
 *
 * No bundle NAME is rendered beside it, deliberately. `GET /api/skills` re-keys a colliding
 * package row onto its qualified spelling, so such a copy DOES reach the picker under its own
 * key -- but the qualifier is a digest, not a name, and the readable disambiguator is the
 * package field the row already carries, which is also what an origin span would have shown.
 */
const QUALIFIED_PACKAGE_KEY = /^package\/([0-9a-f]{8,}):(.+)$/

function splitQualifiedKey(key: string): { digest: string; rel: string } | null {
  const m = QUALIFIED_PACKAGE_KEY.exec(key)
  return m ? { digest: m[1], rel: m[2] } : null
}

/**
 * The save mutation's variables, declared ONCE.
 *
 * Annotating only one handler's parameter narrows react-query's inferred `TVariables` to
 * that shape, which then contradicts the others -- the compile error this alias prevents.
 */
interface SaveVars {
  agent: string
  /**
   * The managed keys to write. ABSENT on a removal, which states no managed set at all: the
   * spec the backend re-reads under lock stays authoritative for every mapping this write
   * does not name, so a concurrent session's mapping cannot be overwritten away.
   */
  next?: string[]
  /** Readable label of the pick, so a refusal can name WHICH one failed. */
  attempted?: string
  /** The key this write added, or undefined for a removal. A refusal naming a DIFFERENT
   * key means a mapped chip is blocking the write, which changes the advice. */
  attemptedKey?: string
  /** URIs this write asks to delete. Naming is the ONLY way to remove one: a write that
   * omits a URI leaves it alone, so stale client state cannot destroy a co-owner's. */
  removeUnmanaged?: string[]
}

/**
 * The readable part of a colliding copy's location: the directories ABOVE the skill's own
 * name, last two only.
 *
 * Two copies of one skill differ by where their bundle lives, so those segments are what a
 * person can act on. A digest is unique but says nothing about which copy is which, and a
 * full path is too long for a picker row.
 */
/**
 * The refusal the backend reported, read from its STRUCTURED body.
 *
 * `friendlyErrText` collapses the payload to its `error` string before it reaches a
 * mutation handler, so the `skills` array never survives into the message -- which is why
 * this reads `ApiError.body`, kept for exactly this, and matches on the machine-readable
 * `code` rather than the prose. The refusal is whole-PATCH, so the offender is not
 * necessarily the key this call added, and a removal adds none at all.
 */
function unknownSkillsFrom(e: unknown): { unlisted: boolean; refused: string | null } {
  const body = typeof (e as { body?: unknown })?.body === 'string'
    ? (e as { body: string }).body
    : ''
  if (!body.trim().startsWith('{')) return { unlisted: false, refused: null }
  try {
    const parsed = JSON.parse(body) as { code?: unknown; skills?: unknown }
    if (parsed.code === 'skills_unknown') {
      const list = Array.isArray(parsed.skills) ? parsed.skills : []
      const first = list.find((s): s is string => typeof s === 'string' && s.length > 0)
      return { unlisted: true, refused: first ?? null }
    }
  } catch { /* a body that is not JSON is not this refusal */ }
  return { unlisted: false, refused: null }
}

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
   *
   * `unmanaged` carries the URIs the write PRESERVED. A removal the backend could not
   * honour comes back here, so the caller re-renders it rather than reporting a bare success.
   */
  onChange: (agentName: string, skills: string[], unmanaged?: string[]) => void
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
 */
// From its code point, not a literal: the i18n gate reads a bare string here as user copy.
const ELLIPSIS = String.fromCharCode(0x2026)

function middleElide(text: string, max: number): string {
  if (text.length <= max) return text
  // Twins share a long PREFIX, so end-truncation hides the one part that tells them apart.
  const tailLen = Math.ceil((max - 1) / 2)
  return text.slice(0, max - 1 - tailLen) + ELLIPSIS + text.slice(text.length - tailLen)
}

export default function AgentSkillsEditor({ agentName, skills, unmanaged = [], onChange, beforeSave, pendingChain, onSavePending }: Props) {
  const { confirm, confirmDialog } = useConfirm()
  const [error, setError] = useState('')
  // The catalog is cached, so a refusal can arrive while the stale copy still lists the key.
  const [refusedKey, setRefusedKey] = useState<string | null>(null)
  // A blocked add is the user's pick, and the notice names only the blocker: without
  // holding the pick, clearing the blocker leaves them to find it in the picker again.
  const btnRef = useRef<HTMLButtonElement>(null)
  const queryClient = useQueryClient()

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

  // Empty until the catalog loads: with no rows every key looks unresolved, so a count
  // taken before then would report the whole mapping as missing.
  const unresolvedKeys = useMemo(
    () => (catalogLoaded ? skills.filter(k => !byKey.get(k)) : []),
    [catalogLoaded, skills, byKey]
  )
  const unresolvedCount = unresolvedKeys.length

  // Marking every mapped chip would point the refusal's "remove the skill marked with a
  // warning" at healthy skills, which the picker cannot put back.

  // Candidates = catalog minus what's already mapped, name-sorted for a stable
  // list regardless of the catalog's source-grouped order.
  const candidates = useMemo(
    () => catalog.filter(s => !skills.includes(s.key)).sort((a, b) => a.name.localeCompare(b.name)),
    [catalog, skills],
  )

  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered } =
    useFilteredDropdown(candidates)

  const save = useMutation({
    // The agent name travels WITH the request so the response can be matched to
    // the agent it was issued for, not to whatever is selected when it lands.
    // `beforeSave` may redirect the write to a just-forked private copy; the
    // resolved target is what onChange reports, so the caller tracks the copy.
    mutationFn: async ({ agent, next, removeUnmanaged }: SaveVars) => {
      // Chained onto the caller's shared instant-save chain when one is
      // provided: an action that snapshots the file (publish) can then drain
      // ONE promise and know every queued edit — model pick or skill toggle —
      // has landed first.
      const run = (pendingChain?.current ?? Promise.resolve())
        .catch(() => undefined)
        .then(async () => {
          const target = beforeSave ? await beforeSave() : agent
          const res = await api.agentPatch(target, {
            // Omitted entirely for a removal: resubmitting this client's managed keys would
            // overwrite whatever a concurrent session mapped since they were read.
            ...(next !== undefined ? { skills: next } : {}),
            ...(removeUnmanaged?.length ? { removed_unmanaged_skills: removeUnmanaged } : {}),
            // What this client SAW as unmanaged. Never a keep-list: it only lets the write
            // tell a mid-flight reclassification from a mapping that was managed all along.
            ...(unmanaged.length ? { unmanaged_skills: unmanaged } : {}),
          })
          return { res: res as { skills?: string[]; unmanaged_skills?: string[] }, target }
        })
      if (pendingChain) pendingChain.current = run
      return run
    },
    onMutate: () => {
      setError('')
      setRefusedKey(null)
    },
    onSuccess: (
      { res, target }: { res: { skills?: string[]; unmanaged_skills?: string[] }; target: string },
      { next, removeUnmanaged }: SaveVars
    ) => {
      // The chip that opened the confirm unmounts with this save, so the dialog's own restore
      // target is a detached node by now and focusing it is a no-op that lands on <body>.
      if (removeUnmanaged?.length) btnRef.current?.focus()
      // Two-arg when there is nothing unmanaged to report: the third argument is optional
      // and callers assert the arity, so passing an explicit undefined breaks them.
      // A removal states no managed set, so this client's own is what still holds when the
      // response does not carry one. Never undefined: callers assert the arity.
      const applied = res?.skills ?? next ?? skills
      if (res?.unmanaged_skills === undefined) onChange(target, applied)
      else onChange(target, applied, res.unmanaged_skills)
    },
    onError: (e: unknown, vars: SaveVars) => {
      const { unlisted, refused } = unknownSkillsFrom(e)
      setRefusedKey(refused)
      const offender = refused
        ? // Still the key the BACKEND refused, but labelled the way the picker labels it:
          // both copies of a collision share a name, so a bare one names neither.
          (candidates.some(c => c.key === refused) ? pickLabel(refused) : null) ||
          catalog.find(c => c.key === refused)?.name ||
          splitQualifiedKey(refused)?.rel ||
          refused
        : // The backend named no key, so the pick just made is the best name available --
          // and for an add it is also the one a re-pick would fix.
          vars.attempted
      // Re-picking only helps when the refused key IS the one just chosen; otherwise a
      // mapped chip blocks every write and removing it is the only move that can succeed.
      const blocker = refused && refused !== vars.attemptedKey
      const named = offender ?? ''
      setError(
        unlisted
          ? // No name means no sentence can name one: every named string starts with the
            // label, so interpolating '' renders a dangling colon on either branch.
            !named
            ? i18nT('components.agentSkillsEditor.key_changed_remove_blocked_generic')
            : blocker || vars.attemptedKey === undefined
                ? i18nT('components.agentSkillsEditor.key_changed_remove_blocked', {
                    name: named,
                  })
                : i18nT('components.agentSkillsEditor.key_changed_repick', { name: named })
          : e instanceof Error
            ? e.message
            : String(e)
      )
      // The catalog in hand is the stale one that minted the refused key, and it is
      // cached, so without this a retry re-sends exactly the key that was just rejected.
      if (unlisted) {
        void queryClient.invalidateQueries({ queryKey: ['skills-catalog'] })
        // The agent detail too: the whole PATCH is refused, so `skills` still holds EVERY
        // stale key and each removal names the other, with no exit but a page reload.
        // Prefix-keyed, as the two sibling call sites are: the detail is registered as
        // ['agentDetail', <template>], and a fork may have retargeted the write.
        void queryClient.invalidateQueries({ queryKey: ['agentDetail'] })
      }
    },
  })

  // Reported as an effect, not inline in render: the parent uses it to fence
  // actions (publish) that must not run over an in-flight skill save.
  useEffect(() => {
    onSavePending?.(save.isPending)
  }, [save.isPending, onSavePending])

  // Both copies of a collision carry the same name, so a bare one names neither: the
  // notice would read "remove shared-skill first; shared-skill will then be added".
  const pickLabel = (key: string): string => {
    const row = candidates.find(c => c.key === key)
    if (!row) return key
    const twin = row.source === 'package' && (nameCounts.get(row.name) ?? 0) > 1
    const tail = twin ? (tailByKey.get(row.key) ?? pathTail(row.path, row.name)) : null
    return tail ? `${row.name} (${tail})` : row.name
  }
  const add = (key: string) => {
    setOpen(false)
    save.mutate({
      agent: agentName,
      next: [...skills, key],
      attempted: pickLabel(key),
      attemptedKey: key,
    })
  }
  const remove = (key: string) => {
    // An unqualified `package/<rel>` key is unique only while its bundle is the sole vendor,
    // so one held across the refusal's catalog invalidation can bind another root's copy.
    save.mutate({ agent: agentName, next: skills.filter(k => k !== key) })
  }

  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    hasFilterInput: true,
    filteredCount: filtered.length,
    onEnterSingleMatch: () => add(filtered[0].key),
    closeToTrigger: () => { setOpen(false); btnRef.current?.focus() },
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
          const qualified = splitQualifiedKey(key)
          // An unresolved key has no catalog row to name it, so the raw key would render
          // as a 32-hex digest; its rel half is the readable part.
          const label = skill?.name || qualified?.rel || key
          // No catalog row means the mapped copy is not installed NOW, and the ordinary
          // style made that dead mapping look healthy. Gated on the query having SUCCEEDED:
          // an empty catalog while loading or after a failure is not evidence of absence.
          const unresolved = (catalogLoaded && !skill) || key === refusedKey
          const unresolvedNote = i18nT('components.agentSkillsEditor.mapping_unresolved')
          const ambiguous = skill
            ? skill.source === 'package' && (nameCounts.get(skill.name) ?? 0) > 1
            : Boolean(qualified)
          // Removing THIS chip also applies the held pick, so the control has to say so:
          // a screen reader hears only the label, never the notice that announced it.
          // The user picked by PATH, so the chip says the same thing the picker row did.
          // No digest fallback: it is unique but cannot be correlated back to that choice,
          // so "shared-skill deadbeef" names nothing the user could act on.
          // The label says "Located in", so it must carry a location: the `package` field
          // names the bundle, which reads like one but is not.
          const disambiguator = skill
            ? (tailByKey.get(skill.key) ?? pathTail(skill.path, skill.name))
            : null
          return (
            <span
              key={key}
              className={`group inline-flex items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono ${
                unresolved
                  ? 'bg-warn-subtle border border-warn text-warn-fg'
                  : 'bg-accent-subtle border border-accent/30 text-text'
              }`}
              // A qualified key leads with a 32-hex digest, so the readable label comes
              // first on every chip and the key follows it as the precise form.
              title={
                unresolved
                  ? `${unresolvedNote}\n${key}`
                  : skill?.description
                    ? `${skill.description}\n${key}`
                    : `${label}\n${key}`
              }
              // A screen reader would otherwise spell the whole 32-hex qualifier, so the
              // name carries the readable label plus the short id the chip already shows.
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
              {label}
              {ambiguous && disambiguator && (
                <span
                  // Same weight as the picker row's line: with two otherwise-identical chips
                  // this is the ONLY text saying which copy is bound.
                  className="inline-block align-bottom text-text text-[11px]"
                  title={skill?.path || i18nT('components.agentSkillsEditor.copy_identifier_hint')}
                >
                  {i18nT('components.agentSkillsEditor.copy_identifier_label', {
                    where: middleElide(disambiguator, 28),
                  })}
                </span>
              )}
              <button
                className="text-muted hover:text-danger-fg hover:bg-danger rounded-full px-0.5 transition-colors disabled:opacity-40"
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
        <div className="relative">
          <Btn
            ref={btnRef}
            className="flex items-center gap-1 px-2 py-1 text-[12px]"
            disabled={save.isPending || candidates.length === 0}
            onClick={() => setOpen(!open)}
          >
            <Plus className="lucide-inline" /> {i18nT('components.agentSkillsEditor.add_skill')}
            <span className="text-muted text-[10px]"><ChevronDown className="lucide-inline" /></span>
          </Btn>
          {open && btnRef.current && createPortal(
            // Presentational positioning wrapper: interactive semantics live on
            // the inner role="listbox" and its option buttons, so this element
            // only hosts the roving-focus keydown handler (mirrors the model
            // dropdown on this page).
            // eslint-disable-next-line jsx-a11y/no-static-element-interactions
            <div
              ref={dropdownRef}
              tabIndex={-1}
              onKeyDown={onListKeyDown}
              className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[280px] max-w-[380px] max-h-[320px] flex flex-col overflow-hidden animate-slide-up"
              style={(() => {
                const r = btnRef.current!.getBoundingClientRect()
                const dropH = 320
                const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4
                const left = Math.max(8, Math.min(r.left, window.innerWidth - 388))
                return { top, left }
              })()}
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
                ) : filtered.map(s => {
                  // A repeated PACKAGE name makes two rows visual twins, so the
                  // disambiguator is rendered INSIDE the button, where it is announced.
                  const twin = s.source === 'package' && (nameCounts.get(s.name) ?? 0) > 1
                  // No raw-key fallback: the chip refuses one too, because a 32-hex digest
                  // is not something a user can act on. Better no line than an opaque one.
                  const tail = twin ? (tailByKey.get(s.key) ?? pathTail(s.path, s.name)) : null
                  return (
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
                            where: middleElide(tail, 44),
                          })}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </div>,
            document.body
          )}
        </div>
      </div>
      {confirmDialog}
      {unmanaged.length > 0 && (
        <div
          className="flex flex-wrap items-center gap-1.5 mt-1.5"
          data-testid="agent-skills-unmanaged-region"
        >
          {unmanaged.map(uri => (
            <span
              key={uri}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-muted"
              title={i18nT('components.agentSkillsEditor.edit_agent_config_to_change_mapping', { path: uri })}
            >
              <FileCog className="lucide-inline shrink-0" aria-hidden="true" />
              {uri}
              <button
                className="text-muted hover:text-danger-fg hover:bg-danger rounded-full px-0.5 transition-colors disabled:opacity-40"
                aria-label={i18nT('components.agentSkillsEditor.remove_skill', { name: uri })}
                disabled={save.isPending}
                onClick={async () => {
                  // Hand-authored URIs are not catalog rows, so the picker cannot put one
                  // back: unasked, a mis-click is recoverable only by editing config.
                  // The URI's TAIL, not the whole thing: a hand-authored path renders an
                  // unreadable title and an unreadable button when interpolated whole.
                  const tail = uri.split('/').filter(Boolean).slice(-2).join('/') || uri
                  const ok = await confirm({
                    title: i18nT('components.agentSkillsEditor.remove_skill', { name: tail }),
                    // The consequence, which is the whole reason to ask: restoring one is a
                    // config-file edit, since a hand-authored URI is not a catalog row.
                    // The URI alone: the title names the skill and the button names the
                    // action, so a sentence here can only argue with them.
                    body: i18nT(
                      'components.agentSkillsEditor.edit_agent_config_to_change_mapping',
                      { path: uri },
                    ),
                    confirmLabel: i18nT('components.agentSkillsEditor.remove', { name: tail }),
                  })
                  if (ok) {
                    save.mutate({ agent: agentName, removeUnmanaged: [uri] })
                  }
                }}
              >
                <X className="lucide-inline" />
              </button>
            </span>
          ))}
        </div>
      )}
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
