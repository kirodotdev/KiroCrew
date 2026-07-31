import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { Pencil, Circle, Pin, Zap, Locate, Link2, Tag as TagIcon, X, ExternalLink, Monitor, Undo2 } from 'lucide-react'
import type { ChatFolder } from '../types'
import FolderMoveSubmenu from './FolderMoveSubmenu'
import SessionColorSwatches from './SessionColorSwatches'
import LinkedSurfacesSection from './LinkedSurfacesSection'
import { DropdownMenuItem, DropdownMenuSeparator } from './ui/dropdown-menu'
import { ContextMenuItem, ContextMenuSeparator } from './ui/context-menu'
import { useAppSelector } from '../store'
import { useTagPopover } from '../hooks/useTagPopover'
import { api } from '../api/client'
import { useSessionActions } from '../hooks/useSessionActions'
import { useChatPopouts } from '../hooks/useChatPopouts'

import { i18nT } from '../i18n/t'
export interface SessionActionsMenuProps {
  /** Chooses the Radix primitive family; must match the enclosing menu. */
  variant: 'dropdown' | 'context'
  /**
   * The session this menu acts on. This is a *connected* component: every
   * store-derived fact (unread / pinned / folder / colour) and every generic
   * action (mark read/unread · pin · move · copy link · close) is keyed on this
   * slot and wired straight to the store internally. A surface therefore opts
   * into the full menu simply by rendering it with a `slotKey` — no wall of
   * handlers or data props to plumb.
   */
  slotKey: string
  /** Surface mode — forwarded to useSessionActions (scopes the copy-link URL). */
  mode?: string
  // ── The "absolutely necessary" bubble props: surface-specific UI-state or
  //    async ownership that genuinely can't (and shouldn't) be internalised. ──
  /** Header only: scrolls the sidebar to reveal this session. */
  onReveal?: () => void
  /** Rename entry point — differs per surface (sidebar inline row-edit vs header title editor). */
  onRename?: () => void
  /** Extra items rendered in the top "informational" group (header-only today:
   *  the MCP-servers submenu). Generic so the shared menu stays surface-agnostic. */
  infoSlots?: React.ReactNode[]
  /** Called after a colour pick; lets a caller that controls its own menu close it (the header does). */
  onColorPicked?: () => void
}

/**
 * Drop falsy items within each group, then drop groups that became empty.
 * This is what makes separators auto-collapse: the caller renders a divider
 * only *between* surviving groups, so an absent section never leaves a stray
 * divider behind. Exported (and generic) so the visibility logic can be
 * unit-tested with plain values, dodging jsdom's Radix-submenu flakiness.
 */
export function collapseGroups<T>(groups: (T | false | null | undefined)[][]): T[][] {
  return groups
    .map(g => g.filter((n): n is T => Boolean(n)))
    .filter(g => g.length > 0)
}

/**
 * One session menu, shared by all four surfaces — the sidebar row's mobile
 * dropdown, desktop dropdown, and right-click context menu, plus the session
 * header dropdown. Renders the *item list only* (not the Root/Trigger/Content
 * shell) in one canonical order; each caller keeps its own trigger + Content
 * wrapper (they differ in alignment, width, and open-state control).
 *
 * It connects to the store itself (useSessionActions + selectors keyed on
 * `slotKey`) and renders the colour row inline, so callers bubble in only the
 * surface-specific residue (rename/reveal + the MCP node slot + the
 * colour-pick close hook). Connected surfaces are themselves a connected
 * sub-section (keyed on `slotKey`), so they render on every surface, not just
 * the header. The generic actions read their live state at call time, so the
 * labels never drift from what the handlers do.
 *
 * Canonical order, five groups (each renders only if it has surviving items,
 * with dividers auto-collapsing between them):
 *   [informational]  MCP servers ▸  (header only)
 *   [tab modifiers]  Rename · Mark read/unread · Pin · Switch to Autopilot/Chat · Move to folder ▸ · Tags…
 *   [nav / access]   Reveal in sidebar (header only) · Copy link · Connected surfaces
 *   [colour]         colour swatches
 *   [close]          Close session
 */
