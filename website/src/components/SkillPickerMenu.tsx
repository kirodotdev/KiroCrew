import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Lock, Sparkles } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'
import { skillsCacheStaleTime } from '../lib/skillsCache'
import type { SendMode } from '../pages/chat/ChatSettings'

import { i18nT } from '../i18n/t'
// — $skill inline trigger autocomplete.
// Mirrors FilePickerMenu but lists skills (from /api/skills, all sources:
// kirocrew + workspace + AIM). Selecting one inserts a `$leaf` token; the
// backend SkillsLoader.resolve_dollar_skills then expands it (allowlist match
// on the leaf segment — no path is constructed from user input, per input-validation guidance).

interface SkillItem {
  key: string          // full key, e.g. "WorkforceEmploymentKnowledgeBase/oncall-handover"
  name: string
  description: string
  source?: string      // kirocrew | aim | kiro-user | kiro-workspace
  // Only present on `kiro-workspace` rows: whether the operator has granted
  // this project directory trust. An untrusted row is LISTED but not usable —
  // its `$token` would expand to nothing, because SkillsLoader gates the
  // project root on the grant.
  trusted?: boolean
}

interface Props {
  query: string
  anchorRef: React.RefObject<HTMLElement | null>
  open: boolean
  // Real chat-slot key (e.g. "dashboard:chat-7"). Sent as X-Session-Key so the
  // server resolves THIS chat's project rather than the shared placeholder.
  slotKey?: string
  // The slot's current project is cache identity: one slot can switch projects
  // while its session key stays constant.
  project?: string
  // Receives the leaf token to insert (e.g. "oncall-handover") plus the full key.
  onSelect: (info: { leaf: string; key: string }) => void
  // An untrusted project skill was chosen: ask for consent rather than
  // inserting a token that cannot resolve.
  onTrustRequest?: (info: { leaf: string; key: string }) => void
  onClose: () => void
  /**
   * The composer's effective send binding (see ChatInput's SendMode). Only
   * read by the settled-empty copy: in 'ctrl-enter' mode a released bare
   * Enter is a newline, so the announcement must name Ctrl+Enter instead.
   */
  sendOnEnter?: SendMode
}

// Last path segment of a skill key — this is what `$token` matches against.
function leafOf(key: string): string {
  const i = key.lastIndexOf('/')
  return i === -1 ? key : key.slice(i + 1)
}

// A workspace skill the operator has not consented to yet.
function needsTrust(s: SkillItem): boolean {
  return s.source === 'kiro-workspace' && s.trusted === false
}

