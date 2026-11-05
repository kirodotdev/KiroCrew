import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { OverlayPortal } from './overlayRoot'
import { usePickerSource } from './dashboardPickerSource'
import ErrorNotice from './ErrorNotice'
import { pickerQueryKey, type PickerWorkflow, type SlashCommand } from '../chat-core/composer/pickerSource'
import { SLASH_MENU_QUERY, WORKFLOW_PICK_QUERY } from '../chat-core/composer/slashQuery'
import { i18nT } from '../i18n/t'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'
import type { SendMode } from '../pages/chat/ChatSettings'

/**
 * Catalog KEY for each command's description, keyed by the command's TRIGGER
 * TOKEN.
 *
 * The token (`/compact`) is the command itself — what the user types and what
 * `onSelect` inserts into the composer — so it is never translated; only the
 * description is copy.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language and never re-resolve on a language
 * switch. The lookup happens in {@link commandDescription}, which runs during
 * render. Shaped as a flat `Record` of full literal keys, indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically.
 *
 * This is also what localises the LIVE path, not just the offline fallback. The
 * descriptions normally come from `GET /api/slash-commands` (backend
 * `SLASH_COMMAND_DESCRIPTIONS`), which is English-only and replaces the fallback
 * as soon as the query resolves — so translating the fallback array alone would
 * have shown localised text for a few milliseconds and English from then on.
 * Resolving by token covers both sources.
 *
 * `/q` and `/quit` are aliases and deliberately share one key rather than
 * duplicating the string.
 */
const COMMAND_DESC_KEY: Record<string, string> = {
  '/agent': 'components.slashCommandMenu.desc_agent',
  // Alias of /side, so it shares the description key deliberately: the two
  // rows must never drift apart, and the locale catalogs stay untouched.
  '/btw': 'components.slashCommandMenu.desc_side',
  '/changelog': 'components.slashCommandMenu.desc_changelog',
  '/chat': 'components.slashCommandMenu.desc_chat',
  '/clear': 'components.slashCommandMenu.desc_clear',
  '/code': 'components.slashCommandMenu.desc_code',
  '/compact': 'components.slashCommandMenu.desc_compact',
  '/context': 'components.slashCommandMenu.desc_context',
  '/editor': 'components.slashCommandMenu.desc_editor',
  '/exit': 'components.slashCommandMenu.desc_exit',
  '/experiment': 'components.slashCommandMenu.desc_experiment',
  '/help': 'components.slashCommandMenu.desc_help',
  '/hooks': 'components.slashCommandMenu.desc_hooks',
  '/issue': 'components.slashCommandMenu.desc_issue',
  '/kb': 'components.slashCommandMenu.desc_kb',
  '/logdump': 'components.slashCommandMenu.desc_logdump',
  '/mcp': 'components.slashCommandMenu.desc_mcp',
  '/model': 'components.slashCommandMenu.desc_model',
  '/onboarding': 'components.slashCommandMenu.desc_onboarding',
  '/paste': 'components.slashCommandMenu.desc_paste',
  '/plain': 'components.slashCommandMenu.desc_plain',
  '/prompts': 'components.slashCommandMenu.desc_prompts',
  '/q': 'components.slashCommandMenu.desc_quit',
  '/quit': 'components.slashCommandMenu.desc_quit',
  '/reply': 'components.slashCommandMenu.desc_reply',
  '/side': 'components.slashCommandMenu.desc_side',
  '/tangent': 'components.slashCommandMenu.desc_tangent',
  '/todos': 'components.slashCommandMenu.desc_todos',
  '/tools': 'components.slashCommandMenu.desc_tools',
  '/usage': 'components.slashCommandMenu.desc_usage',
  '/workflow': 'components.slashCommandMenu.desc_workflow',
}

/**
 * Localised description for a command row, resolved per render.
 *
 * A command the backend reports that has no entry above (a new server-side
 * command this build has no key for) falls back to the server's own English
 * text rather than rendering a raw key.
 */
function commandDescription(cmd: SlashCommand): string {
  // `hasOwnProperty`, not `in`: the names come from the API, so a backend
  // reporting `toString` or `constructor` would otherwise resolve to an
  // inherited Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(COMMAND_DESC_KEY, cmd.name)
    ? i18nT(COMMAND_DESC_KEY[cmd.name])
    : cmd.description ?? ''
}

