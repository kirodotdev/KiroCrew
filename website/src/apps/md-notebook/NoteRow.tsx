/** Rows for the notes panel: one note, one folder, and the tree renderer. */
import { Fragment, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Folder as FolderIcon, FolderOpen } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { ACCENT, ACCENT_BG, RAIL_X } from './constants'
import Clickable from '../../components/Clickable'
import { relTime } from './utils'
import type { Note, TreeNode } from './types'
import { compareText } from '../../i18n/format'

/** Sync badge, matching the Sessions list tag-chip recipe. */
function badgeStyle(status: string): CSSProperties {
  const map: Record<string, CSSProperties> = {
    pending: {
      background: 'var(--warn-subtle)',
      color: 'var(--warn)',
      borderColor: 'var(--warn)',
    },
    conflict: {
      background: 'var(--danger-subtle)',
      color: 'var(--danger)',
      borderColor: 'var(--danger)',
    },
    synced: { background: 'var(--card)', color: 'var(--muted)', borderColor: 'var(--border)' },
  }
  return {
    ...(map[status] ?? map.synced),
    padding: '1px 6px',
    borderRadius: '4px',
    fontSize: '10px',
    fontWeight: 500,
    lineHeight: 1,
    border: '1px solid',
    display: 'inline-flex',
    alignItems: 'center',
  }
}

export function NoteRow({
  note,
  active,
  onOpen,
  showFolder,
}: {
  note: Note
  active: boolean
  onOpen: (path: string) => void
  showFolder?: boolean
}) {
  // In flat-list view the folder tree is gone, so surface the note's parent
  // folder in the meta line to disambiguate same-named notes.
  const folder =
    showFolder && note.path.includes('/') ? note.path.split('/').slice(0, -1).pop() : null
  return (
    <Clickable
      className="mdnb-row"
      aria-label={note.title}
      onClick={() => onOpen(note.path)}
      // Drag a note onto a folder row (or the list background) to file it.
      draggable
      onDragStart={e => {
        e.dataTransfer.setData('text/plain', note.path)
        e.dataTransfer.effectAllowed = 'move'
      }}
      style={{
        padding: '8px 16px',
        borderRadius: '8px',
        cursor: 'pointer',
        ...(active ? { background: ACCENT_BG } : null),
      }}
    >
      <div
        style={{
          fontSize: '13px',
          fontWeight: 600,
          lineHeight: 1.375,
          color: 'var(--text)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {note.title}
      </div>
      <div
        style={{
          display: 'flex',
          gap: '6px',
          alignItems: 'center',
          marginTop: '2px',
          minWidth: 0,
        }}
      >
        {folder && (
          <>
            <span
              title={note.path}
              style={{
                fontSize: '11px',
                fontWeight: 400,
                color: 'var(--muted)',
                maxWidth: '96px',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                flexShrink: 0,
              }}
            >
              {folder}
            </span>
            <span style={{ fontSize: '11px', color: 'var(--muted)', flexShrink: 0 }}>·</span>
          </>
        )}
        <span
          style={{ fontSize: '11px', fontWeight: 400, color: 'var(--muted)', flexShrink: 0 }}
        >
          {relTime(note.modifiedAt)}
        </span>
        {note.syncStatus !== 'synced' && (
          <span style={badgeStyle(note.syncStatus)}>
            {i18nT('apps.mdNotebook.badge.pending')}
          </span>
        )}
      </div>
    </Clickable>
  )
}

function countNotes(node: TreeNode): number {
  let n = node.notes.length
  for (const [, child] of node.folders) n += countNotes(child)
  return n
}

export interface TreeProps {
  activePath: string | null
  onOpen: (path: string) => void
  collapsed: Set<string>
  toggle: (name: string) => void
  cmp: (a: Note, b: Note) => number
  onMove: (from: string, folder: string) => void
}

function FolderRow({
  name,
  node,
  depth,
  ...rest
}: TreeProps & { name: string; node: TreeNode; depth: number }) {
  const isCollapsed = rest.collapsed.has(name)
  const [dropping, setDropping] = useState(false)
  const Glyph = isCollapsed ? FolderIcon : FolderOpen
  return (
    <Fragment>
      <Clickable
        className="mdnb-row"
        aria-label={name.split('/').pop()}
        onClick={() => rest.toggle(name)}
        // Drop target: filing a dragged note into this folder.
        onDragOver={e => {
          e.preventDefault()
          e.stopPropagation()
          e.dataTransfer.dropEffect = 'move'
          setDropping(true)
        }}
        onDragLeave={() => setDropping(false)}
        onDrop={e => {
          e.preventDefault()
          e.stopPropagation()
          setDropping(false)
          const from = e.dataTransfer.getData('text/plain')
          if (from) rest.onMove(from, name)
        }}
        style={{
          display: 'flex',
          gap: '8px',
          alignItems: 'center',
          padding: '4px 8px',
          borderRadius: '8px',
          cursor: 'pointer',
          fontSize: '12px',
          fontWeight: 400,
          color: dropping ? ACCENT : 'var(--muted)',
          marginLeft: depth * 10,
          ...(dropping ? { background: ACCENT_BG, outline: `1px solid ${ACCENT}` } : null),
        }}
      >
        {/* The glyph carries the open/closed state itself — no rotation, so a
            click produces no transform. */}
        <span
          style={{ display: 'flex', alignItems: 'center', flexShrink: 0, color: 'inherit' }}
        >
          <Glyph size={14} />
        </span>
        {name.split('/').pop()}
        <span style={{ marginLeft: 'auto', fontSize: '10px', color: 'inherit' }}>
          {countNotes(node)}
        </span>
      </Clickable>
      {!isCollapsed && (
        // Nesting rail: one continuous line at this folder's glyph centre,
        // spanning its children. RAIL_X is the same offset the rendered-note
        // rails use, so the two surfaces line up conceptually. Each nested
        // folder draws its own, which is what produces one line per level.
        <div style={{ marginLeft: depth * 10 + 8, position: 'relative' }}>
          <div
            aria-hidden
            style={{
              position: 'absolute',
              left: `${RAIL_X}px`,
              top: 0,
              bottom: 0,
              width: '1px',
              background: 'var(--border)',
            }}
          />
          {renderTree(node, depth + 1, name, rest)}
        </div>
      )}
    </Fragment>
  )
}

/** Render a folder tree. Folders stay alphabetical; the sort applies to notes. */
export function renderTree(
  node: TreeNode,
  depth: number,
  prefix: string,
  props: TreeProps,
): ReactNode[] {
  const items: ReactNode[] = []
  for (const [name, child] of [...node.folders].sort((a, b) => compareText(a[0], b[0]))) {
    const full = prefix ? `${prefix}/${name}` : name
    items.push(
      <FolderRow key={full} name={full} node={child} depth={depth} {...props} />,
    )
  }
  for (const n of [...node.notes].sort(props.cmp)) {
    items.push(
      <NoteRow
        key={n.path}
        note={n}
        active={n.path === props.activePath}
        onOpen={props.onOpen}
      />,
    )
  }
  return items
}