export default function SessionActionsMenu({
  variant, slotKey, mode, onReveal, onRename, infoSlots, onColorPicked,
}: SessionActionsMenuProps) {
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const Separator = variant === 'context' ? ContextMenuSeparator : DropdownMenuSeparator

  // Generic, surface-agnostic actions — one definition, wired straight to the store.
  const { toggleRead, togglePin, toggleMode, copyLink, move, close } = useSessionActions(mode)
  // Popped-out window coordination (shared singleton — one channel for all menus).
  const { isPoppedOut, isSelfPopout, open: openPopout, focus: focusPopout, bringBack, returnSelfToMain } = useChatPopouts()
  // This menu also renders INSIDE a popout window (via the header). There the
  // map never contains the window's own slot (no channel self-delivery), so we
  // must key off isSelfPopout: offering "Pop out" would window.open into the
  // popout's own window name and reload it in place.
  const selfPopout = isSelfPopout(slotKey)
  const poppedOut = !selfPopout && isPoppedOut(slotKey)
  const { open: openTagPopover } = useTagPopover()

  // Store-derived per-slot state: the canonical live source, matching exactly
  // what the action handlers read at call time (so a label never drifts from
  // its behaviour). `unread` comes from dashboard.unreadSlots — the same source
  // toggleRead reads — and pin/folder/colour from the slot itself.
  const isUnread = useAppSelector(s => s.dashboard.unreadSlots.includes(slotKey))
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  const isPinned = !!slot?.pinned
  const currentFolderId = slot?.folder_id
  const colorIndex = slot?.color_index

  // Folders drive the Move submenu. A menu's Content only mounts while it's open
  // (Radix), so this keyed query effectively runs only while a menu is open and
  // dedupes against the sidebar's own ['chat-folders'] cache — no extra fetch.
  const { data: folders = [] } = useQuery<ChatFolder[]>({ queryKey: ['chat-folders'], queryFn: () => api.chatFolders() })

  const groups = collapseGroups<React.ReactNode>([
    // Informational (header only) — generic slots injected by the caller.
    infoSlots ?? [],
    // Modifiers to the tab itself
    [
      onRename && (
        <Item key="rename" onSelect={onRename}>
          <Pencil size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.rename')}
        </Item>
      ),
      <Item key="read" onSelect={() => toggleRead(slotKey)}>
        <Circle size={13} className="shrink-0 text-muted" /> {isUnread ? i18nT('components.sessionActionsMenu.mark_as_read') : i18nT('components.sessionActionsMenu.mark_as_unread')}
      </Item>,
      <Item key="pin" onSelect={() => togglePin(slotKey)}>
        <Pin size={13} className="shrink-0 text-muted" /> {isPinned ? i18nT('components.sessionActionsMenu.unpin') : i18nT('components.sessionActionsMenu.pin')}
      </Item>,
      <Item key="mode" onSelect={() => toggleMode(slotKey)}>
        <Zap size={13} className="shrink-0 text-muted" /> {slot?.mode === 'orchestrator' ? i18nT('components.sessionActionsMenu.switch_to_chat') : i18nT('components.sessionActionsMenu.switch_to_autopilot')}
      </Item>,
      folders.length > 0 && (
        <FolderMoveSubmenu
          key="move"
          variant={variant}
          folders={folders}
          currentFolderId={currentFolderId}
          onPick={(folderId) => move(slotKey, folderId)}
          label={i18nT('components.sessionActionsMenu.move_to_folder')}
        />
      ),
      <Item key="tags" onSelect={() => openTagPopover(slotKey)}>
        <TagIcon size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.tags')}
      </Item>,
    ],
    // Navigation / access
    [
      onReveal && (
        <Item key="reveal" onSelect={onReveal}>
          <Locate size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.reveal_in_sidebar')}
        </Item>
      ),
      // Pop out to a dedicated browser window — or, if already out, focus /
      // bring it back. Lets you keep typing to one session while looking at an
      // artifact or another view in the main window. Inside the popout window
      // itself, the only meaningful action is returning to the main dashboard.
      selfPopout ? (
        <Item key="bring-back-self" onSelect={returnSelfToMain}>
          <Undo2 size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.bring_back_to_main')}
        </Item>
      ) : poppedOut ? (
        <Item key="focus-popout" onSelect={() => focusPopout(slotKey)}>
          <Monitor size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.focus_popped_out_window')}
        </Item>
      ) : (
        <Item key="popout" onSelect={() => openPopout(slotKey, slot?.title)}>
          <ExternalLink size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.pop_out_to_window')}
        </Item>
      ),
      poppedOut && (
        <Item key="bring-back" onSelect={() => bringBack(slotKey)}>
          <Undo2 size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.bring_back_to_main')}
        </Item>
      ),
      <Item key="copy" onSelect={() => copyLink(slotKey)}>
        <Link2 size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.copy_link')}
      </Item>,
      // Channel-neutral link state and actions — connected origins are read-only,
      // explicit mirrors can be reminded/stopped, and an otherwise-unlinked
      // dashboard session retains the existing Slack channel picker.
      <LinkedSurfacesSection key="links" slotKey={slotKey} variant={variant} />,
    ],
    // Colour — its own section
    [
      <SessionColorSwatches key="color" slotKey={slotKey} colorIndex={colorIndex} onPicked={onColorPicked} />,
    ],
    // Close session — terminal, destructive
    [
      <Item key="close" className="text-danger focus:text-danger" onSelect={() => close(slotKey)}>
        <X size={13} /> {i18nT('components.sessionActionsMenu.close_session')}
      </Item>,
    ],
  ])

  return (
    <>
      {groups.map((group, i) => (
        <React.Fragment key={i}>
          {i > 0 && <Separator />}
          {group}
        </React.Fragment>
      ))}
    </>
  )
}
