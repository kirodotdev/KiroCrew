import type React from 'react'
import { Folder, Check, ChevronRight } from 'lucide-react'
import type { ChatFolder } from '../types'
import { orderFoldersWithPaths } from '../utils/folderTree'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent, ContextMenuItem,
} from './ui/context-menu'

interface FolderMoveSubmenuProps {
  readonly folders: readonly ChatFolder[]
  /** Invoked with the chosen folder id, or null for the root entry. */
  readonly onPick: (folderId: string | null) => void
  /** When set, the matching entry shows a checkmark. null/'' marks the root entry. */
  readonly currentFolderId?: string | null
  /** Which menu family this submenu nests inside — chooses Radix Dropdown vs Context primitives. */
  readonly variant: 'dropdown' | 'context'
  /** Trigger label (defaults to "Move to folder"). */
  readonly label?: string
  /** Label for the "no folder (root)" entry. */
  readonly rootLabel?: string
}

/**
 * "Move to folder" as a native Radix submenu, shared by the sidebar session
 * menus (dropdown + right-click context) and the session-header dropdown so all
 * three behave identically. Rendering through the real Radix Sub primitives
 * (rather than a bespoke anchored popover) gives roving-focus keyboard nav,
 * collision handling, and focus management for free.
 *
 * Folders render in pre-order tree sequence (orderFoldersWithPaths), each row
 * indented by depth (12 + depth*16 px, matching the sidebar folder tree). The
 * full ancestry path is the row's title (hover tooltip + accessible name) so
 * same-named subfolders under different parents stay unambiguous.
 *
 * `variant` selects the primitive family: a submenu must use the same menu
 * family as its parent (a DropdownMenuSub can only live inside a DropdownMenu,
 * a ContextMenuSub inside a ContextMenu).
 */
/**
 * The picker's item list, shared between the nested submenu below and
 * root-level menu surfaces (e.g. the artifact detail page's folder chip,
 *). `Item` is the Radix menu-item primitive of the hosting menu
 * family — items must match their parent menu's family.
 */
export function FolderPickerItems({ folders, onPick, currentFolderId, rootLabel = 'No folder (root)', Item }: {
  readonly folders: readonly ChatFolder[]
  readonly onPick: (folderId: string | null) => void
  readonly currentFolderId?: string | null
  readonly rootLabel?: string
  readonly Item: React.ComponentType<{ title?: string; style?: React.CSSProperties; onSelect?: (event: Event) => void; children?: React.ReactNode }>
}) {
  const atRoot = currentFolderId == null || currentFolderId === ''
  const ordered = orderFoldersWithPaths(folders)
  return (
    <>
      <Item title={rootLabel} onSelect={() => onPick(null)}>
        <Folder size={13} className="text-muted shrink-0" />
        <span className="truncate">{rootLabel}</span>
        {atRoot && <Check size={13} className="ml-auto text-accent shrink-0" />}
      </Item>
      {ordered.map(({ folder: f, depth, path }) => (
        <Item key={f.id} title={path} style={depth > 0 ? { paddingLeft: `${12 + depth * 16}px` } : undefined} onSelect={() => onPick(f.id)}>
          <Folder size={13} className="text-accent shrink-0" />
          <span className="truncate">{f.name}</span>
          {currentFolderId === f.id && <Check size={13} className="ml-auto text-accent shrink-0" />}
        </Item>
      ))}
    </>
  )
}

export default function FolderMoveSubmenu({
  folders,
  onPick,
  currentFolderId,
  variant,
  label = 'Move to folder',
  rootLabel = 'No folder (root)',
}: FolderMoveSubmenuProps) {
  // Pick the primitive family for this surface. Both families share the same
  // props shape, so the body below is identical regardless of variant.
  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem

  return (
    <Sub>
      <SubTrigger>
        <Folder size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{label}</span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[170px] max-h-[280px] overflow-y-auto">
        <FolderPickerItems folders={folders} onPick={onPick} currentFolderId={currentFolderId} rootLabel={rootLabel} Item={Item} />
      </SubContent>
    </Sub>
  )
}
