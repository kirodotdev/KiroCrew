import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'

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
// Kept in sync with the backend SLASH_COMMAND_DESCRIPTIONS map so the same
// commands appear whether they came from the live API or this fallback; the
// descriptions themselves come from COMMAND_DESC_KEY either way. /kb is a
// frontend-only command (also merged via FRONTEND_COMMANDS below).
const FALLBACK_COMMAND_NAMES = [
  '/agent', '/changelog', '/chat', '/clear', '/code', '/compact', '/context',
  '/editor', '/exit', '/experiment', '/help', '/hooks', '/issue', '/kb',
  '/logdump', '/mcp', '/model', '/paste', '/prompts', '/q', '/quit', '/reply',
  '/side', '/tangent', '/todos', '/tools', '/usage',
] as const

const FALLBACK_COMMANDS: SlashCommand[] = FALLBACK_COMMAND_NAMES.map(name => ({ name }))

interface Props {
  input: string
  anchorRef: React.RefObject<HTMLElement | null>
  onSelect: (command: string) => void
  onClose: () => void
  open?: boolean
}

const FRONTEND_COMMAND_NAMES = ['/kb', '/onboarding'] as const

const FRONTEND_COMMANDS: SlashCommand[] = FRONTEND_COMMAND_NAMES.map(name => ({ name }))

export default function SlashCommandMenu({ input, anchorRef, onSelect, onClose, open = true }: Props) {
  const { data: apiCommands = FALLBACK_COMMANDS } = useQuery<SlashCommand[]>({
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

  // Consolidated onto the SAME nav hook as the $skill / @file pickers — this is
  // what gives the slash menu arrow-scroll and consistent Enter/Tab/Escape (it
  // previously rolled its own selection state with no scroll-into-view).
  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open: visible,
    count: displayed.length,
    onChoose: choose,
    onClose,
  })

  // Order + initial selection: bottom-up when the menu opens above the input
  // (shared helper — identical to the other pickers). Filter is computed INSIDE
  // the effect and keyed on primitives (visible/filter/commands) so unrelated
  // re-renders (e.g. arrow-key selection changes) don't reset the selection.
  useEffect(() => {
    if (!visible) { setDisplayed([]); resultsRef.current = []; return }
    const f = commands.filter(c => c.name.slice(1).startsWith(filter))
    const above = anchorRef.current ? menuGeometry(anchorRef.current, f.length, 40).above : false
    const { ordered, initialIndex } = bottomUpOrder(f, above)
    setDisplayed(ordered); resultsRef.current = ordered
    setSelected(initialIndex)
  }, [visible, filter, commands, anchorRef, setSelected])

  // Scroll the selected row into view once it renders (open + filter change),
  // matching the $skill / @file pickers.
  useEffect(() => {
    if (!visible) return
    itemRefs.current[selectedRef.current]?.scrollIntoView({ block: 'nearest' })
  }, [displayed, visible, itemRefs, selectedRef])

  if (!visible || displayed.length === 0 || !anchorRef.current) return null

  const { top, left, width, maxHeight } = menuGeometry(anchorRef.current, displayed.length, 40)

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left, width: Math.min(width, 380), maxHeight }}
    >
      {displayed.map((cmd, i) => (
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
