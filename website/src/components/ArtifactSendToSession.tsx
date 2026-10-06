import { useMemo, useState } from 'react'
import { Forward, Plus } from 'lucide-react'

import { useAppDispatch, useAppSelector } from '../store'
import { createSlot } from '../store/chatSlice'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from './ui/dropdown-menu'
import HoverTip from './HoverTip'
import { prepareCurrentSlots, isEmptyNewSlot } from './commandPalette/providers/recentsProvider'
import { loadDrafts, mergeIntoDraft } from '../utils/chatDrafts'
import type { NavIntent } from '../utils/popoutController'
import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'

/** How many live sessions the menu lists. The menu is a quick hand-off, not a
 *  session browser — the sidebar and the command palette own the long tail. */
const MAX_LISTED_SESSIONS = 10

/**
 * The text a hand-off seeds the target composer with. Addressed to the agent,
 * so it names the slug (the handle `artifact_get` takes) rather than relying on
 * the title, which is neither unique nor stable. Kept in English like the
 * page's other agent-addressed prompts: the agent reads it, and the user can
 * edit it before sending.
 */
export function artifactReferencePrompt(name: string, slug: string): string {
  return `Reference artifact "${name}" (slug \`${slug}\`; load it with artifact_get).`
}

/**
 * Toolbar control on the artifact page: drop a reference to this artifact into
 * a chat session's composer — an existing live session or a new one — so the
 * agent there can load it. The composer is only PRE-FILLED (the user still
 * presses send), and the reference is appended to that session's stored draft
 * rather than replacing it: the prefill consumer overwrites the composer, so an
 * unmerged seed would silently destroy text the user had typed there.
 */
export function ArtifactSendToSession({ name, slug, onSend, className }: {
  name: string
  slug: string
  /** The page's navigation dispatcher, so a popout window forwards the
   *  hand-off to the main dashboard instead of navigating in place. */
  onSend: (intent: NavIntent) => void
  className?: string
}) {
  const dispatch = useAppDispatch()
  const slots = useAppSelector((s) => s.dashboard.slots)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState(false)

  // Live sessions, most recent first. Sessions bound to an artifact are that
  // artifact's companion chat (this page's own lives behind the chat toggle),
  // and an empty placeholder is what "New session" already creates.
  const listed = useMemo(() => {
    const candidates = (slots ?? []).filter((s: ChatSlot) => !s.artifact && !isEmptyNewSlot(s))
    return prepareCurrentSlots(candidates).ordered.slice(0, MAX_LISTED_SESSIONS)
  }, [slots])

  const prompt = artifactReferencePrompt(name, slug)
  const handOff = (slotKey: string) => {
    const merged = mergeIntoDraft(loadDrafts()[slotKey], prompt)
    onSend({ path: '/chat', slotKey, prefill: { slotKey, prompt: merged } })
  }
  const handOffToNew = async () => {
    setCreating(true)
    setError(false)
    try {
      const slot = await dispatch(createSlot({ activate: false })).unwrap()
      handOff(slot.key)
    } catch {
      setError(true)
    } finally {
      setCreating(false)
    }
  }

  const label = error
    ? i18nT('pages.artifactDetailPage.send_to_session_failed')
    : i18nT('pages.artifactDetailPage.send_to_session')
  return (
    <DropdownMenu>
      <HoverTip label={label}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            disabled={creating}
            className={className ?? `p-1.5 rounded-md border border-border cursor-pointer transition-all disabled:opacity-40 ${error ? 'text-danger hover:text-danger' : 'text-muted hover:text-text hover:border-border-strong'}`}
            aria-label={label}
          >
            <Forward size={13} />
          </button>
        </DropdownMenuTrigger>
      </HoverTip>
      <DropdownMenuContent align="end" className="min-w-[220px] max-w-[320px] max-h-[min(360px,var(--radix-dropdown-menu-content-available-height))]">
        <DropdownMenuItem onSelect={() => { void handOffToNew() }}>
          <Plus size={13} aria-hidden="true" />
          {i18nT('pages.artifactDetailPage.send_to_new_session')}
        </DropdownMenuItem>
        {listed.length > 0 && <DropdownMenuSeparator />}
        {listed.map((s) => (
          <DropdownMenuItem key={s.key} onSelect={() => handOff(s.key)}>
            <span className="truncate">{s.title || i18nT('pages.artifactDetailPage.untitled_session')}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
