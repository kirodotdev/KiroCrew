import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'

interface SlashCommand {
  name: string
  description: string
}

// Offline fallback shown before the API query resolves (or if it fails).
// Kept in sync with the backend SLASH_COMMAND_DESCRIPTIONS map so the same
// text renders whether it came from the live API or this fallback. /kb is a
// frontend-only command (also merged via FRONTEND_COMMANDS below).
const FALLBACK_COMMANDS: SlashCommand[] = [
  { name: '/agent', description: 'Switch or manage the active agent' },
  { name: '/changelog', description: 'Show the release changelog' },
  { name: '/chat', description: 'Save or load a chat session' },
  { name: '/clear', description: 'Clear conversation history' },
  { name: '/code', description: 'Open code intelligence tools' },
  { name: '/compact', description: 'Compact conversation to free context' },
  { name: '/context', description: 'Manage context files and token usage' },
  { name: '/editor', description: 'Compose your prompt in an external editor' },
  { name: '/exit', description: 'Exit the chat session' },
  { name: '/experiment', description: 'Toggle experimental features' },
  { name: '/help', description: 'Show available commands' },
  { name: '/hooks', description: 'View configured context hooks' },
  { name: '/issue', description: 'Report an issue or bug' },
  { name: '/kb', description: 'Search knowledge library' },
  { name: '/logdump', description: 'Dump session logs to a file' },
  { name: '/mcp', description: 'Show configured MCP servers' },
  { name: '/model', description: 'Show or switch the current model' },
  { name: '/paste', description: 'Paste an image from the clipboard' },
  { name: '/prompts', description: 'List or invoke saved prompts & agent SOPs' },
  { name: '/q', description: 'Quit the chat session' },
  { name: '/quit', description: 'Quit the chat session' },
  { name: '/reply', description: 'Reply to the last assistant message' },
  { name: '/side', description: 'Open a side conversation panel' },
  { name: '/tangent', description: 'Start a tangent conversation' },
  { name: '/todos', description: 'Show or manage the task list' },
  { name: '/tools', description: 'Show available tools' },
  { name: '/usage', description: 'Show billing and usage information' },
]

interface Props {
  input: string
  anchorRef: React.RefObject<HTMLElement | null>
  onSelect: (command: string) => void
  onClose: () => void
  open?: boolean
}

const FRONTEND_COMMANDS: SlashCommand[] = [
  { name: '/kb', description: 'Search knowledge library' },
  { name: '/onboarding', description: 'Replay setup import and the welcome tour' },
]

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

  // Uses the SAME nav hook as the $skill / @file pickers, which gives the slash
  // menu arrow-scroll and consistent Enter/Tab/Escape.
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
          <span className="text-[12px] truncate">{cmd.description}</span>
        </button>
      ))}
    </div>,
    document.body
  )
}
