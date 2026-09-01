import type * as React from 'react'
import { useId, useState, useRef } from 'react'
import { ChevronRight, ChevronDown, ChevronUp, MoreVertical, Pencil, Trash2, Star, ExternalLink, Loader2, X, Share2, FileText, FolderOpen, Folder as FolderIcon } from 'lucide-react'
import { openPopout } from '../../utils/artifactPopout'
import { Badge, Btn, Input, IconButton } from '../ui'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '../ui/dropdown-menu'
import { timeAgo as _timeAgo } from '../../utils/timeAgo'
import FolderMoveSubmenu from '../FolderMoveSubmenu'
import { DndDraggable, DndDroppable } from '../dnd'
import { childFolders, isDescendantFolder, folderSubtreeStats } from '../../utils/artifactFolderTree'
import { useImeGuard } from '../../hooks/useImeGuard'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { FOLDER_COLOR_PALETTE } from '../folderColorCatalog'
import { i18nT } from '../../i18n/t'
import type { Artifact, ArtifactFolder, SessionDoc } from '../../types'

export type SortKey = 'name' | 'slug' | 'kind' | 'source' | 'version' | 'tags' | 'updated'
export type SortState = { key: SortKey; dir: 'asc' | 'desc' } | null

export const KIND_BADGE: Record<Artifact['kind'], 'ok' | 'err' | 'warn' | 'aim'> = {
  widget: 'aim',
  html: 'ok',
  markdown: 'ok',
  svg: 'warn',
  json: 'ok',
  text: 'ok',
  webapp: 'aim',
  image: 'warn',
}

export function isoToTs(iso: string): number {
  if (!iso) return 0
  const t = Date.parse(iso)
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0
}

/** Infer an artifact `kind` for a session document from its extension.
 * Mirrors the backend's DOC_EXTENSIONS (.md/.markdown/.mdx → markdown;
 * .txt/.rst → text). */
export function docFileType(path: string): Artifact['kind'] {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return ext === 'txt' || ext === 'rst' ? 'text' : 'markdown'
}

/** Payload carried by draggable cards/rows; routes the drop in handleDragEnd. */
export type LibraryDrag =
  | { type: 'artifact'; slug: string; name: string; folderId: string }
  | { type: 'folder'; id: string; name: string }

export type FolderActions = {
  onOpen: (folderId: string) => void
  onRename: (f: ArtifactFolder) => void
  onMove: (f: ArtifactFolder, newParentId: string) => void
  onDelete: (f: ArtifactFolder) => void
  onSetColor: (f: ArtifactFolder, color: string) => void
  /** Folder currently in inline-rename mode (its card/row swaps the name for an input). */
  renamingId: string | null
  onRenameSubmit: (f: ArtifactFolder, name: string) => void
  onRenameCancel: () => void
}

/** Curated folder color palette (works on light + dark themes). '' = none. */
/** Swatch strip for picking a folder color ('' clears back to default).
 *  The palette is the shared folder catalog (folderColorCatalog.tsx), so
 *  artifact folders and chat folders offer the same hues and the aria labels
 *  reuse the localized color names. */
export function FolderColorSwatches({ value, onPick, size = 16 }: { value?: string; onPick: (color: string) => void; size?: number }) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap" role="radiogroup" aria-label={i18nT('pages.artifactsPage.folder_color')}>
      {FOLDER_COLOR_PALETTE.map(({ value: c, label }) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={value === c}
          aria-label={label()}
          title={label()}
          onClick={(e) => { e.stopPropagation(); onPick(c) }}
          onPointerDown={(e) => e.stopPropagation()}
          className={`rounded-full border cursor-pointer transition-transform hover:scale-110 ${
            value === c ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'
          }`}
          style={{ width: size, height: size, background: c }}
        />
      ))}
      <button
        type="button"
        role="radio"
        aria-checked={!value}
        aria-label={i18nT('pages.artifactsPage.no_color')}
        title={i18nT('pages.artifactsPage.no_color')}
        onClick={(e) => { e.stopPropagation(); onPick('') }}
        onPointerDown={(e) => e.stopPropagation()}
        className={`rounded-full border cursor-pointer transition-transform hover:scale-110 flex items-center justify-center text-muted bg-transparent ${
          !value ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'
        }`}
        style={{ width: size, height: size }}
      >
        <X size={Math.max(8, size - 7)} />
      </button>
    </div>
  )
}