// Offline fallback shown before the API query resolves (or if it fails).
// Kept in sync with the backend's GET /api/slash-commands payload — the
// _SLASH_COMMANDS set MINUS _BLOCKED_SLASH_COMMANDS — so the same commands
// appear whether they came from the live API or this fallback. Blocked
// commands (/quit, /exit, /q, /chat, /paste, /reply, /editor, /tangent) are
// terminal-only kiro-cli gestures the dashboard rejects. /issue is documented
// by the interactive CLI but unsupported by the ACP harness, and /todos is one
// the ACP harness does not implement, so suggesting any of them anywhere is an
// inert affordance; the descriptions themselves come from COMMAND_DESC_KEY
// either way. /kb is a frontend-only command (also merged via
// FRONTEND_COMMANDS below).
const FALLBACK_COMMAND_NAMES = [
  '/agent', '/changelog', '/clear', '/code', '/compact', '/context',
  '/experiment', '/help', '/hooks', '/kb', '/logdump',
  '/mcp', '/model', '/prompts', '/side', '/tools', '/usage', '/workflow',
] as const

const FALLBACK_COMMANDS: SlashCommand[] = FALLBACK_COMMAND_NAMES.map(name => ({ name }))

interface Props {
  input: string
  anchorRef: React.RefObject<HTMLElement | null>
  onSelect: (command: string) => void
  onClose: () => void
  open?: boolean
  /**
   * The composer's effective send binding (see ChatInput's SendMode). Only
   * read by the settled-empty copy: in 'ctrl-enter' mode a released bare
   * Enter is a newline, so the announcement must name Ctrl+Enter instead.
   */
  sendOnEnter?: SendMode
  /** Commands the host refuses; left out of the list. */
  hidden?: readonly string[]
}

/**
 * Commands the backend's GET /api/slash-commands does not report, merged in so
 * the menu still offers them. Two different kinds live here, and the difference
 * matters when adding a row:
 *
 * - CLIENT-INTERCEPTED (`/btw`, `/kb`, `/onboarding`): the composer recognises
 *   the text and acts on it locally; the message is never sent. Those also need
 *   a branch in `interceptSlashCommand` (`/btw` rides `/side`'s — it is a pure
 *   alias, matched by the same SIDE_RE).
 * - QUICK PROMPT (`/plain`): a backend MACRO. The message IS sent, unchanged, and
 *   `ContextBuilder.build_message` swaps the token for the instruction it stands
 *   for (`src/kiro_crew/quick_prompts.py`). It must therefore stay OUT of
 *   `interceptSlashCommand` — intercepting it would stop it ever reaching the
 *   expansion — and out of the kiro-cli passthrough set, which would forward it
 *   to a harness that has no such command.
 */
const FRONTEND_COMMAND_NAMES = ['/btw', '/kb', '/onboarding', '/plain'] as const

const FRONTEND_COMMANDS: SlashCommand[] = FRONTEND_COMMAND_NAMES.map(name => ({ name }))

/**
 * A menu row: a command, or -- once `/workflow ` is typed -- a saved workflow.
 * The workflow rows are the pick the backend's bare `/workflow` reply lists as
 * text (`chat_turn/slash_commands.py`); choosing one writes `/workflow <slug> `
 * so the user can add the run input before sending.
 */
type Row = { kind: 'cmd'; cmd: SlashCommand } | { kind: 'workflow'; workflow: PickerWorkflow }

const rowKey = (r: Row) => (r.kind === 'cmd' ? r.cmd.name : `/workflow ${r.workflow.slug}`)
const rowInsert = (r: Row) => rowKey(r) + ' '

/** Saved workflows are re-read at most this often while the menu is in use. */
const WORKFLOWS_STALE_MS = 30_000

