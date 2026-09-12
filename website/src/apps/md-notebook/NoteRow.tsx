/** Rows for the notes panel: one note, one folder, and the tree renderer. */
import { Fragment, useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties, InputHTMLAttributes, ReactNode, Ref } from 'react'
import {
  Copy,
  FilePlus,
  Folder as FolderIcon,
  FolderOpen,
  FolderPlus,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Trash2,
} from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { ACCENT, ACCENT_BG, FONT_BODY, RAIL_TYPE, RAIL_X } from './constants'
import Clickable from '../../components/Clickable'
import { portableName, relTime, rowBadge } from './utils'
import type { Note, NoteActions, TreeNode } from './types'
import { compareText } from '../../i18n/format'
import { useImeGuard } from '../../hooks/useImeGuard'

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
    ...RAIL_TYPE.micro,
    fontWeight: 500,
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

/** The inline text field shared by note rename and new-folder naming. */
const inlineField: CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  background: 'var(--card)',
  border: `1px solid ${ACCENT}`,
  borderRadius: '6px',
  padding: '1px 6px',
  ...RAIL_TYPE.row,
  fontWeight: 600,
  color: 'var(--text)',
  fontFamily: FONT_BODY,
  outline: 'none',
}

/** The dropped-down create menu: sits under its trigger, right-aligned. */
const createMenu: CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 4px)',
  right: 0,
  minWidth: '200px',
  background: 'var(--bg-elevated)',
  border: '1px solid var(--border)',
  borderRadius: '8px',
  boxShadow: 'var(--shadow-md)',
  padding: '4px',
  zIndex: 20,
}

/** One item of the create menu. */
const createItem: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: '8px',
  padding: '5px 8px',
  borderRadius: '6px',
  cursor: 'pointer',
  ...RAIL_TYPE.row,
  color: 'var(--text)',
}

/**
 * The two-item "create" menu — a note or a folder, at the place the trigger
 * stands for. ONE trigger with this menu, never two buttons: a row already
 * holding another control (the vault selector in the header, the toggle on a
 * folder row) would otherwise carry three actions (AUTOSDE
 * max-two-buttons-per-row). Rendered inside a `position: relative` host, below
 * it. Choosing an item closes it; clicks stop at the menu so a host row's own
 * click (a folder toggle) does not fire underneath. It also closes on Escape
 * from wherever focus is (the trigger keeps focus right after opening, so a
 * handler on the items alone would miss the most common Escape) and on a
 * pointer press outside its host — the folder-row menu floats over the rows
 * below with the hover bar held visible, so left open it lingers over the
 * tree. The host, not the menu, is the boundary: a press on the trigger is
 * the trigger's toggle, and closing here as well would reopen it.
 */
export function CreateMenu({
  noteLabel,
  folderLabel,
  onNote,
  onFolder,
  onClose,
}: {
  noteLabel: string
  folderLabel: string
  onNote: () => void
  onFolder: () => void
  onClose: () => void
}) {
  const menuRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      const host = menuRef.current?.parentElement
      if (host && e.target instanceof Node && host.contains(e.target)) return
      onClose()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])
  const item = (label: string, glyph: ReactNode, act: () => void) => (
    <Clickable
      className="mdnb-row"
      aria-label={label}
      onClick={e => {
        e?.stopPropagation()
        onClose()
        act()
      }}
      onKeyDown={e => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          onClose()
        }
      }}
      style={createItem}
    >
      {glyph}
      <span style={{ flex: 1 }}>{label}</span>
    </Clickable>
  )
  return (
    <div ref={menuRef} style={createMenu}>
      {item(noteLabel, <FilePlus size={14} />, onNote)}
      {item(folderLabel, <FolderPlus size={14} />, onFolder)}
    </div>
  )
}

/**
 * An inline name field with its validation shown beside it. `problem` is the
 * reason the typed name cannot be used, or null; while it is set the field
 * stays open with the draft intact, marked invalid and described by the hint,
 * so the user fixes the name instead of retyping it. A validation hint about a
 * value not yet sent, not a failed operation — so plain text at the field,
 * never the error notice (AUTOSDE errors-use-error-notice).
 *
 * `hint` is a persistent disclosure shown under the field while there is no
 * problem — what committing will do. It lives here rather than in the
 * placeholder because a placeholder is clipped to the field's width (the rail
 * is narrow) and vanishes on the first keystroke, so a consequence written
 * there is exactly the text a user never gets to read. The problem replaces it
 * in place, so the row's height does not jump between the two.
 */
