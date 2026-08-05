/** Rows for the notes panel: one note, one folder, and the tree renderer. */
import { Fragment, useEffect, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import {
  Copy,
  Folder as FolderIcon,
  FolderOpen,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { ACCENT, ACCENT_BG, FONT_BODY, RAIL_X } from './constants'
import Clickable from '../../components/Clickable'
import { relTime, rowBadge } from './utils'
import type { Note, NoteActions, TreeNode } from './types'
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

/** One button in the hover action bar. */
const actionBtn: CSSProperties = {
  width: '22px',
  height: '22px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 0,
  borderRadius: '4px',
  border: 'none',
  background: 'transparent',
  color: 'var(--muted)',
  cursor: 'pointer',
  flexShrink: 0,
}

export function NoteRow({
  note,
  active,
  onOpen,
  showFolder,
  showSyncBadge = true,
  actions,
}: {
  note: Note
  active: boolean
  onOpen: (path: string) => void
  showFolder?: boolean
  /**
   * False for a vault with no git remote. `pending` reports "differs from the
   * last commit", which on such a vault is not a state the user can act on —
   * there is nowhere for the note to be pending TO, and it clears itself on the
   * next autosave. Left visible it reads as "not saved", which is the opposite
   * of the truth: the badge only appears once the file has reached disk.
   */
  showSyncBadge?: boolean
  /** Omitted in contexts with no row affordances (e.g. a preview list). */
  actions?: NoteActions
}) {
  // In flat-list view the folder tree is gone, so surface the note's parent
  // folder in the meta line to disambiguate same-named notes.
  const folder =
    showFolder && note.path.includes('/') ? note.path.split('/').slice(0, -1).pop() : null
  const pinned = !!actions?.isPinned(note.path)
  const renaming = actions?.renamingPath === note.path
  // A delete is in flight for this row: dim it and swap the badge, so the note
  // reads as on its way out instead of looking untouched during the round trip.
  const deleting = actions?.deletingPath === note.path
  const badge = rowBadge({ deleting, syncStatus: note.syncStatus, showSyncBadge })
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  // The row's own folder — where a note dropped ON this row should land, so a
  // drop inside a folder files it into that folder instead of falling through to
  // the list background (which files at the vault root).
  const ownFolder = note.path.includes('/') ? note.path.slice(0, note.path.lastIndexOf('/')) : ''

  // Seed and focus the rename field when this row enters rename mode. Seeded
  // from the title (what the user sees), not the filename, so an edit does not
  // silently rewrite a frontmatter-titled note's name to something unrelated.
  useEffect(() => {
    if (!renaming) return
    setDraft(note.title)
    const el = inputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [renaming, note.title])

  const commitRename = () => {
    if (!actions) return
    const next = draft.trim()
    actions.onRenameEnd()
    if (next && next !== note.title) actions.onRename(note.path, next)
  }

  return (
    <Clickable
      className="mdnb-row"
      aria-label={note.title}
      disabled={deleting}
      onClick={() => {
        if (!renaming && !deleting) onOpen(note.path)
      }}
      // Drag a note onto a folder row, another note, or the list background to
      // file it. Dragging is suppressed while renaming so a text selection
      // inside the field is not read as the start of a drag.
      draggable={!renaming && !deleting}
      onDragStart={e => {
        if (renaming) {
          e.preventDefault()
          return
        }
        e.dataTransfer.setData('text/plain', note.path)
        e.dataTransfer.effectAllowed = 'move'
      }}
      onDragOver={e => {
        if (!actions) return
        e.preventDefault()
        e.stopPropagation()
        e.dataTransfer.dropEffect = 'move'
      }}
      onDrop={e => {
        if (!actions) return
        e.preventDefault()
        e.stopPropagation()
        const from = e.dataTransfer.getData('text/plain')
        if (from && from !== note.path) actions.onMove(from, ownFolder)
      }}
      style={{
        position: 'relative',
        padding: '8px 16px',
        borderRadius: '8px',
        cursor: deleting ? 'default' : 'pointer',
        // 50% on the row, so every piece of text in it dims together.
        ...(deleting ? { opacity: 0.5 } : null),
        ...(active ? { background: ACCENT_BG } : null),
      }}
    >
      {renaming ? (
        <input
          ref={inputRef}
          value={draft}
          aria-label={i18nT('apps.mdNotebook.row.renameField')}
          onChange={e => setDraft(e.target.value)}
          onClick={e => e.stopPropagation()}
          onBlur={commitRename}
          onKeyDown={e => {
            e.stopPropagation()
            if (e.key === 'Enter') {
              e.preventDefault()
              commitRename()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              actions?.onRenameEnd()
            }
          }}
          style={{
            width: '100%',
            boxSizing: 'border-box',
            background: 'var(--card)',
            border: `1px solid ${ACCENT}`,
            borderRadius: '6px',
            padding: '1px 6px',
            fontSize: '13px',
            fontWeight: 600,
            lineHeight: 1.375,
            color: 'var(--text)',
            fontFamily: FONT_BODY,
            outline: 'none',
          }}
        />
      ) : (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            minWidth: 0,
          }}
        >
          {pinned && (
            <Pin
              size={11}
              aria-hidden
              fill={ACCENT}
              stroke="none"
              style={{ flexShrink: 0, color: ACCENT }}
            />
          )}
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
        </div>
      )}
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
        {/* The badge slot doubles as the delete progress indicator: one place on
            the row already means "state of this file", so a second affordance
            would be noise. The precedence lives in `rowBadge`. */}
        {badge === 'deleting' ? (
          <span style={badgeStyle('conflict')}>
            {i18nT('apps.mdNotebook.badge.deleting')}
          </span>
        ) : (
          badge === 'pending' && (
            <span style={badgeStyle('pending')}>
              {i18nT('apps.mdNotebook.badge.pending')}
            </span>
          )
        )}
      </div>

      {/* Floating action bar, revealed on hover — or when a button inside it
          takes KEYBOARD focus, which is what keeps it Tab-reachable.
          `:has(:focus-visible)` on the bar, not `:focus-within` on the row, and
          both halves of that matter: the row itself is tabbable (Clickable), so
          a row rule kept the bar lit on the note you last clicked; and plain
          focus stays on a button after a MOUSE click, so it kept the bar lit on
          the note you last pinned. Hidden while renaming, which owns the row. */}
      {actions && !renaming && !deleting && (
        <div className="mdnb-row-actions">
          <button
            type="button"
            style={actionBtn}
            className="mdnb-act"
            title={pinned ? i18nT('apps.mdNotebook.row.unpin') : i18nT('apps.mdNotebook.row.pin')}
            aria-label={
              pinned ? i18nT('apps.mdNotebook.row.unpin') : i18nT('apps.mdNotebook.row.pin')
            }
            onClick={e => {
              e.stopPropagation()
              actions.onTogglePin(note.path)
            }}
          >
            {pinned ? <PinOff size={13} /> : <Pin size={13} />}
          </button>
          <button
            type="button"
            style={actionBtn}
            className="mdnb-act"
            title={i18nT('apps.mdNotebook.row.duplicate')}
            aria-label={i18nT('apps.mdNotebook.row.duplicate')}
            onClick={e => {
              e.stopPropagation()
              actions.onDuplicate(note.path)
            }}
          >
            <Copy size={13} />
          </button>
          <button
            type="button"
            style={actionBtn}
            className="mdnb-act"
            title={i18nT('apps.mdNotebook.row.rename')}
            aria-label={i18nT('apps.mdNotebook.row.rename')}
            onClick={e => {
              e.stopPropagation()
              actions.onRenameStart(note.path)
            }}
          >
            <Pencil size={13} />
          </button>
          {actions.onDelete && (
            <button
              type="button"
              style={{ ...actionBtn, color: 'var(--danger)' }}
              className="mdnb-act mdnb-act-danger"
              title={i18nT('apps.mdNotebook.row.delete')}
              aria-label={i18nT('apps.mdNotebook.row.delete')}
              onClick={e => {
                e.stopPropagation()
                actions.onDelete?.(note.path, note.title)
              }}
            >
              <Trash2 size={13} />
            </button>
          )}
        </div>
      )}
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
  /** False on a vault with no remote — see NoteRow's own prop. */
  showSyncBadge?: boolean
  actions: NoteActions
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
          if (from) rest.actions.onMove(from, name)
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

/**
 * Order notes for display: pinned first, then the user's chosen sort within
 * each group. Pinned notes stay inside their own folder rather than being
 * hoisted to the top of the tree — the folder is the note's location, and moving
 * a row out of it on pin would misreport where the file lives.
 */
export function orderNotes(
  notes: readonly Note[],
  cmp: (a: Note, b: Note) => number,
  isPinned: (path: string) => boolean,
): Note[] {
  return [...notes].sort((a, b) => {
    const pa = isPinned(a.path) ? 0 : 1
    const pb = isPinned(b.path) ? 0 : 1
    return pa !== pb ? pa - pb : cmp(a, b)
  })
}

/**
 * The paths the folders view renders, in the order it renders them: each
 * folder's subtree depth-first (folders alphabetical, notes by the active sort
 * with pinned first), then this level's own notes. A COLLAPSED folder
 * contributes nothing, because its notes are not on screen.
 *
 * Mirrors `renderTree` deliberately — it is what "the next note down" means, so
 * the two must not drift. Pure, so the ordering is unit-testable.
 */
export function flattenVisibleNotes(
  node: TreeNode,
  cmp: (a: Note, b: Note) => number,
  isPinned: (path: string) => boolean,
  collapsed: Set<string>,
  prefix = '',
): string[] {
  const out: string[] = []
  for (const [name, child] of [...node.folders].sort((a, b) => compareText(a[0], b[0]))) {
    const full = prefix ? `${prefix}/${name}` : name
    if (collapsed.has(full)) continue
    out.push(...flattenVisibleNotes(child, cmp, isPinned, collapsed, full))
  }
  for (const n of orderNotes(node.notes, cmp, isPinned)) out.push(n.path)
  return out
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
  for (const n of orderNotes(node.notes, props.cmp, props.actions.isPinned)) {
    items.push(
      <NoteRow
        key={n.path}
        note={n}
        active={n.path === props.activePath}
        onOpen={props.onOpen}
        showSyncBadge={props.showSyncBadge}
        actions={props.actions}
      />,
    )
  }
  return items
}
