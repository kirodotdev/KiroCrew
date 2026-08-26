import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'
import type { SendMode } from '../pages/chat/ChatSettings'

interface SlashCommand {
  name: string
  /**
   * Description as the SERVER sent it (English). Only a fallback for display —
   * {@link commandDescription} prefers the catalog. Optional because the local
   * command lists below carry no copy of their own; the backend itself also
   * sends `""` for a command missing from its description map.
   */
  description?: string
}

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
// terminal-only kiro-cli gestures the dashboard rejects, so suggesting them
// anywhere is an inert affordance; the descriptions themselves come from
// COMMAND_DESC_KEY either way. /kb is a frontend-only command (also merged
// via FRONTEND_COMMANDS below).
const FALLBACK_COMMAND_NAMES = [
  '/agent', '/changelog', '/clear', '/code', '/compact', '/context',
  '/experiment', '/help', '/hooks', '/issue', '/kb', '/logdump',
  '/mcp', '/model', '/prompts', '/side', '/todos', '/tools', '/usage',
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
}

/**
 * Commands the backend's GET /api/slash-commands does not report, merged in so
 * the menu still offers them. Two different kinds live here, and the difference
 * matters when adding a row:
 *
 * - CLIENT-INTERCEPTED (`/kb`, `/onboarding`): the composer recognises the text
 *   and acts on it locally; the message is never sent. Those also need a branch
 *   in `interceptSlashCommand`.
 * - QUICK PROMPT (`/plain`): a backend MACRO. The message IS sent, unchanged, and
 *   `ContextBuilder.build_message` swaps the token for the instruction it stands
 *   for (`src/kiro_crew/quick_prompts.py`). It must therefore stay OUT of
 *   `interceptSlashCommand` — intercepting it would stop it ever reaching the
 *   expansion — and out of the kiro-cli passthrough set, which would forward it
 *   to a harness that has no such command.
 */
const FRONTEND_COMMAND_NAMES = ['/kb', '/onboarding', '/plain'] as const

const FRONTEND_COMMANDS: SlashCommand[] = FRONTEND_COMMAND_NAMES.map(name => ({ name }))

export default function SlashCommandMenu({ input, anchorRef, onSelect, onClose, open = true, sendOnEnter = 'enter' }: Props) {
  const { data: apiCommands = FALLBACK_COMMANDS, isFetching } = useQuery<SlashCommand[]>({
    queryKey: ['slash-commands'],
    queryFn: () => api.slashCommands(),
    enabled: typeof api.slashCommands === 'function',
  })
  const commands = useMemo(() => {
    const names = new Set(apiCommands.map(c => c.name))
    return [...apiCommands, ...FRONTEND_COMMANDS.filter(c => !names.has(c.name))].sort((a, b) => a.name.localeCompare(b.name))
  }, [apiCommands])

  const match = input.match(/^\/([a-z]*)$/)
  const visible = open && !!match
  const filter = match?.[1] ?? ''

  // Displayed order (bottom-up when the menu opens above); resultsRef mirrors it
  // so the keyboard-nav choose() indexes the same list the user sees.
  const [displayed, setDisplayed] = useState<SlashCommand[]>([])
  const resultsRef = useRef<SlashCommand[]>([])

  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const c = r[idx >= r.length ? 0 : idx]
    if (c) onSelect(c.name + ' ')
  }, [onSelect])

  // Filter computed synchronously (not inside the ordering effect) so the
  // keyboard-release gate below reads the SAME render's match set — a gate
  // derived from the `displayed` state would lag one effect flush behind and
  // could release Enter while matches exist.
  const filtered = useMemo(
    () => (visible ? commands.filter(c => c.name.slice(1).startsWith(filter)) : []),
    [visible, filter, commands]
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
  const releaseKeysWhenEmpty = !isFetching && filtered.length === 0

  // Uses the SAME nav hook as the $skill / @file pickers, which gives the slash
  // menu arrow-scroll and consistent Enter/Tab/Escape.
  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open: visible,
    count: displayed.length,
    onChoose: choose,
    onClose,
    releaseKeysWhenEmpty,
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
  // Unsettled zero-match (remote list still in flight): keep rendering
  // nothing — the release gate is off and the row set is not yet decided.
  if (displayed.length === 0 && !releaseKeysWhenEmpty) return null

  const { top, left, width, maxHeight } = menuGeometry(anchorRef.current, Math.max(displayed.length, 1), 40)

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left, width: Math.min(width, 380), maxHeight }}
    >
      {displayed.length === 0
        // Settled zero-match: Enter's meaning flips (pick → send), and the
        // menu vanishing on its own would leave that flip invisible — announce
        // it at the point of action, mirroring the $skill picker's empty state.
        // Named per the composer's send binding ('ctrl-enter' → bare Enter is
        // a newline); role="status" so screen-reader users hear the flip too.
        ? <div role="status" className="px-3 py-3 text-[12px] text-muted">{i18nT(sendOnEnter === 'ctrl-enter' ? 'components.slashCommandMenu.no_matching_commands_ctrl_enter_sends' : 'components.slashCommandMenu.no_matching_commands_enter_sends')}</div>
        : displayed.map((cmd, i) => (
        <button
          role="option"
          aria-selected={i === selected}
          tabIndex={-1}
          key={cmd.name}
          ref={el => { itemRefs.current[i] = el }}
          className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
          onMouseEnter={() => setSelected(i)}
          onMouseDown={e => { e.preventDefault(); onSelect(cmd.name + ' ') }}
        >
          <span className="text-[13px] font-mono font-semibold text-accent shrink-0">{cmd.name}</span>
          <span className="text-[12px] truncate">{commandDescription(cmd)}</span>
        </button>
      ))}
    </div>,
    document.body
  )
}