function NameField({
  inputRef,
  problem,
  hint,
  style,
  ...input
}: InputHTMLAttributes<HTMLInputElement> & {
  inputRef: Ref<HTMLInputElement>
  problem: string | null
  hint?: string
}) {
  const hintId = useId()
  const described = problem ?? hint
  return (
    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '2px' }}>
      <input
        ref={inputRef}
        aria-invalid={problem ? true : undefined}
        aria-describedby={described ? hintId : undefined}
        style={style}
        {...input}
      />
      {described && (
        <div
          id={hintId}
          style={{ ...RAIL_TYPE.micro, color: problem ? 'var(--danger)' : 'var(--muted)' }}
        >
          {described}
        </div>
      )}
    </div>
  )
}

/**
 * The name field for a folder about to be created under `parent` ('' is the
 * vault root). Rendered in the folder's place in the tree so the user names it
 * where it will appear. Enter commits, Escape abandons, blur commits — the same
 * contract as the note rename field, and a blank name is a cancel, not an error.
 * A name that cannot be used keeps the field open with the reason beside it.
 * The draft is mirrored to `actions.newFolderDraft` on every keystroke and read
 * back as the initial value: this row lives in the tree, which unmounts while a
 * search is typed, and the name typed so far must survive that round trip.
 * When the cleaner would change the name (`2026/Q1` → `2026Q1`), the hint shows
 * the name that will actually be created, before Enter — the app has no folder
 * rename, so a silently cleaned name is fixable only by moving notes.
 */