export default function SkillPickerMenu({
  query, anchorRef, open, slotKey, project, onSelect, onTrustRequest, onClose,
  sendOnEnter = 'enter',
}: Props) {
  const [results, setResults] = useState<SkillItem[]>([])
  const resultsRef = useRef<SkillItem[]>([])

  // Shared skills cache, keyed per slot and project: a slot can switch projects
  // without changing its session key. Prefix-matching keeps every
  // existing invalidateQueries({queryKey:['skills']}) call working.
  // staleTime is long because skills change rarely. `enabled: open` keeps the
  // menu lazy — the focus-prefetch warms the same key separately.
  const { data, isLoading, isFetching, isError } = useQuery<SkillItem[]>({
    queryKey: ['skills', slotKey ?? null, project ?? null],
    queryFn: () => api.skills(slotKey),
    enabled: open,
    staleTime: skillsCacheStaleTime(project),
  })
  const loading = isLoading && open

  // Choose handler reads from resultsRef (current at keypress time).
  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const s = r[idx >= r.length ? 0 : idx]
    if (!s) return
    const info = { leaf: leafOf(s.key || s.name), key: s.key || s.name }
    // Route an unconsented project skill to the consent step. Inserting its
    // token instead would look like success and then do nothing at all.
    if (needsTrust(s) && onTrustRequest) onTrustRequest(info)
    else onSelect(info)
  }, [onSelect, onTrustRequest])

  // Filter by leaf-name substring (case-insensitive). Empty query lists all,
  // capped for menu height. Dedupe by leaf so the same $token isn't ambiguous
  // (mirrors the backend's leaf-addressed resolution). Memoized (not computed
  // in the ordering effect below) so the keyboard-release gate reads the SAME
  // render's match set — a gate derived from the `results` state would lag one
  // effect flush behind and could release Enter while matches exist.
  const matched = useMemo(() => {
    const list = Array.isArray(data) ? data : []
    const q = query.toLowerCase()
    const seen = new Set<string>()
    const out: SkillItem[] = []
    for (const s of list) {
      const leaf = leafOf(s.key || s.name).toLowerCase()
      if (q && !leaf.includes(q)) continue
      if (seen.has(leaf)) continue
      seen.add(leaf)
      out.push(s)
    }
    return out.slice(0, 50)
  }, [data, query])

  // "Settled and genuinely empty": only then does the menu have no claim on
  // Enter/Tab — release them so the composer's own Enter action still works
  // (the #5029 trap, deferred to the sibling pickers by #5041). There is no
  // debounce here (the list filters an already-loaded in-memory array), but
  // settled-ness still matters: on the render where `enabled: open` flips, or
  // during a background refetch over a cached empty list, matches are
  // transiently unknowable and releasing Enter would irreversibly send a
  // draft whose $token the user was still completing. A settled ERROR counts
  // as settled-empty — the menu shows the same empty state and has nothing to
  // offer, so keeping the swallow there would recreate the trap on a failed
  // fetch. Same gate shape as FilePickerMenu's.
  const releaseKeysWhenEmpty = !isFetching && (data !== undefined || isError) && matched.length === 0

  // Shared Arrow/Enter/Tab/Escape + scroll-into-view (see useListKeyboardNav).
  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    onChoose: choose,
    onClose,
    releaseKeysWhenEmpty,
  })

  // Populate bottom-up when the menu opens above the input (shared helper —
  // same geometry the render uses for positioning, and the same reversal the
  // @file and /command pickers use): the first row sits at the bottom nearest
  // the cursor with the initial selection on it; when it flips below, keep it
  // at the top.
  useEffect(() => {
    if (!open) return
    const above = anchorRef.current ? menuGeometry(anchorRef.current, matched.length, 48).above : false
    const { ordered, initialIndex } = bottomUpOrder(matched, above)
    setResults(ordered); resultsRef.current = ordered
    // setSelected is the hook's synced setter (keeps selectedRef in lockstep),
    // so Enter-before-arrow dispatches on this row.
    setSelected(initialIndex)
  }, [matched, open, anchorRef, setSelected])

  // Scroll the selected row into view once results actually render. The filter
  // effect sets the selection (often the bottom row when the menu opens above)
  // BEFORE those rows exist in the DOM, so the hook's own scrollIntoView (fired
  // from setSelected) no-ops on a not-yet-mounted ref. This runs after results
  // commit — refs are populated — so the selected row is visible on open and on
  // query change. Keyed on [results]: arrow-key nav changes `selected` but not
  // `results`, and the hook already scrolls on move, so this never fights
  // per-keystroke navigation.
  useEffect(() => {
    if (!open) return
    itemRefs.current[selectedRef.current]?.scrollIntoView({ block: 'nearest' })
  }, [results, open, selectedRef, itemRefs])

  if (!open || !anchorRef.current) return null

  const { top, left, width, maxHeight } = menuGeometry(anchorRef.current, results.length, 48)

  const empty = loading
    ? <div className="px-3 py-3 text-[12px] text-muted">{i18nT('components.skillPickerMenu.loading_skills')}</div>
    // Enter's meaning flips with the release gate (pick → send), so the copy
    // must announce it at the point of action rather than silently sending.
    // Named per the composer's send binding: in 'ctrl-enter' mode a bare
    // Enter is a newline, so promising "Enter sends" there would be false.
    // role="status": the flip is otherwise announced only visually, and a
    // screen-reader user would hit the very silent-send surprise this copy
    // exists to prevent.
    : <div role="status" className="px-3 py-3 text-[12px] text-muted">{i18nT(!releaseKeysWhenEmpty ? 'components.skillPickerMenu.no_matching_skills' : sendOnEnter === 'ctrl-enter' ? 'components.skillPickerMenu.no_matching_skills_ctrl_enter_sends' : 'components.skillPickerMenu.no_matching_skills_enter_sends')}</div>

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left, width: Math.min(width, 460), maxHeight }}
    >
      {results.length === 0 ? empty : results.map((s, i) => {
        const leaf = leafOf(s.key || s.name)
        const gated = needsTrust(s)
        const info = { leaf, key: s.key || s.name }
        return (
          <div
            role="option"
            aria-selected={i === selected}
            tabIndex={-1}
            key={s.key || s.name}
            ref={el => { itemRefs.current[i] = el }}
            className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
            title={s.key}
            onMouseEnter={() => setSelected(i)}
            onMouseDown={e => {
              e.preventDefault()
              if (gated && onTrustRequest) onTrustRequest(info)
              else onSelect(info)
            }}
          >
            {gated
              ? <Lock size={14} className="shrink-0 lucide-inline" />
              : <Sparkles size={14} className="shrink-0 lucide-inline" />}
            <div className="flex-1 min-w-0">
              <div className={`text-[13px] font-mono font-semibold truncate ${gated ? 'text-muted' : ''}`}>${leaf}</div>
              <div className="text-[11px] text-muted truncate">
                {gated ? i18nT('components.skillPickerMenu.trust_needed_hint') : (s.description || s.key)}
              </div>
            </div>
            {gated
              ? (
                <span className="text-[10px] text-warn shrink-0 whitespace-nowrap uppercase tracking-wide">
                  {i18nT('components.skillPickerMenu.trust_needed_badge')}
                </span>
              )
              : s.source && s.source !== 'kirocrew' && (
                <span className="text-[10px] text-muted shrink-0 whitespace-nowrap uppercase tracking-wide">{s.source}</span>
              )}
          </div>
        )
      })}
    </div>,
    document.body
  )
}
