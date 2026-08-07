import { useState, useEffect, useCallback, useRef, useMemo, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { Search, X, Pin, MessageSquare, Clock, Plus } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { useAppSelector } from '../store'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { useSimplifiedToolNames } from '../hooks/useSimplifiedToolNames'
import type { Result, ResourceProvider } from './commandPalette/types'
import { registerProvider } from './commandPalette/providers'
import { usePaletteActions } from './commandPalette/paletteActions'
import { useAllAggregator } from './commandPalette/providers/allAggregator'
import { useSessionsProvider } from './commandPalette/providers/sessionsProvider'
import type { SessionRef } from './commandPalette/providers/sessionsProvider'
import { usePagesProvider } from './commandPalette/providers/pagesProvider'
import { useActionsProvider } from './commandPalette/providers/actionsProvider'
import { useKnowledgeProvider } from './commandPalette/providers/knowledgeProvider'
import type { KnowledgeRef } from './commandPalette/providers/knowledgeProvider'
import { useSkillsProvider } from './commandPalette/providers/skillsProvider'
import { usePromptsProvider } from './commandPalette/providers/promptsProvider'
import { useArtifactsProvider } from './commandPalette/providers/artifactsProvider'
import { useRecentsProvider } from './commandPalette/providers/recentsProvider'
import { useSettingsProvider } from './commandPalette/providers/settingsProvider'
import { useAppsProvider } from './commandPalette/providers/appsProvider'
import { Highlighted } from './commandPalette/Highlighted'

import { i18nT } from '../i18n/t'
/**
 * Search Everywhere command palette.
 *
 * A portal-rendered, centered modal — the IntelliJ-style "search everything"
 * surface. It owns:
 *  - the search input,
 *  - a tab strip (All · Sessions · Knowledge · Skills · Prompts, with Pages +
 *    Actions riding along),
 *  - a result list with matched-character highlighting.
 *
 * Keyboard model (reuses {@link useListKeyboardNav} verbatim for the row
 * selection + scroll-into-view that the picker menus already share):
 *  - Arrow Up / Down       — move the selection.
 *  - Enter                 — `dispatchEnter(result, false)` (then close).
 *  - ⌘/Ctrl + Enter        — `dispatchEnter(result, true)` — the modifier
 *                            branch of the Enter matrix (always-new-session /
 *                            attach-as-context); then close.
 *  - ⌥/Alt + Enter         — `result.onAltActivate()` (read / preview, e.g.
 *                            open SKILL.md); does not close the palette.
 *  - Tab / Shift+Tab       — cycle the active category tab (scopes results to
 *                            one provider). Never "chooses" a row here.
 *  - Escape                — close.
 *
 * Enter and ⌘/Ctrl+Enter both flow through one central {@link dispatchEnter}
 * ({@link OnEnter}): the shared {@link useListKeyboardNav} hook computes the
 * `withModifier` flag (`metaKey || ctrlKey`) on the Enter keypress and threads
 * it into `onChoose(index, withModifier)`; `dispatchEnter` looks the row up and
 * switches on its declarative {@link EnterAction} (`result.enter`), falling back
 * to the legacy `on*Activate` closures while providers are migrated.
 *
 * `useListKeyboardNav` maps Tab to "choose" (its picker-menu default). To take
 * over Tab (cycle category) and ⌥/Alt+Enter (preview) without forking the
 * shared hook, this component registers a *window*-phase capture listener:
 * window-capture fires before document-capture, so it can
 * `stopImmediatePropagation()` those two keys before the hook's document-level
 * listener sees them.
 *
 * Highlighting renders matched indices as React `<strong>` nodes split out of
 * the title — never `dangerouslySetInnerHTML` (`frontend-security` lint rule).
 * Visuals reuse the shared design tokens + the portal/result-row pattern from
 * `SkillPickerMenu.tsx`; no hardcoded Tailwind colors.
 */

export interface CommandPaletteProps {
  /** Whether the palette is shown. */
  open: boolean
  /** Close the palette (Escape, overlay click, or after activation). */
  onClose: () => void
  /**
   * Open the keyboard-shortcuts help modal (backs the Actions provider's
   * "Open Shortcuts" command). Supplied by the app shell, which owns the
   * `ShortcutsModal` open state. Defaults to a no-op.
   */
  openShortcuts?: () => void
  /**
   * Open a session in a split pane / Session Grid (⌘Enter on a session row).
   * Optional — when omitted, ⌘Enter on a session falls back to plain open.
   */
  openInSplit?: (ref: SessionRef) => void
}

/** Stable no-op so `useActionsProvider` always gets a defined callback. */
const NOOP = () => {}

/**
 * Idle gap before a typed query is dispatched to the providers. Sized against
 * the cost of a dispatch, not typing feel: one debounced value fans out to
 * every registered provider (see {@link useAllAggregator}), and Sessions +
 * Artifacts both content-search server-side.
 */
export const SEARCH_DEBOUNCE_MS = 250

/** Composer-style sigil scopes: typing a leading sigil instantly scopes the
 * palette, mirroring the chat composer's `$skill` / `/command` muscle memory.
 * `@` is mapped to Artifacts here (the palette's `@` destination). Value =
 * provider (tab) id. */
const SIGIL_SCOPE: Record<string, string> = { $: 'skills', '@': 'artifacts', '/': 'actions' }

export default function CommandPalette({
  open,
  onClose,
  openShortcuts,
  openInSplit,
}: CommandPaletteProps) {
  // P0 providers. Each is memoized inside its hook, so identities are stable.
  const all = useAllAggregator()
  const sessions = useSessionsProvider({ openInSplit })
  const pages = usePagesProvider()
  const actions = useActionsProvider({ openShortcuts: openShortcuts ?? NOOP })

  // Live chat-store/router actions backing the Enter matrix. `usePaletteActions`
  // exposes the same composer-insert (`setPendingInput`) and new-session
  // (`createSlot` + `setPendingInput`) paths the inline `$`/`@` pickers use, plus
  // the context-aware `enterInsertOrNewSession` helper (insert into the active
  // chat, else open a new seeded session). Token resolution stays server-side on
  // submit (input validation) — the palette only ever emits a plain `$skill` / `@prompt` /
  // `@knowledge` string. Used by the `insert-token` + `open-knowledge` branches
  // of {@link dispatchEnter}.
  const { enterInsertOrNewSession, newSessionWithToken, navigate } = usePaletteActions()

  // ⌘Enter on a Knowledge row attaches the entry as context to the active chat.
  // There is no global attach-by-id API — the shipped
  // path is the `@knowledge <query>` composer prefix the chat surface intercepts
  // (`useKnowledgeFetch.extractKnowledgeQuery`) to pull entries in as context.
  // So we reuse that entry-point: seed the active chat (else a new session) with
  // `@knowledge <title>`. Input validation: a plain token string — the FE picker / server
  // search resolve it; no filesystem access from user-controlled values.
  const attachKnowledgeAsContext = useCallback(
    (ref: KnowledgeRef) => enterInsertOrNewSession(`@knowledge ${ref.title}`),
    [enterInsertOrNewSession],
  )

  // P1 providers (Knowledge · Skills · Prompts). Each is memoized inside its
  // hook, so identities are stable across renders. Knowledge gets the
  // attach-as-context callback so its ⌘Enter is bound.
  const knowledge = useKnowledgeProvider({ attachAsContext: attachKnowledgeAsContext })
  const skills = useSkillsProvider()
  const prompts = usePromptsProvider()
  // Artifacts tab — content-search over the artifact library with a
  // match-centered preview (wraps GET /api/artifacts?content=1&snippet=1).
  const artifacts = useArtifactsProvider()
  // Recent-sessions quick-switcher for the unscoped empty state (not a tab).
  const recents = useRecentsProvider()
  // Settings — instant client-side search over the codegen settings registry.
  const settings = useSettingsProvider()
  // Apps — launch an installed app by name. Destinations come from the shared
  // `appNav` derivation the left rail uses, so the two cannot disagree.
  const apps = useAppsProvider()

  // Tab strip order (§1): All · Sessions · Knowledge · Skills ·
  // Prompts, with Artifacts + Apps + Pages + Actions riding along after the v1
  // corpus. Apps sits next to Pages because both are pure navigation targets.
  const tabs = useMemo<ResourceProvider[]>(
    () => [all, sessions, knowledge, skills, prompts, artifacts, apps, pages, actions, settings],
    [all, sessions, knowledge, skills, prompts, artifacts, apps, pages, actions, settings],
  )

  // Make the per-category providers discoverable by the All aggregator, which
  // fans the query out to everything in the registry. Skills and Knowledge are
  // deliberately NOT registered here — skills are noisy in the global blend,
  // and knowledge search embeds the query through a local Ollama model
  // (~90ms warm, ~1.7s when the model has been unloaded), which stalls the
  // Promise.all fan-out and drags every other provider's results with it.
  // Both surface only when scoped (sigil or prefix+Tab), reached directly via
  // the tabs list rather than the aggregator. Re-registration is idempotent.
  //
  // Apps IS registered: the list is one small cached request on a key the Apps
  // page already warms, and "type a name, press Enter to launch" is the whole
  // point of putting apps in the palette — it has to work from the default tab.
  useEffect(() => {
    registerProvider(sessions)
    registerProvider(prompts)
    registerProvider(artifacts)
    registerProvider(apps)
    registerProvider(pages)
    registerProvider(actions)
    registerProvider(settings)
  }, [sessions, prompts, artifacts, apps, pages, actions, settings])

  const [query, setQuery] = useState('')
  const [scope, setScope] = useState<string | null>(null)
  // Debounced query drives the actual fetch so we don't hit the network (and,
  // for artifacts/sessions, re-scan content server-side) on every keystroke.
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const resultsRef = useRef<Result[]>([])

  const inputRef = useRef<HTMLInputElement | null>(null)
  const q = query.trim()
  const providerById = useMemo(
    () => Object.fromEntries(tabs.map((t) => [t.id, t])),
    [tabs],
  )
  // Prefix + Tab scoping (blended from our palette): when unscoped and the
  // query uniquely prefixes one category's label/id (excluding "all"), Tab
  // adopts that scope. Ambiguous prefixes show no hint until unique.
  const scopeHint = useMemo(() => {
    if (scope || !q) return null
    const ql = q.toLowerCase()
    const hits = tabs.filter(
      (t) =>
        t.id !== 'all' &&
        (t.label.toLowerCase().startsWith(ql) || t.id.toLowerCase().startsWith(ql)),
    )
    return hits.length === 1 ? hits[0] : null
  }, [scope, q, tabs])
  const scopeProvider = scope ? providerById[scope] : undefined
  const scopeLabel = scopeProvider?.label
  // Default (unscoped + empty) opens onto recent sessions — a quick switcher.
  // Unscoped typing searches everything (All aggregator). A scope narrows to
  // that one category.
  const activeProvider = scopeProvider ?? (q === '' ? recents : all)
  // Stable refs so the window key handler doesn't re-subscribe per keystroke.
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const scopeHintRef = useRef(scopeHint)
  scopeHintRef.current = scopeHint
  const queryRef = useRef(query)
  queryRef.current = query

  /**
   * Central Enter dispatcher ({@link OnEnter}). Switches on the
   * result's declarative {@link EnterAction} (`result.enter`) and routes to the
   * per-type branch; `withModifier` is `true` for ⌘/Ctrl+Enter (the
   * always-new-session / attach-as-context branch of the matrix).
   *
   * Migration-safe: providers that have not yet been ported to populate
   * `result.enter` fall through to the legacy `onActivate` / `onCmdActivate`
   * closures, so current behavior is unchanged. ⌥/Alt+Enter (preview) is
   * intentionally NOT handled here — it stays a separate, non-closing path in
   * the window-capture listener below.
   *
   * Always closes the palette after dispatching (Enter / ⌘Enter both close;
   * only preview keeps it open).
   */
  const dispatchEnter = useCallback(
    (result: Result, withModifier: boolean) => {
      // Skills are a navigation target in the palette: Enter opens the skills
      // catalog (to view/edit them) rather than inserting a $skill token —
      // there's no per-skill deep link yet, so all skill rows go to the catalog.
      if (result.providerId === 'skills') {
        navigate('/capabilities')
        onClose()
        return
      }
      const action = result.enter
      if (action) {
        switch (action.kind) {
          case 'open-session':
            // Sessions: Enter opens/switches, ⌘Enter opens in split (Grid).
            if (withModifier && result.onCmdActivate) {
              result.onCmdActivate()
            } else {
              result.onActivate()
            }
            break
          case 'insert-token': {
            // Skills / Prompts. Primary Enter is
            // context-aware: insert the token (`$<skill>` / `@<prompt>`, from
            // the result payload) into the active chat composer, or — when no
            // chat is active — open a new session seeded with it.
            // `enterInsertOrNewSession` encodes that branch off the live
            // `hasActiveChat` predicate. ⌘Enter is always "new seeded session".
            // The token is a plain string; allowlisted resolution happens
            // server-side on submit (input validation, see paletteActions.ts).
            if (withModifier) {
              newSessionWithToken(action.token)
            } else {
              enterInsertOrNewSession(action.token)
            }
            break
          }
          case 'open-knowledge':
            // Knowledge. Primary Enter opens /
            // navigates to the entry; ⌘Enter attaches it as context to the
            // active chat. Both reuse the provider-bound closures
            // (`onActivate` → openEntry, `onCmdActivate` → attachAsContext);
            // `action.entryId`/`title` are the declarative, testable payload.
            // When the host supplies no attach callback (`onCmdActivate`
            // undefined), ⌘Enter degrades to opening the entry.
            if (withModifier && result.onCmdActivate) {
              result.onCmdActivate()
            } else {
              result.onActivate()
            }
            break
          case 'navigate':
            // Pages: navigate to the page route. Pages
            // are pure navigation targets — ⌘Enter takes NO distinct action, so
            // `withModifier` is intentionally ignored (⌘Enter == Enter). The
            // provider bound `navigate(action.route)` into `onActivate`; reuse
            // it as the execution path (the declarative `action.route` is the
            // testable payload), mirroring the `open-session` branch.
            result.onActivate()
            break
          case 'invoke':
            // Actions: run the free action callback.
            // Actions are pure command-invocations — ⌘Enter takes NO distinct
            // action, so `withModifier` is intentionally ignored
            // (⌘Enter == Enter). The callback rides on the declarative payload,
            // so invoke it directly.
            action.run()
            break
          default: {
            // Exhaustiveness guard — every EnterAction kind must have a branch.
            const _exhaustive: never = action
            console.warn('[CommandPalette] dispatchEnter: unhandled enter action', _exhaustive)
          }
        }
      } else if (withModifier && result.onCmdActivate) {
        // Legacy closure fallback (pre-`enter` providers): ⌘Enter.
        result.onCmdActivate()
      } else {
        // Legacy closure fallback (pre-`enter` providers): primary Enter.
        result.onActivate()
      }
      onClose()
    },
    [onClose, enterInsertOrNewSession, newSessionWithToken, navigate],
  )

  // Enter / ⌘Enter from the shared hook: look up the chosen row and dispatch,
  // threading the modifier flag through to {@link dispatchEnter}.
  // Search via React Query — handles caching, cancellation, dedup automatically.
  // The recents quick-switcher renders LIVE redux state (running / approval /
  // unread / pin / recency), but React Query only re-invokes search() on a KEY
  // change — a re-memoized provider identity alone does nothing. Fold a
  // fingerprint of that live state into the key so status dots and MRU order
  // update while the palette is open instead of freezing at open-time.
  const slots = useAppSelector((s) => s.dashboard.slots)
  const unreadSlots = useAppSelector((s) => s.dashboard.unreadSlots)
  const slotStatusDetail = useAppSelector((s) => s.chat.slotStatusDetail ?? {})
  // Rows render a tool status through toolStatusLabel, so the purpose-vs-raw-title
  // preference is part of the live state the key has to cover: without it, toggling
  // the setting between two opens leaves a running row on its pre-toggle label until
  // the next status tick or staleTime expiry.
  const simplifiedToolNames = useSimplifiedToolNames()
  const liveFingerprint = useMemo(
    () =>
      activeProvider.id === 'recents'
        ? slots
            .map(
              (s) =>
                `${s.key}:${s.running ? 1 : 0}${s.pending_approval ? 1 : 0}${
                  s.pinned ? 1 : 0
                }:${s.last_activity_ts ?? s.last_ts ?? ''}:${
                  slotStatusDetail[s.key]?.kind ?? ''
                }:${slotStatusDetail[s.key]?.text ?? ''}:${slotStatusDetail[s.key]?.ts ?? ''}`,
            )
            .join('|') + `#${unreadSlots.join(',')}#${simplifiedToolNames ? 1 : 0}`
        : '',
    [activeProvider.id, slots, unreadSlots, slotStatusDetail, simplifiedToolNames],
  )
  const { data: results = [], isLoading: loading } = useQuery({
    queryKey: ['palette', 'search', activeProvider.id, debouncedQuery, liveFingerprint],
    queryFn: () => Promise.resolve(activeProvider.search(debouncedQuery)),
    enabled: open,
    placeholderData: (prev) => prev ?? [],
    staleTime: 10_000,
  })

  // Debounce the query into debouncedQuery (empty resets immediately so the
  // recents/quick-switcher shows without lag on open or clear).
  //
  // 250ms rather than a tighter window because each debounced value is a fresh
  // React Query key, and the All tab fans that out to every provider — two of
  // which content-search server-side. A 150ms window turned typing one word
  // into five or six full corpus scans that the user never saw the results of.
  useEffect(() => {
    if (query === '') {
      setDebouncedQuery('')
      return
    }
    const t = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [query])

  // Keep resultsRef in sync for imperative reads (Enter handler).
  useEffect(() => {
    resultsRef.current = results
  }, [results])

  const onChoose = useCallback(
    (idx: number, withModifier: boolean) => {
      const r = resultsRef.current
      const item = r[idx >= r.length ? 0 : idx]
      if (!item) return
      dispatchEnter(item, withModifier)
    },
    [dispatchEnter],
  )

  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    wrap: true,
    onChoose,
    onClose,
  })

  // Reset selection to top when results change (new query or tab switch).
  useEffect(() => { setSelected(0) }, [results, setSelected])

  // Reset query + scope each time the palette (re)opens, and focus input.
  useEffect(() => {
    if (!open) return
    setQuery('')
    setScope(null)
    setDebouncedQuery('')
    const id = requestAnimationFrame(() => inputRef.current?.focus())
    return () => cancelAnimationFrame(id)
  }, [open])

  // Window-capture listener for the two keys the shared hook doesn't own the
  // way the palette needs: Tab (cycle category) and ⌥/Alt+Enter (preview).
  // window-capture runs before the hook's document-capture listener, so
  // stopImmediatePropagation here keeps the hook from also acting on them.
  useEffect(() => {
    if (!open) return
    const onWinKey = (e: KeyboardEvent) => {
      if (e.key === 'Tab') {
        e.preventDefault()
        e.stopImmediatePropagation()
        // Prefix + Tab adopts the hinted scope (clearing the query); Shift+Tab
        // while scoped pops it.
        if (!scopeRef.current && scopeHintRef.current) {
          setScope(scopeHintRef.current.id)
          setQuery('')
        } else if (scopeRef.current && e.shiftKey) {
          setScope(null)
        }
      } else if (e.key === 'Backspace' && scopeRef.current && queryRef.current === '') {
        // Backspace on an empty query pops the active scope (like a chip).
        e.preventDefault()
        e.stopImmediatePropagation()
        setScope(null)
      } else if (e.key === 'Enter' && e.altKey && !e.metaKey && !e.ctrlKey) {
        e.preventDefault()
        e.stopImmediatePropagation()
        const r = resultsRef.current
        const item = r[selectedRef.current] ?? r[0]
        item?.onAltActivate?.()
        // Preview deliberately leaves the palette open.
      }
    }
    window.addEventListener('keydown', onWinKey, true)
    return () => window.removeEventListener('keydown', onWinKey, true)
  }, [open, selectedRef])

  if (!open) return null

  const emptyState = loading ? (
    <div className="px-3 py-6 text-center text-[12px] text-muted">{i18nT('components.commandPalette.searching')}</div>
  ) : (
    <div className="px-3 py-6 text-center text-[12px] text-muted">
      {query.trim()
        ? 'No matches'
        : scopeLabel
          ? i18nT('components.commandPalette.no_scope', { scope: scopeLabel.toLowerCase() })
          : 'No recent sessions'}
    </div>
  )

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-start justify-center bg-bg/60 backdrop-blur-sm animate-rise"
      role="dialog"
      aria-modal="true"
      aria-label={i18nT('components.commandPalette.search_everywhere')}
      onMouseDown={onClose}
    >
      <div
        className="mt-[12vh] w-full max-w-xl mx-4 bg-card border border-border rounded-xl shadow-xl overflow-hidden flex flex-col max-h-[70vh]"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
          <Search size={16} className="shrink-0 text-muted lucide-inline" />
          {scopeLabel && (
            <span className="shrink-0 flex items-center gap-1 rounded-md bg-accent-subtle px-2 py-0.5 text-[12px] font-medium text-text">
              {scopeLabel}
              <button
                type="button"
                aria-label={i18nT('components.commandPalette.clear_filter')}
                className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 flex items-center"
                onMouseDown={(e) => {
                  e.preventDefault()
                  setScope(null)
                  inputRef.current?.focus()
                }}
              >
                <X size={12} />
              </button>
            </span>
          )}
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              const v = e.target.value
              // Leading sigil ($/@//) instantly adopts the matching scope and
              // strips the sigil — mirrors the composer's $skill/@prompt/
              // /command tokens. Only fires when unscoped and the sigil maps to
              // a real provider.
              const sigil = !scope && v.length ? SIGIL_SCOPE[v[0]] : undefined
              if (sigil && providerById[sigil]) {
                setScope(sigil)
                setQuery(v.slice(1))
              } else {
                setQuery(v)
              }
            }}
            placeholder={scopeLabel ? i18nT('components.commandPalette.search_scope', { scope: scopeLabel.toLowerCase() }) : i18nT('components.commandPalette.search_for_anything')}
            aria-label={i18nT('components.commandPalette.search_everywhere')}
            className="flex-1 bg-transparent border-none outline-none text-[14px] text-text placeholder:text-muted"
          />
          {scopeHint && (
            <span className="shrink-0 flex items-center gap-1 text-[11px] text-muted">
              <kbd className="font-mono px-1 rounded bg-bg border border-border">{i18nT('components.commandPalette.tab')}</kbd> {scopeHint.label}
            </span>
          )}
          <button
            type="button"
            className="text-muted cursor-pointer hover:text-text bg-transparent border-none"
            onClick={onClose}
            aria-label={i18nT('components.commandPalette.close')}
          >
            <X size={16} />
          </button>
        </div>

        {/* Result list */}
        <div className="overflow-y-auto py-1" role="listbox">
          {results.length === 0
            ? emptyState
            : results.map((r, i) => {
                // Section header whenever the group changes — the recents view
                // sets groupLabel (Current / Planned / Today / Yesterday /
                // Earlier); search views leave it unset, so no headers render.
                const showHeader =
                  !!r.groupLabel && r.groupLabel !== results[i - 1]?.groupLabel
                // Recents rows (groupLabel set) render as sidebar-style cards:
                // agent + time on the top metadata line, bold title below, a
                // status/last-message line, and no leading icon. Search rows
                // keep the compact icon + title + subtitle layout.
                const isRecents = !!r.groupLabel
                return (
                  <Fragment key={r.id}>
                    {showHeader && (
                      <div
                        role="presentation"
                        className="px-4 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted flex items-center gap-1.5"
                      >
                        {r.groupLabel === 'Scheduled' ? (
                          <Clock size={11} className="lucide-inline" />
                        ) : (
                          <MessageSquare size={11} className="lucide-inline" />
                        )}
                        {r.groupLabel}
                      </div>
                    )}
                    <div
                      role="option"
                      aria-selected={i === selected}
                      tabIndex={-1}
                      ref={(el) => {
                        itemRefs.current[i] = el
                      }}
                      className={`relative w-full text-left px-4 py-2 cursor-pointer transition-colors ${
                        i === selected
                          ? 'bg-accent-subtle text-text'
                          : 'text-muted hover:bg-bg-hover hover:text-text'
                      } ${r.faded ? 'opacity-60' : ''} ${
                        isRecents ? '' : 'flex items-start gap-3'
                      }`}
                      title={r.subtitle || r.title}
                      onMouseEnter={() => setSelected(i)}
                      onMouseDown={(e) => {
                        e.preventDefault()
                        dispatchEnter(r, e.metaKey || e.ctrlKey)
                      }}
                    >
                      {isRecents ? (
                        <>
                          <div className="min-w-0">
                            {/* Line 1 — pin + bold title + folder chip, with the
                                timestamp and status dot right-aligned on the SAME row. */}
                            <div className="text-[13px] font-semibold leading-snug text-text flex items-center gap-1.5 min-w-0">
                              {r.pinned && (
                                <Pin
                                  size={10}
                                  className="text-accent shrink-0 lucide-inline"
                                  aria-label={i18nT('components.commandPalette.pinned')}
                                />
                              )}
                              {r.isNew && (
                                <Plus
                                  size={14}
                                  className="text-accent shrink-0 lucide-inline"
                                />
                              )}
                              <span className="truncate">
                                <Highlighted text={r.title} indices={r.indices} />
                              </span>
                              {r.folder && (
                                <span className="shrink-0 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-normal text-muted bg-bg-hover">
                                  {r.folder}
                                </span>
                              )}
                              {(r.timestamp || r.statusDot) && (
                                <span className="ml-auto flex items-center gap-1.5 shrink-0 text-[11px] font-normal text-muted">
                                  {r.timestamp && <span>{r.timestamp}</span>}
                                  {r.statusDot && (
                                    <span
                                      className={`w-2 h-2 rounded-full pointer-events-none ${
                                        r.statusDot.pulse ? 'animate-pulse' : ''
                                      }`}
                                      style={{ background: `var(${r.statusDot.colorVar})` }}
                                      aria-hidden="true"
                                    />
                                  )}
                                </span>
                              )}
                            </div>
                            {/* Line 3 — status accent or last-message preview. */}
                            {r.statusColorVar ? (
                              r.statusStyle === 'pill' ? (
                                <div className="text-[12px] leading-snug truncate mt-0.5 flex items-center gap-1.5 min-w-0">
                                  <span
                                    className="shrink-0 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold text-white"
                                    style={{ backgroundColor: `var(${r.statusColorVar})` }}
                                  >
                                    {r.statusLabel}
                                  </span>
                                  {r.statusDetail ? (
                                    <span className="text-muted truncate">{r.statusDetail}</span>
                                  ) : null}
                                </div>
                              ) : (
                                <div className="text-[12px] leading-snug truncate mt-0.5 flex items-center gap-1.5 min-w-0">
                                  <span
                                    className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                                      r.statusPulse ? 'animate-pulse' : ''
                                    }`}
                                    style={{ backgroundColor: `var(${r.statusColorVar})` }}
                                  />
                                  <span className="truncate">
                                    <span
                                      className="font-medium"
                                      style={{ color: `var(${r.statusColorVar})` }}
                                    >
                                      {r.statusLabel}
                                    </span>
                                    {r.statusDetail ? (
                                      <span className="text-muted"> · {r.statusDetail}</span>
                                    ) : null}
                                  </span>
                                </div>
                              )
                            ) : r.subtitle ? (
                              <div className="text-[12px] text-muted leading-snug truncate mt-0.5">
                                {r.subtitleIndices ? (
                                  <Highlighted text={r.subtitle} indices={r.subtitleIndices} />
                                ) : (
                                  r.subtitle
                                )}
                              </div>
                            ) : null}
                          </div>
                        </>
                      ) : (
                        <>
                          <span className="shrink-0 flex items-center mt-[1px]">{r.icon}</span>
                          <div className="flex-1 min-w-0">
                            <div className="text-[13px] flex items-center gap-1.5 min-w-0">
                              <span className="truncate">
                                <Highlighted text={r.title} indices={r.indices} />
                              </span>
                              {r.folder && (
                                <span className="shrink-0 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-normal text-muted bg-bg-hover">
                                  {r.folder}
                                </span>
                              )}
                            </div>
                            {r.subtitle ? (
                              <div className="text-[11px] text-muted truncate mt-0.5">
                                {r.subtitleIndices ? (
                                  <Highlighted text={r.subtitle} indices={r.subtitleIndices} />
                                ) : (
                                  r.subtitle
                                )}
                              </div>
                            ) : null}
                          </div>
                        </>
                      )}
                    </div>
                  </Fragment>
                )
              })}
        </div>
      </div>
    </div>,
    document.body,
  )
}