export function NewFolderRow({
  parent,
  depth,
  actions,
}: {
  parent: string
  depth: number
  actions: NoteActions
}) {
  const ime = useImeGuard()
  const [draft, setDraft] = useState(actions.newFolderDraft)
  const [problem, setProblem] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    inputRef.current?.focus()
  }, [])
  const typed = draft.trim()
  const clean = portableName(typed)
  const hint =
    typed && !problem && clean && clean !== typed
      ? i18nT('apps.mdNotebook.row.nameWillBecome', { name: clean })
      : i18nT('apps.mdNotebook.row.newFolderHint')
  // Guard against the double commit blur + Enter would otherwise produce: Enter
  // ends the field, which unmounts it, which fires blur on the way out.
  const done = useRef(false)
  const commit = () => {
    if (done.current) return
    const name = draft.trim()
    if (name) {
      // Refused: the field stays, draft and all, with the reason beside it.
      const why = actions.validateName(name)
      if (why) {
        setProblem(why)
        return
      }
    }
    done.current = true
    actions.onNewFolderEnd()
    if (name) actions.onNewFolder(parent, name)
  }
  const cancel = () => {
    if (done.current) return
    done.current = true
    actions.onNewFolderEnd()
  }
  return (
    <div
      className="mdnb-row"
      style={{
        display: 'flex',
        gap: '8px',
        alignItems: 'flex-start',
        padding: '4px 8px',
        marginLeft: depth * 10,
        color: 'var(--muted)',
      }}
    >
      <span style={{ display: 'flex', alignItems: 'center', flexShrink: 0, height: '24px' }}>
        <FolderPlus size={14} />
      </span>
      <NameField
        inputRef={inputRef}
        problem={problem}
        value={draft}
        aria-label={i18nT('apps.mdNotebook.row.newFolderField')}
        placeholder={i18nT('apps.mdNotebook.row.newFolderPlaceholder')}
        hint={hint}
        onChange={e => {
          setDraft(e.target.value)
          actions.onNewFolderDraft(e.target.value)
          setProblem(null)
        }}
        onClick={e => e.stopPropagation()}
        {...ime.bindComposition({ onBlur: commit })}
        onKeyDown={e => {
          e.stopPropagation()
          if (e.key === 'Enter') {
            if (!ime.claimEnter(e)) return
            commit()
          } else if (e.key === 'Escape') {
            e.preventDefault()
            cancel()
          }
        }}
        style={{ ...inlineField, fontWeight: 500 }}
      />
    </div>
  )
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
  const ime = useImeGuard()
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
  const [problem, setProblem] = useState<string | null>(null)
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
    setProblem(null)
    const el = inputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [renaming, note.title])

  const commitRename = () => {
    if (!actions) return
    const next = draft.trim()
    const changed = Boolean(next) && next !== note.title
    if (changed) {
      // Refused: the field stays, draft and all, with the reason beside it.
      const why = actions.validateName(next)
      if (why) {
        setProblem(why)
        return
      }
    }
    actions.onRenameEnd()
    if (changed) actions.onRename(note.path, next)
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
        <NameField
          inputRef={inputRef}
          problem={problem}
          value={draft}
          aria-label={i18nT('apps.mdNotebook.row.renameField')}
          onChange={e => {
            setDraft(e.target.value)
            setProblem(null)
          }}
          onClick={e => e.stopPropagation()}
          {...ime.bindComposition({ onBlur: commitRename })}
          onKeyDown={e => {
            e.stopPropagation()
            if (e.key === 'Enter') {
              if (!ime.claimEnter(e)) return
              commitRename()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              actions?.onRenameEnd()
            }
          }}
          style={inlineField}
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
              ...RAIL_TYPE.row,
              fontWeight: 600,
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
                ...RAIL_TYPE.meta,
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
            <span style={{ ...RAIL_TYPE.meta, color: 'var(--muted)', flexShrink: 0 }}>·</span>
          </>
        )}
        <span
          style={{ ...RAIL_TYPE.meta, fontWeight: 400, color: 'var(--muted)', flexShrink: 0 }}
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
  const [creating, setCreating] = useState(false)
  const Glyph = isCollapsed ? FolderIcon : FolderOpen
  const createLabel = i18nT('apps.mdNotebook.row.createHere')
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
          position: 'relative',
          display: 'flex',
          gap: '8px',
          alignItems: 'center',
          padding: '4px 8px',
          borderRadius: '8px',
          cursor: 'pointer',
          ...RAIL_TYPE.row,
          fontWeight: 500,
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
        {/* Ellipsized in a shrinkable child: bare text in a flex row cannot
            shrink below its longest word, and a folder name is one word up to
            120 code points. On touch the action bar sits in this row's flow,
            so an unshrinkable label would push the row's only create trigger
            past the viewport edge on a narrow screen. */}
        <span
          style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {name.split('/').pop()}
        </span>
        <span
          style={{ marginLeft: 'auto', flexShrink: 0, ...RAIL_TYPE.secondary, color: 'inherit' }}
        >
          {countNotes(node)}
        </span>
        {/* Same hover bar as a note row (see NoteRow for the :has(:focus-visible)
            reasoning). It covers the count while shown, which is the trade for
            keeping the row one line tall. ONE trigger opening the create menu,
            not one button per action: the row's own click is already the
            toggle, so a second visible button would make three actions on the
            row (AUTOSDE max-two-buttons-per-row). While the menu is open the
            bar is held visible inline — the menu hangs below the row, and the
            pointer crossing onto it would otherwise leave the row's hover and
            take the menu away with it — and lifted above the rows that follow:
            the bar's transform makes it a stacking context, so the menu's own
            z-index cannot reach past it, and the later rows (positioned, drawn
            after) would paint over the menu. `mdnb-folder-actions` is what the
            no-hover media rule targets: on a touch screen there is no hover, so
            this bar is shown outright — it is the only host of "new subfolder",
            and a host that only appears on hover removes the action on a phone
            (AUTOSDE narrow-viewport-required). Shown outright it can no longer
            float over the count (that trade only works for a bar that comes
            and goes), so the same rule drops it into the row's flow beside the
            count; `position:relative` keeps it the menu's anchor. */}
        <div
          className="mdnb-row-actions mdnb-folder-actions"
          style={creating ? { opacity: 1, pointerEvents: 'auto', zIndex: 30 } : undefined}
        >
          <button
            type="button"
            style={actionBtn}
            className="mdnb-act"
            title={createLabel}
            aria-label={createLabel}
            aria-expanded={creating}
            onClick={e => {
              e.stopPropagation()
              setCreating(o => !o)
            }}
          >
            <Plus size={13} />
          </button>
          {creating && (
            <CreateMenu
              noteLabel={i18nT('apps.mdNotebook.row.newNoteHere')}
              folderLabel={i18nT('apps.mdNotebook.row.newFolderHere')}
              onNote={() => rest.actions.onNewNote(name)}
              onFolder={() => rest.actions.onNewFolderStart(name)}
              onClose={() => setCreating(false)}
            />
          )}
        </div>
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
  // A folder being named appears where it will land: first among this level's
  // folders. The ROOT field is the page's to place (it must show in list view
  // too, where no tree is rendered), so only nested parents are handled here.
  if (prefix && props.actions.newFolderParent === prefix) {
    items.push(
      <NewFolderRow key="__new-folder__" parent={prefix} depth={depth} actions={props.actions} />,
    )
  }
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