/** Folder glyph — same composition as the chat sidebar's FolderGlyph: the
 * Lucide Folder icon is always the icon (design-token colorable, CSS-sized,
 * fixed footprint), with the auto-derived emoji overlaid as a small badge on
 * the closed folder's flat face. Expanded folders show the open glyph alone
 * (its angled flap has no flat face for the badge). */
export function FolderGlyph({ folder, size = 16, open = false }: { folder: ArtifactFolder; size?: number; open?: boolean }) {
  const Glyph = open ? FolderOpen : FolderIcon
  return (
    <span className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <Glyph size={size} className="shrink-0" style={{ color: folder.color || 'var(--accent)' }} />
      {folder.icon && !open && (
        <span
          aria-hidden
          className="absolute inset-x-0 bottom-0 flex items-center justify-center leading-none pointer-events-none"
          style={{ top: Math.round(size * 0.42), fontSize: Math.max(7, Math.round(size * 0.52)) }}
        >
          {folder.icon}
        </span>
      )}
    </span>
  )
}

/** Inline folder-name editor (create + rename) — the same native pattern the
 * chat sidebar uses for slot/folder renames: autofocused input, Enter commits,
 * Escape cancels, blur commits a non-empty value. IME-guarded. */
export function FolderNameInput({ initial = '', placeholder = 'Folder name', onCommit, onCancel }: {
  initial?: string
  placeholder?: string
  onCommit: (name: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  const cancelledRef = useRef(false)
  const ime = useImeGuard()
  return (
    <Input
      autoFocus
      value={value}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(e) => setValue(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      className="w-full bg-transparent border border-accent rounded px-1.5 py-0.5 text-text-strong outline-none text-sm select-text focus-ring"
      {...ime.bindEnter<HTMLInputElement>({
        onFocus: (e) => (e.target as HTMLInputElement).select(),
        onEnter: () => { (document.activeElement as HTMLInputElement)?.blur() },
        onEscape: () => { cancelledRef.current = true; onCancel() },
        onBlur: () => {
          if (cancelledRef.current) { cancelledRef.current = false; return }
          const name = value.trim()
          if (name) onCommit(name)
          else onCancel()
        },
      })}
    />
  )
}

/** Shared "…" menu for a folder (gallery card + table row). The move submenu
 * excludes the folder's own subtree — a folder can't become its own descendant. */
export function FolderMenu({ folder, folders, actions }: { folder: ArtifactFolder; folders: ArtifactFolder[]; actions: FolderActions }) {
  const moveTargets = folders.filter(f => !isDescendantFolder(folders, folder.id, f.id))
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          onClick={(e) => e.stopPropagation()}
          className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
          title={i18nT('pages.artifactsPage.folder_actions')}
          aria-label={i18nT('pages.artifactsPage.actions_for_folder', { name: folder.name })}
        >
          <MoreVertical size={13} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        <DropdownMenuItem onSelect={() => actions.onRename(folder)}>
          <Pencil size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.rename')}
        </DropdownMenuItem>
        <FolderMoveSubmenu
          variant="dropdown"
          folders={moveTargets}
          currentFolderId={folder.parent_id || null}
          onPick={(pid) => actions.onMove(folder, pid || '')}
        />
        <DropdownMenuSeparator />
        {/* Color swatches live inline (not a menu item) so picking one doesn't
            navigate — the menu closes after the pick via the row's own click. */}
        <div className="px-2 py-1.5">
          <div className="text-[11px] text-muted mb-1.5">{i18nT('pages.artifactsPage.color')}</div>
          <FolderColorSwatches value={folder.color} onPick={(c) => actions.onSetColor(folder, c)} />
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem className="text-danger" onSelect={() => actions.onDelete(folder)}>
          <Trash2 size={13} className="shrink-0" /> {i18nT('pages.artifactsPage.delete')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Column headers shared by the flat table and the folder tree table. Data
 * columns sort on click (asc → desc → default); the star and Actions columns
 * are control columns and stay plain. */
/**
 * One declared column of a library-shaped table.
 *
 * The table CHROME is shared and the ROW is not, deliberately. Two surfaces
 * render this table today -- the artifact library and the AWS Control cloud
 * drive -- and their rows have nothing in common: an artifact row carries a
 * drag payload, a popout, a pin and a publication badge keyed on `slug`, while a
 * drive row carries an S3 key with download, share and delete. Forcing one row
 * component to serve both would be a props union that is a conditional in
 * disguise. What they DO share is the header: the `th` styling, the sort
 * control, and the pinned Actions cell with its measured seam -- which is the
 * part that is easy to get subtly wrong and expensive to keep in sync by hand.
 */
export type LibraryColumn = {
  /** Sort key, or '' for a column that does not sort (a star or icon gutter). */
  key: SortKey | ''
  /** Already-translated header label. '' renders an empty cell with aria-label. */
  label: string
  /** Width / min-width utilities for this column. */
  className?: string
  /** Accessible name when `label` is empty. */
  ariaLabel?: string
}

/** The artifact library's own nine columns, unchanged. */
export const ARTIFACT_COLUMNS: LibraryColumn[] = [
  { key: '', label: '', className: 'w-[40px] text-center', ariaLabel: 'pages.artifactsPage.starred' },
  { key: 'name', label: 'pages.artifactsPage.name', className: 'min-w-[160px]' },
  { key: 'slug', label: 'pages.artifactsPage.slug', className: 'w-[180px]' },
  { key: 'kind', label: 'pages.artifactsPage.kind', className: 'w-[100px]' },
  { key: 'source', label: 'pages.artifactsPage.source', className: 'w-[110px]' },
  { key: 'version', label: 'pages.artifactsPage.ver', className: 'w-[60px]' },
  { key: 'tags', label: 'pages.artifactsPage.tags', className: 'min-w-[160px]' },
  { key: 'updated', label: 'pages.artifactsPage.updated', className: 'w-[110px]' },
]

export function LibraryTableHead({ sort, onSort, edgeRight = false, columns = ARTIFACT_COLUMNS, actionsLabelKey = 'pages.artifactsPage.actions' }: {
  sort: SortState
  onSort: (key: SortKey) => void
  edgeRight?: boolean
  /** Data columns, in order. The Actions column is not one of them: it is
   *  written literally below because its pin is the fragile part. */
  columns?: LibraryColumn[]
  actionsLabelKey?: string
}) {
  const th = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
  const sortable = (key: SortKey, label: string, extra: string) => {
    const active = sort?.key === key
    return (
      <th
        key={key}
        className={`${th} ${extra}`}
        aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
      >
        <Btn
          type="button"
          onClick={() => onSort(key)}
          className={`bg-transparent border-none p-0 gap-1 rounded-none text-[12px] font-medium uppercase tracking-[.04em] hover:bg-transparent active:scale-100 ${active ? 'text-text hover:text-text' : 'text-muted hover:text-text'}`}
        >
          {label}
          {active && (sort.dir === 'asc'
            ? <ChevronUp size={12} className="shrink-0" aria-hidden="true" />
            : <ChevronDown size={12} className="shrink-0" aria-hidden="true" />)}
        </Btn>
      </th>
    )
  }
  return (
    <thead>
      <tr>
        {columns.map((c, i) => (
          c.key
            ? sortable(c.key, i18nT(c.label), c.className ?? '')
            : (
              <th
                key={`c${i}`}
                className={`${th} ${c.className ?? ''}`}
                aria-label={c.ariaLabel ? i18nT(c.ariaLabel) : undefined}
              >
                {c.label ? i18nT(c.label) : null}
              </th>
            )
        ))}
        {/* Actions is the last column, and the declared widths total
            past a phone (and a rail-narrowed desktop pane), so at rest it
            starts beyond the scroll edge and every open/delete costs a
            horizontal scroll. Pinned `sticky right-0` on an OPAQUE `bg-card`
            (the default cell background is transparent and the scrolling
            columns would show through the pin). The seam is TWO parts, both
            gated on the measured overflow flag so a table that fits renders
            neither: a 1px child div (NOT `border-l` — under Preflight's
            `border-collapse: collapse` a cell border belongs to the collapsed
            table grid and paints at the cell's layout slot, so it stays behind
            while the sticky cell travels) and a `right-full` gradient hung just
            left of the pin (says "columns continue"). Same treatment as the
            hooks and schedule tables, adapted for auto layout where a
            wrapper-anchored cue cannot know the pinned column's left edge. */}
        <th className={`${th} w-[120px] sticky right-0 bg-card`}>
          {edgeRight && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
          {edgeRight && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
          {i18nT(actionsLabelKey)}
        </th>
      </tr>
    </thead>
  )
}

/** One artifact row, shared by the flat table and the folder tree. Draggable
 * onto folder rows / the Unfiled lane (indent nests it under its folder). */
export function ArtifactRow({ a, onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug = null, indent = 0, dropFolderId, dropHighlight = false, edgeRight = false }: {
  a: Artifact
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  /** Toggle the artifact's pin/favorite mark. */
  onTogglePin: (a: Artifact) => void
  /** Slug whose pin toggle is in flight (disables its star to avoid double-fire). */
  pinningSlug?: string | null
  indent?: number
  /** True while the table's scroller hides columns past its right edge — gates
   * the pinned Actions cell's seam + fade so a table that fits shows neither. */
  edgeRight?: boolean
  /** When set, the row also accepts drops, filing the dragged item into this
   * folder (''=unfile) — so dropping anywhere over an expanded folder's
   * region (or the Unfiled section) works, not just on the header row. */
  dropFolderId?: string
  /** True while the active drag hovers anywhere over this row's folder region. */
  dropHighlight?: boolean
}) {
  const inner = (setDropRef?: (el: HTMLElement | null) => void) => (
    <DndDraggable id={`artifact-row:${a.slug}`} data={{ type: 'artifact', slug: a.slug, name: a.name, folderId: a.folder_id || '' } satisfies LibraryDrag}>
      {({ setNodeRef, listeners, isDragging }) => (
        <tr
          ref={(el) => { setNodeRef(el); setDropRef?.(el) }}
          {...listeners}
          style={{ opacity: isDragging ? 0.4 : 1 }}
          className={`group/artrow transition-colors cursor-pointer ${dropHighlight ? 'bg-accent/10' : 'hover:bg-bg-hover'}`}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey) {
              openPopout(a.slug, a.name)
            } else {
              onOpen(a.slug)
            }
          }}
        >
          <td className="px-2.5 py-2 border-b border-border text-center">
            <button
              type="button"
              disabled={pinningSlug === a.slug}
              onClick={(e) => { e.stopPropagation(); onTogglePin(a) }}
              className={`p-0.5 rounded transition-colors cursor-pointer bg-transparent border-none disabled:cursor-default ${a.pinned ? 'text-accent' : 'text-muted/40 hover:text-accent'}`}
              title={a.pinned ? i18nT('pages.artifactsPage.starred_click_to_unstar') : i18nT('pages.artifactsPage.star_artifact')}
              aria-label={a.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_artifact')}
              aria-pressed={!!a.pinned}
            >
              <Star size={14} className={a.pinned ? 'fill-current' : ''} />
            </button>
          </td>
          <td className="px-2.5 py-2 border-b border-border" style={indent > 0 ? { paddingLeft: `${10 + indent * 20}px` } : undefined}>
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-text-strong font-medium">{a.name}</span>
              {a.publication && (
                <Share2
                  size={12}
                  className={a.publication.last_error ? 'text-danger' : 'text-ok'}
                  aria-label={a.publication.last_error ? i18nT('pages.artifactsPage.published_sync_issue') : i18nT('pages.artifactsPage.published', { visibility: a.publication.visibility.toLowerCase() })}
                />
              )}
            </div>
            {a.description && <div className="text-[12px] text-muted truncate max-w-[400px]">{a.description}</div>}
          </td>
          <td className="px-2.5 py-2 border-b border-border">
            <code className="text-[12px] text-muted">{a.slug}</code>
          </td>
          <td className="px-2.5 py-2 border-b border-border">
            <Badge variant={KIND_BADGE[a.kind]}>{a.kind}</Badge>
          </td>
          <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted truncate max-w-[180px]" title={a.session_title || a.source}>{a.session_title || a.source}</td>
          <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{i18nT('pages.artifactsPage.v')}{a.version}</td>
          <td className="px-2.5 py-2 border-b border-border">
            <div className="flex flex-wrap gap-1">
              {(a.tags || []).map((t) => (
                <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted">{t}</span>
              ))}
            </div>
          </td>
          <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">{_timeAgo(isoToTs(a.updated_at))}</td>
          {/* Pinned like the header cell, on an OPAQUE `bg-card`. The row's
              states live on the <tr>, which the opaque base would hide, so the
              overlay re-applies them beneath the controls (`-z-10` inside the
              stacking context the sticky cell creates): the `.table-striped`
              zebra keys off the row's REAL DOM position (`nth-child(even)`),
              which — unlike the hooks/schedule tables' clean `.map` index — is
              not knowable here (folder rows, artifact rows, and lane rows
              interleave), so the overlay mirrors it with the ancestor arbitrary
              variant instead of an index; the drag-file highlight and the hover
              tint layer on top, matching the <tr>. */}
          <td className="sticky right-0 bg-card px-2.5 py-2 border-b border-border">
            <div aria-hidden className={`absolute inset-0 -z-10 transition-colors [.table-striped_tbody_tr:nth-child(even)_&]:bg-[var(--card-hl)] ${dropHighlight ? 'bg-accent/10' : 'group-hover/artrow:bg-bg-hover'}`} />
            {edgeRight && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
            {edgeRight && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); openPopout(a.slug, a.name) }}
                className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                title={i18nT('pages.artifactsPage.pop_out_into_its_own_window')}
                aria-label={i18nT('pages.artifactsPage.pop_out_to_window')}
              >
                <ExternalLink size={13} />
              </button>
              <button
                type="button"
                disabled={deletingSlug === a.slug}
                onClick={(e) => { e.stopPropagation(); onDelete(a) }}
                className="p-1 rounded text-muted hover:text-danger transition-colors cursor-pointer bg-transparent border-none disabled:opacity-60 disabled:cursor-default"
                title={i18nT('pages.artifactsPage.remove_from_library')}
                aria-label={i18nT('pages.artifactsPage.remove_from_artifacts_library')}
              >
                {deletingSlug === a.slug ? <Loader2 size={13} className="animate-spin" /> : <X size={13} />}
              </button>
            </div>
          </td>
        </tr>
      )}
    </DndDraggable>
  )
  if (dropFolderId === undefined) return inner()
  return (
    <DndDroppable id={`row-drop:${a.slug}`} data={{ type: 'folder-drop', folderId: dropFolderId }}>
      {({ setNodeRef }) => inner(setNodeRef)}
    </DndDroppable>
  )
}


/** The star-to-materialize affordance shared by the table/tree rows and the
 * gallery section, so the two views cannot drift (this PR is already the
 * second "feature existed in one view only" fix of this class). */
export function SessionDocStar({ d, busy, onMaterialize }: { d: SessionDoc; busy: boolean; onMaterialize: (path: string, sessionKey?: string) => void }) {
  return (
    <IconButton
      variant="accent"
      disabled={busy}
      onClick={() => onMaterialize(d.path, d.session_key)}
      title={i18nT('pages.artifactsPage.star_creates_a_starred_artifact_from_this_docume')}
      aria-label={i18nT('pages.artifactsPage.star_document')}
      className="shrink-0"
    >
      {busy ? <Loader2 size={14} className="animate-spin" /> : <Star size={14} />}
    </IconButton>
  )
}

/** A single unsaved session-document row (from "your chats"). Leading star
 * materializes it into a real, starred artifact. Shares the same columns as
 * ArtifactRow so both live in one unified table. */
export function SessionDocRow({ d, busy, onMaterialize, edgeRight = false }: { d: SessionDoc; busy: boolean; onMaterialize: (path: string, sessionKey?: string) => void; edgeRight?: boolean }) {
  const ftype = docFileType(d.path)
  return (
    <tr className="group/docrow transition-colors hover:bg-bg-hover">
      <td className="px-2.5 py-2 border-b border-border text-center">
        <SessionDocStar d={d} busy={busy} onMaterialize={onMaterialize} />
      </td>
      <td className="px-2.5 py-2 border-b border-border">
        <div className="flex items-center gap-1.5 min-w-0">
          <FileText size={13} className="text-ok shrink-0" />
          <span className="text-sm text-text-strong font-medium truncate">{d.name}</span>
        </div>
        <div className="text-[11px] text-muted truncate max-w-[420px]">{d.path}</div>
      </td>
      <td className="px-2.5 py-2 border-b border-border"><code className="text-[12px] text-muted">—</code></td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">{ftype}</td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted truncate max-w-[180px]" title={d.session_title}>{d.session_title}</td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">—</td>
      {/* eslint-disable-next-line jsx-a11y/control-has-associated-label -- the Tags column, empty because a session document carries none. A cell of a plain data <table> has role `cell`: it is named by its contents and an empty one is legitimately unnamed, associated with the column by the header's `th`. The rule reads every `td` as a grid's `gridcell` widget. */}
      <td className="px-2.5 py-2 border-b border-border"></td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted whitespace-nowrap">{_timeAgo(isoToTs(d.updated_at))}</td>
      {/* This row has no Actions controls, but it shares the pinned column, so
          its trailing cell must pin too — otherwise the scrolling columns show
          through where the pin sits. Same opaque base + overlay (zebra by real
          DOM position + hover) + gated seam as ArtifactRow. */}
      <td className="sticky right-0 bg-card px-2.5 py-2 border-b border-border">
        <div aria-hidden className="absolute inset-0 -z-10 transition-colors [.table-striped_tbody_tr:nth-child(even)_&]:bg-[var(--card-hl)] group-hover/docrow:bg-bg-hover" />
        {edgeRight && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
        {edgeRight && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
      </td>
    </tr>
  )
}

/** The compact table view of the local artifact library (flat —
 * rendered while any filter is active, when folder scoping is bypassed). */
export function LibraryTable({
  items,
  sort,
  onSort,
  onOpen,
  onDelete,
  deletingSlug,
  onTogglePin,
  pinningSlug,
  sessionDocs = [],
  onMaterialize,
  materializingPath = null,
}: {
  items: Artifact[]
  sort: SortState
  onSort: (key: SortKey) => void
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
  sessionDocs?: SessionDoc[]
  onMaterialize?: (path: string, sessionKey?: string) => void
  materializingPath?: string | null
}) {
  // The pinned Actions column's seam is painted only while the scroller hides
  // columns. Auto layout means the ROWS set scrollWidth (a filter emptying
  // rows, a locale switch re-labelling headers, a webfont load), none of which
  // resize the scroller's own box — so the table is the observed content node.
  const [attachScroller, edges, , attachTable] = useScrollEdges<HTMLDivElement>()
  return (
    <div ref={attachScroller} className="overflow-x-auto">
      <table ref={attachTable} className="w-full border-collapse table-striped">
        <LibraryTableHead sort={sort} onSort={onSort} edgeRight={edges.right} />
        <tbody>
          {items.map((a) => (
            <ArtifactRow key={a.slug} a={a} onOpen={onOpen} onDelete={onDelete} deletingSlug={deletingSlug} onTogglePin={onTogglePin} pinningSlug={pinningSlug} edgeRight={edges.right} />
          ))}
          {onMaterialize && sessionDocs.map((d) => (
            <SessionDocRow key={d.path} d={d} busy={materializingPath === d.path} onMaterialize={onMaterialize} edgeRight={edges.right} />
          ))}
        </tbody>
      </table>
    </div>
  )
}


/** Folder header row in the tree table: collapsible (chevron / row click),
 * draggable (reorder among siblings, nest elsewhere), droppable. */
export function FolderRow({ folder, folders, depth, expanded, onToggle, actions, dropHighlight = false }: {
  folder: ArtifactFolder
  folders: ArtifactFolder[]
  depth: number
  expanded: boolean
  onToggle: (id: string) => void
  actions: FolderActions
  /** True while the active drag hovers anywhere over this folder's region
   * (header row or any row inside it) — lights the whole folder up. */
  dropHighlight?: boolean
}) {
  const stats = folderSubtreeStats(folders, folder.id)
  const Chevron = expanded ? ChevronDown : ChevronRight
  const renaming = actions.renamingId === folder.id
  return (
    <DndDroppable id={`folder-row-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
      {({ setNodeRef: setDropRef, isOver }) => (
        <DndDraggable id={`folder-row:${folder.id}`} data={{ type: 'folder', id: folder.id, name: folder.name } satisfies LibraryDrag}>
          {({ setNodeRef: setDragRef, listeners, isDragging }) => (
            <tr
              ref={(el) => { setDropRef(el); setDragRef(el) }}
              {...(renaming ? {} : listeners)}
              onClick={() => { if (!renaming) onToggle(folder.id) }}
              style={{ opacity: isDragging ? 0.4 : 1 }}
              className={`group cursor-pointer transition-colors ${isOver || dropHighlight ? 'bg-accent/15' : 'hover:bg-bg-hover'}`}
              aria-expanded={expanded}
            >
              <td colSpan={9} className="px-2.5 py-1.5 border-b border-border" style={depth > 0 ? { paddingLeft: `${10 + depth * 20}px` } : undefined}>
                <div className={`flex items-center gap-1.5 rounded transition-shadow ${isOver || dropHighlight ? 'ring-2 ring-inset ring-accent/50 px-1 -mx-1' : ''}`}>
                  <Chevron size={13} className="text-muted shrink-0" />
                  <FolderGlyph folder={folder} size={14} open={expanded} />
                  {renaming ? (
                    <span className="min-w-0 flex-1 max-w-[280px]">
                      <FolderNameInput
                        initial={folder.name}
                        placeholder={i18nT('pages.artifactsPage.rename_folder')}
                        onCommit={(name) => actions.onRenameSubmit(folder, name)}
                        onCancel={actions.onRenameCancel}
                      />
                    </span>
                  ) : (
                    <span className="text-sm text-text-strong font-medium truncate">{folder.name}</span>
                  )}
                  <span className="text-[11px] text-muted">
                    {stats.artifactCount}{stats.subfolderCount > 0 ? ` · ${i18nT('pages.artifactsPage.folder', { count: stats.subfolderCount })}` : ''}
                  </span>
                  <span className="ml-auto opacity-0 group-hover:opacity-100 transition-opacity">
                    <FolderMenu folder={folder} folders={folders} actions={actions} />
                  </span>
                </div>
              </td>
            </tr>
          )}
        </DndDraggable>
      )}
    </DndDroppable>
  )
}

/** Nested, collapsible tree table (browse mode): folders in pre-order with
 * their artifacts indented beneath, Unfiled at the end. Collapsed by default —
 * expansion is client-local (localStorage), by design (§2.5). */
export function LibraryTree({ items, sort, onSort, folders, expandedIds, onToggleExpand, folderActions, onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug, overFolderId, dragActive, sessionDocs = [], onMaterialize, materializingPath = null }: {
  items: Artifact[]
  sort: SortState
  onSort: (key: SortKey) => void
  folders: ArtifactFolder[]
  expandedIds: ReadonlySet<string>
  onToggleExpand: (id: string) => void
  folderActions: FolderActions
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
  /** Folder the active drag currently hovers (''=Unfiled, null=none). */
  overFolderId: string | null
  /** True while any library drag is in flight. */
  dragActive: boolean
  sessionDocs?: SessionDoc[]
  onMaterialize?: (path: string, sessionKey?: string) => void
  materializingPath?: string | null
}) {
  // See LibraryTable: the pinned Actions seam is gated on measured overflow,
  // and auto layout makes the table (not the scroller's box) the content node.
  const [attachScroller, edges, , attachTable] = useScrollEdges<HTMLDivElement>()
  // Names the Unfiled drop lane from its own visible "Unfiled N" text rather
  // than a duplicate translated string. Instance-scoped because two trees can
  // mount at once (library page + a side panel) and a repeated id would point
  // both lanes at the first one's label.
  const unfiledLabelId = useId()
  const folderIds = new Set(folders.map(f => f.id))
  const byFolder = new Map<string, Artifact[]>()
  for (const a of items) {
    // Dangling folder_id (deleted folder) degrades to Unfiled.
    const fid = a.folder_id && folderIds.has(a.folder_id) ? a.folder_id : ''
    const bucket = byFolder.get(fid)
    if (bucket) bucket.push(a)
    else byFolder.set(fid, [a])
  }
  const rows: React.ReactNode[] = []
  const walk = (parentId: string, depth: number, visited: Set<string>) => {
    for (const f of childFolders(folders, parentId)) {
      if (visited.has(f.id) || depth > 20) continue
      visited.add(f.id)
      const expanded = expandedIds.has(f.id)
      rows.push(
        <FolderRow
          key={`folder:${f.id}`}
          folder={f}
          folders={folders}
          depth={depth}
          expanded={expanded}
          onToggle={onToggleExpand}
          actions={folderActions}
          dropHighlight={overFolderId === f.id}
        />,
      )
      if (expanded) {
        for (const a of byFolder.get(f.id) || []) {
          rows.push(
            <ArtifactRow
              key={a.slug}
              a={a}
              onOpen={onOpen}
              onDelete={onDelete}
              deletingSlug={deletingSlug}
              onTogglePin={onTogglePin}
              pinningSlug={pinningSlug}
              indent={depth + 1}
              dropFolderId={f.id}
              dropHighlight={overFolderId === f.id}
              edgeRight={edges.right}
            />,
          )
        }
        walk(f.id, depth + 1, visited)
      }
    }
  }
  walk('', 0, new Set())
  const unfiled = byFolder.get('') || []
  const unfiledHot = overFolderId === ''
  return (
    <div ref={attachScroller} className="overflow-x-auto">
      <table ref={attachTable} className="w-full border-collapse table-striped">
        <LibraryTableHead sort={sort} onSort={onSort} edgeRight={edges.right} />
        <tbody>
          {rows}
          {folders.length > 0 && (
            <DndDroppable id="unfiled-lane" data={{ type: 'folder-drop', folderId: '' }}>
              {({ setNodeRef, isOver }) => (
                <tr ref={setNodeRef} aria-labelledby={unfiledLabelId} className={`transition-colors ${isOver || unfiledHot ? 'bg-accent/15' : ''}`}>
                  <td colSpan={9} className="px-2.5 border-b border-border" style={{ paddingTop: dragActive ? 10 : 6, paddingBottom: dragActive ? 10 : 6 }}>
                    <div className={`flex items-center gap-2 rounded transition-all ${
                      dragActive ? `border border-dashed px-2 py-1.5 ${isOver || unfiledHot ? 'border-accent text-text' : 'border-border text-muted'}` : ''
                    }`}>
                      <span id={unfiledLabelId} className="text-[11px] uppercase tracking-[.04em] text-muted font-medium">
                        {i18nT('pages.artifactsPage.unfiled')} {unfiled.length}
                      </span>
                      {dragActive && (
                        <span className="text-[11px] text-muted italic">{i18nT('pages.artifactsPage.drop_here_to_unfile')}</span>
                      )}
                    </div>
                  </td>
                </tr>
              )}
            </DndDroppable>
          )}
          {unfiled.map((a) => (
            <ArtifactRow
              key={a.slug}
              a={a}
              onOpen={onOpen}
              onDelete={onDelete}
              deletingSlug={deletingSlug}
              onTogglePin={onTogglePin}
              pinningSlug={pinningSlug}
              dropFolderId={folders.length > 0 ? '' : undefined}
              dropHighlight={unfiledHot}
              edgeRight={edges.right}
            />
          ))}
          {onMaterialize && sessionDocs.map((d) => (
            <SessionDocRow key={d.path} d={d} busy={materializingPath === d.path} onMaterialize={onMaterialize} edgeRight={edges.right} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