export default function SlashCommandMenu({ input, anchorRef, onSelect, onClose, open = true, sendOnEnter = 'enter', hidden }: Props) {
  const source = usePickerSource()
  const load = source.slashCommands
  const { data: apiCommands = FALLBACK_COMMANDS, isFetching, isError, refetch } = useQuery<SlashCommand[]>({
    queryKey: pickerQueryKey(source, ['slash-commands']),
    queryFn: ({ signal }) => load!(signal),
    enabled: typeof load === 'function',
  })
  const allCommands = useMemo(() => {
    const names = new Set(apiCommands.map(c => c.name))
    return [...apiCommands, ...FRONTEND_COMMANDS.filter(c => !names.has(c.name))].sort((a, b) => a.name.localeCompare(b.name))
  }, [apiCommands])
  const commands = useMemo(
    () => (hidden?.length ? allCommands.filter(c => !hidden.includes(c.name)) : allCommands),
    [allCommands, hidden],
  )

  const loadWorkflows = source.workflows
  const workflowsOn = typeof loadWorkflows === 'function' && !hidden?.includes('/workflow')
  const match = input.match(SLASH_MENU_QUERY)
  const workflowMatch = workflowsOn ? input.match(WORKFLOW_PICK_QUERY) : null
  const workflowMode = !!workflowMatch
  const visible = open && (!!match || workflowMode)
  const filter = match?.[1] ?? ''
  const workflowFilter = workflowMatch?.[1] ?? ''

  // The saved workflows, read only once `/workflow ` is typed. Same key as the
  // Workflows library, so a save there refreshes this list too.
  const workflowsQ = useQuery<{ definitions?: PickerWorkflow[] }>({
    queryKey: pickerQueryKey(source, ['workflow-definitions']),
    queryFn: async () => (await loadWorkflows!()) ?? {},
    enabled: open && workflowMode,
    staleTime: WORKFLOWS_STALE_MS,
  })
  const workflowData = workflowsQ.data

  // The list on screen decides the loading, failed and retry states: the
  // commands while a command is typed, the saved workflows after `/workflow `.
  const listFetching = workflowMode ? workflowsQ.isFetching : isFetching
  const listError = workflowMode ? workflowsQ.isError : isError
  const listRefetch = workflowMode ? workflowsQ.refetch : refetch

  // Displayed order (bottom-up when the menu opens above); resultsRef mirrors it
  // so the keyboard-nav choose() indexes the same list the user sees.
  const [displayed, setDisplayed] = useState<Row[]>([])

  // A manual retry is in flight: set when the user presses Retry on the
  // load-failure notice, cleared when that re-ask settles. A dedicated flag
  // rather than a query internal, like the sibling pickers' own busy flags —
  // on a refetch after an error react-query flips back to its pending state, so
  // this is what keeps the notice with an inert "Retrying…" on screen for the
  // re-ask instead of the first-load "Loading commands…" copy. Cleared off
  // `isFetching` going false below.
  const [retrying, setRetrying] = useState(false)
  useEffect(() => { if (!listFetching) setRetrying(false) }, [listFetching])
  const retryLoad = useCallback(() => {
    if (listFetching) return
    setRetrying(true)
    void listRefetch()
  }, [listFetching, listRefetch])
  // The re-ask is in flight THIS render. `retrying` state clears one commit late
  // (its effect runs after the settle commit), so on a Retry that succeeds with
  // zero rows the failed role="alert" copy would show for one frame before
  // flipping to "No matching commands". Anding with
  // `isFetching`, false the moment the query settles, closes that frame.
  const retryInFlight = retrying && listFetching
  const resultsRef = useRef<Row[]>([])

  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const c = r[idx >= r.length ? 0 : idx]
    if (c) onSelect(rowInsert(c))
  }, [onSelect])

  // Filter computed synchronously (not inside the ordering effect) so the
  // keyboard-release gate below reads the SAME render's match set — a gate
  // derived from the `displayed` state would lag one effect flush behind and
  // could release Enter while matches exist.
  const filtered = useMemo<Row[]>(
    () => (!visible
      ? []
      : workflowMode
        // Sorted by slug, as the command rows are by name: the library lists
        // newest first, and Enter picks the first row, so `/workflow deploy`
        // must offer `deploy` before a newer `deploy-prod`. Plain code-point
        // order -- a slug is ASCII -- so the order is the same in every locale.
        ? (workflowData?.definitions ?? [])
            .filter(w => typeof w?.slug === 'string' && w.slug.startsWith(workflowFilter))
            .sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0))
            .map(workflow => ({ kind: 'workflow' as const, workflow }))
        : commands.filter(c => c.name.slice(1).startsWith(filter)).map(cmd => ({ kind: 'cmd' as const, cmd }))),
    [visible, workflowMode, workflowData, workflowFilter, filter, commands]
  )

  // Zero matches while `visible` is still true: the nav hook's document
  // listener stays attached, and an invisible surface must not swallow Enter
  // on unmatched slash input like "/xyz" (the #5029 trap, deferred to the
  // sibling pickers by #5041). Releasing (rather than auto-closing on empty)
  // keeps the composer's input-derived reopen path working when the user
  // backspaces to a matching prefix. `!isFetching` guards the one in-flight
  // window: a server-only command typed before the remote list replaces the
  // synchronous fallback is transiently a zero-match, and releasing there
  // would send the half-typed command as a chat message.
  // The workflow list holds the keys while it loads (Enter would otherwise send
  // the half-typed pick) and names a failed read with a Retry, as the command
  // list does. Only a list that loaded and holds no match closes the menu, so
  // `/workflow …` then sends as typed, exactly as before it had a menu.
  const releaseKeysWhenEmpty = !listFetching && filtered.length === 0
  const workflowsSettledEmpty = workflowMode && workflowsQ.isSuccess && !workflowsQ.isFetching && filtered.length === 0
  useEffect(() => {
    if (visible && workflowsSettledEmpty) onClose()
  }, [visible, workflowsSettledEmpty, onClose])

  // Uses the SAME nav hook as the $skill / @file pickers, which gives the slash
  // menu arrow-scroll and consistent Enter/Tab/Escape.
  const retryRef = useRef<HTMLButtonElement>(null)
  const { selected, setSelected, selectedRef, itemRefs, hoverSelect, restoreEmptyFocus } = useListKeyboardNav({
    open: visible,
    count: displayed.length,
    onChoose: choose,
    onClose,
    releaseKeysWhenEmpty,
    emptyActionRef: retryRef,
  })

  // Order + initial selection: bottom-up when the menu opens above the input
  // (shared helper — identical to the other pickers). Keyed on the memoized
  // `filtered` so unrelated re-renders (e.g. arrow-key selection changes)
  // don't reset the selection.
  useEffect(() => {
    if (!visible) { setDisplayed([]); resultsRef.current = []; return }
    const above = anchorRef.current ? menuGeometry(anchorRef.current, filtered.length, 40).above : false
    const { ordered, initialIndex } = bottomUpOrder(filtered, above)
    setDisplayed(ordered); resultsRef.current = ordered
    setSelected(initialIndex)
  }, [visible, filtered, anchorRef, setSelected])

  // Scroll the selected row into view once it renders (open + filter change),
  // matching the $skill / @file pickers.
  useEffect(() => {
    if (!visible) return
    itemRefs.current[selectedRef.current]?.scrollIntoView({ block: 'nearest' })
  }, [displayed, visible, itemRefs, selectedRef])

  if (!visible || !anchorRef.current) return null
  // Rows are ordered by an effect, so a matching list is briefly empty here.
  // Render nothing until it lands, whether or not a refetch is in flight.
  if (displayed.length === 0 && (filtered.length > 0 || workflowsSettledEmpty)) return null

  const { above, top, bottom, left, width, maxHeight } =
    menuGeometry(anchorRef.current, Math.max(displayed.length, 1), 40)

  /** Copy for the zero-row state. A settled ERROR is not a zero-match: both
   *  release Enter, but "No matching commands" asserts the live list was read
   *  and did not hold the typed prefix. On a failed load it was never read —
   *  the rows are the offline fallback, and the served list is provider-aware,
   *  so a command it would have offered may simply never have arrived. The
   *  failed copy is held through the re-ask too (`retryInFlight`): a Retry with
   *  no cached data resets the query to pending (`isError` false), and the text
   *  must not flip to "No matching commands" there.
   *  Anding `retrying` with `isFetching` also keeps it from lingering one frame
   *  past the settle commit. Named per the send binding on both paths. */
  const ctrl = sendOnEnter === 'ctrl-enter'
  // The workflow list has no "no matching" copy: a loaded list with no match
  // closes the menu (`workflowsSettledEmpty`) instead.
  const emptyKey = (listError || retryInFlight)
    // While Enter is still swallowed (the re-ask is in flight, or the list has
    // not settled), show the plain failed copy — not the "Enter sends" variant,
    // which would promise a key the composer is holding. Mirrors how
    // SkillPickerMenu builds its key.
    ? (workflowMode
        ? (!releaseKeysWhenEmpty
            ? 'components.slashCommandMenu.workflows_load_failed'
            : ctrl
              ? 'components.slashCommandMenu.workflows_load_failed_ctrl_enter_sends'
              : 'components.slashCommandMenu.workflows_load_failed_enter_sends')
        : (!releaseKeysWhenEmpty
            ? 'components.slashCommandMenu.commands_load_failed'
            : ctrl
              ? 'components.slashCommandMenu.commands_load_failed_ctrl_enter_sends'
              : 'components.slashCommandMenu.commands_load_failed_enter_sends'))
    : (ctrl
        ? 'components.slashCommandMenu.no_matching_commands_ctrl_enter_sends'
        : 'components.slashCommandMenu.no_matching_commands_enter_sends')

  // While the fetch is in flight the release gate is still closed, so the send
  // key is swallowed here — name that hold instead of leaving it mute.
  const loadingKey = workflowMode
    ? (ctrl
        ? 'components.slashCommandMenu.loading_workflows_ctrl_enter_held'
        : 'components.slashCommandMenu.loading_workflows_enter_held')
    : (ctrl
        ? 'components.slashCommandMenu.loading_commands_ctrl_enter_held'
        : 'components.slashCommandMenu.loading_commands_enter_held')

  /** The failed-read notice and its Retry. `asEmptyAction` is the zero-row
   *  state, where the keyboard-nav hook owns the Retry focus moves; above
   *  cached rows the Retry is reached by pointer and the rows keep the keys. */
  const failureNotice = (asEmptyAction: boolean) => (
    <div className="px-3 py-3 flex items-center gap-2" aria-busy={retryInFlight || undefined}>
      {/* No hand-off: this menu floats over the composer's unsaved draft. */}
      <ErrorNotice
        variant="inline"
        className="flex-1 min-w-0 whitespace-normal"
        message={i18nT(emptyKey)}
      />
      <button
        type="button"
        ref={asEmptyAction ? retryRef : undefined}
        onMouseDown={e => e.preventDefault()}
        onClick={() => { if (asEmptyAction) restoreEmptyFocus(); retryLoad() }}
        aria-disabled={retryInFlight || undefined}
        className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0 aria-disabled:opacity-50"
      >
        {retryInFlight ? i18nT('components.slashCommandMenu.retrying') : i18nT('components.slashCommandMenu.retry')}
      </button>
    </div>
  )

  return <OverlayPortal>
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ ...(above ? { bottom } : { top }), left, width: Math.min(width, 380), maxHeight }}
    >
      {displayed.length === 0
        // Settled zero-match: Enter's meaning flips (pick → send), and the
        // menu vanishing on its own would leave that flip invisible — announce
        // it at the point of action, mirroring the $skill picker's empty state.
        // Named per the composer's send binding ('ctrl-enter' → bare Enter is
        // a newline); role="status" so screen-reader users hear the flip too.
        ? ((listError || retryInFlight)
            // A failed load is an ERROR surface, so it renders through
            // ErrorNotice (errors-use-error-notice) — never a hand-written
            // status div — and gains the re-ask the four sibling pickers carry
            // (#7068). No hand-off: this floats over the composer's unsaved
            // draft. `refetch()` re-issues the SAME query; on success the menu
            // re-renders from the live list with no error, so the notice clears
            // itself. Only the load-FAILED (or retry-in-flight) empty state
            // offers it — a genuine "no matching commands" has nothing to
            // re-ask. This branch precedes the loading one so the re-ask shows
            // an inert "Retrying…", not the first-load copy (a refetch after an
            // error flips react-query back to pending). ErrorNotice is
            // role="alert", announcing the Enter-sends flip the copy carries.
            // Retry is inert-but-focusable while busy. Mousedown preserves
            // composer focus; Tab reaches Retry and Tab/Shift+Tab return to
            // the composer. Activation restores focus before refetch, and the
            // nav hook returns focus to the composer whenever the button
            // unmounts while holding it (a retry already in flight landing),
            // so focus never drops to <body>.
            ? failureNotice(true)
            : listFetching
            ? <div role="status" className="px-3 py-3 text-[12px] text-muted">{i18nT(loadingKey)}</div>
            : <div role="status" className="px-3 py-3 text-[12px] text-muted">{i18nT(emptyKey)}</div>)
        : <>
        {/* A refresh of the saved workflows can fail while the last list is
          * still cached: the rows stay pickable, and the failure is named above
          * them with its Retry. The command list needs no such row: its rows on
          * a failed read are the offline fallback list, by design. */}
        {workflowMode && (listError || retryInFlight) && failureNotice(false)}
        {displayed.map((row, i) => (
        <button
          role="option"
          aria-selected={i === selected}
          tabIndex={-1}
          key={rowKey(row)}
          ref={el => { itemRefs.current[i] = el }}
          className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
          onMouseMove={e => hoverSelect(i, e)}
          onMouseDown={e => { e.preventDefault(); onSelect(rowInsert(row)) }}
        >
          <span className="text-[13px] font-mono font-semibold text-accent shrink-0">{row.kind === 'cmd' ? row.cmd.name : row.workflow.slug}</span>
          <span className="text-[12px] truncate">{row.kind === 'cmd' ? commandDescription(row.cmd) : (row.workflow.description || row.workflow.name || '')}</span>
        </button>
        ))}
        </>}
    </div>
  </OverlayPortal>
}
