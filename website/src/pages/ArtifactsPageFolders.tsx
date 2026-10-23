/** The Artifacts gallery's folders: the folder card and its content glimpse,
 *  the card grid that aligns with the gallery's columns, the breadcrumb, and
 *  the drag geometry the library's `DndContext` uses (pointer-containment
 *  collision, the cursor-centred overlay). */
import { useQuery } from '@tanstack/react-query'
import { FolderOpen, ChevronRight } from 'lucide-react'
import { pointerWithin, type CollisionDetection, type Modifier } from '@dnd-kit/core'
import { api } from '../api/client'
import { DndDraggable, DndDroppable } from '../components/dnd'
import { folderSubtreeStats, folderBreadcrumb } from '../utils/artifactFolderTree'
import type { Artifact, ArtifactFolder } from '../types'
import { FolderGlyph, FolderNameInput, FolderMenu } from '../components/library/LibraryTable'
import type { LibraryDrag, FolderActions } from '../components/library/LibraryTable'
import { WidgetThumb, ContentThumb, ImageThumb, WebAppThumb } from '../components/library/ArtifactThumbs'
import { useColumnCount } from '../hooks/useColumnCount'
import { i18nT } from '../i18n/t'

// ── Folders ──────────────────────────────────────────────────
// The library's DnD has only `folder-drop` droppables (folder cards/rows,
// breadcrumb segments, the Unfiled lane), so pointer containment is the whole
// story: a drop target is "over" only while the cursor is inside it. No
// closest-fallback — that would keep the nearest folder permanently
// highlighted during any drag, even with the cursor nowhere near it.
export const artifactLibraryCollision: CollisionDetection = (args) => pointerWithin(args)

// Center the DragOverlay ghost on the cursor. Without this the overlay spawns
// at the dragged element's top-left — grabbing a tall masonry card near its
// bottom leaves the ghost hundreds of pixels above the pointer. (Inline port
// of @dnd-kit/modifiers' snapCenterToCursor; the package isn't a dependency.)
export const snapOverlayToCursor: Modifier = ({ activatorEvent, draggingNodeRect, transform }) => {
  if (draggingNodeRect && activatorEvent && 'clientX' in activatorEvent && 'clientY' in activatorEvent) {
    const evt = activatorEvent as PointerEvent
    const offsetX = evt.clientX - draggingNodeRect.left
    const offsetY = evt.clientY - draggingNodeRect.top
    return {
      ...transform,
      x: transform.x + offsetX - draggingNodeRect.width / 2,
      y: transform.y + offsetY - draggingNodeRect.height / 2,
    }
  }
  return transform
}

/** Mini preview tile inside a gallery folder card — the same lazy full-fetch
 * the masonry cards use (shared ['artifact', slug] cache), clipped to a small
 * fixed-height tile so the folder reads as "a glimpse of what's inside". */
function FolderMiniThumb({ a }: { a: Artifact }) {
  const { data: full } = useQuery<Artifact>({
    queryKey: ['artifact', a.slug],
    queryFn: () => api.artifact(a.slug),
    staleTime: 60_000,
    enabled: !!a.slug,
  })
  const hasPreview = a.kind === 'widget' || a.kind === 'html'
  const content = full?.content || ''
  return (
    <div className="h-[84px] rounded-md border border-border overflow-hidden bg-bg-elevated pointer-events-none" title={a.name}>
      {a.kind === 'webapp' ? <WebAppThumb art={full ?? a} mini /> : a.kind === 'image' ? <ImageThumb a={a} /> : hasPreview ? <WidgetThumb content={content} slug={a.slug} goalReport={a.goal_report === true} /> : <ContentThumb content={content} kind={a.kind} mini />}
    </div>
  )
}

/** Gallery folder card: click to enter, draggable (nest via drop on another
 * folder card / breadcrumb), droppable (receives artifacts and folders).
 * Carries the same mb-3 row gap the masonry cards use; the column gutter is
 * the FolderCardGrid's `gap-x-3`, so the card must NOT add an `mr-3` of its own. */
export function FolderCard({ folder, folders, previewArtifacts, actions }: {
  folder: ArtifactFolder
  folders: ArtifactFolder[]
  previewArtifacts: Artifact[]
  actions: FolderActions
}) {
  const stats = folderSubtreeStats(folders, folder.id)
  const renaming = actions.renamingId === folder.id
  const preview = previewArtifacts.slice(0, 3)
  return (
    <DndDroppable id={`folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
      {({ setNodeRef: setDropRef, isOver }) => (
        <DndDraggable id={`folder:${folder.id}`} data={{ type: 'folder', id: folder.id, name: folder.name } satisfies LibraryDrag}>
          {({ setNodeRef: setDragRef, listeners, isDragging }) => (
            <div
              ref={(el) => { setDropRef(el); setDragRef(el) }}
              {...(renaming ? {} : listeners)}
              onClick={() => { if (!renaming) actions.onOpen(folder.id) }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (!renaming && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); actions.onOpen(folder.id) } }}
              aria-label={i18nT('pages.artifactsPage.open_folder', { name: folder.name })}
              className={`group mb-3 rounded-lg border bg-card p-3 cursor-pointer transition-all hover:border-border-strong hover:shadow-md ${
                isOver ? 'border-accent ring-2 ring-accent/40 bg-accent/5' : 'border-border'
              }`}
              style={{
                opacity: isDragging ? 0.4 : 1,
                ...(folder.color && !isOver ? { borderLeft: `3px solid ${folder.color}` } : {}),
              }}
            >
              {/* Content glimpse: up to three mini previews of what's inside. */}
              <div className={`grid gap-1.5 mb-2.5 ${preview.length === 3 ? 'grid-cols-3' : preview.length === 2 ? 'grid-cols-2' : 'grid-cols-1'}`}>
                {preview.length > 0 ? (
                  preview.map((a) => <FolderMiniThumb key={a.slug} a={a} />)
                ) : (
                  <div className="h-[84px] rounded-md border border-dashed border-border flex items-center justify-center text-muted">
                    <FolderOpen size={22} className="opacity-50" />
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2">
                <FolderGlyph folder={folder} size={17} />
                <div className="min-w-0 flex-1">
                  {renaming ? (
                    <FolderNameInput
                      initial={folder.name}
                      placeholder={i18nT('pages.artifactsPage.rename_folder')}
                      onCommit={(name) => actions.onRenameSubmit(folder, name)}
                      onCancel={actions.onRenameCancel}
                    />
                  ) : (
                    <div className="text-[15px] leading-tight text-text-strong font-semibold truncate">{folder.name}</div>
                  )}
                  <div className="text-[11px] text-muted mt-0.5">
                    {i18nT('pages.artifactsPage.artifact', { count: stats.artifactCount })}
                    {stats.subfolderCount > 0 ? ` · ${i18nT('pages.artifactsPage.folder', { count: stats.subfolderCount })}` : ''}
                  </div>
                </div>
                <div className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity shrink-0">
                  <FolderMenu folder={folder} folders={folders} actions={actions} />
                </div>
              </div>
            </div>
          )}
        </DndDraggable>
      )}
    </DndDroppable>
  )
}

/** Width of one card gutter (`mr-3` on the masonry cards, `gap-x-3` here). */
const CARD_GUTTER_PX = 12

/** Grid for folder cards, sized to align column-for-column with the masonry
 * gallery below (same 300px column pitch, same gutter).
 *
 * The gutter is a grid `gap`, NOT the masonry's `-mr-3` wrapper + per-card
 * `mr-3` scheme. That scheme makes the wrapper one gutter WIDER than its
 * parent, which is harmless in a padded page column but is 12px of scrollable
 * overflow inside a scroll container — and once the gallery virtualizes this
 * grid lives inside the capped, `overflow-y-auto` folder region (which makes
 * its overflow-x `auto` too), so the wrapper painted a horizontal scrollbar
 * under the folder cards. The gutter is passed to the column count so both
 * grids still divide the same width and never disagree at a boundary. */
export function FolderCardGrid({ children }: { children: React.ReactNode }) {
  const [ref, cols] = useColumnCount(300, CARD_GUTTER_PX)
  return (
    <div ref={ref} className="grid gap-x-3" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
      {children}
    </div>
  )
}

/** Gallery breadcrumb: "All Artifacts › Parent › Current". Non-current
 * segments navigate on click and accept drops (move the dragged item up to
 * that level; the root segment unfiles). */
export function FolderBreadcrumbBar({ folders, currentFolderId, onNavigate }: {
  folders: ArtifactFolder[]
  currentFolderId: string
  onNavigate: (folderId: string) => void
}) {
  const chain = folderBreadcrumb(folders, currentFolderId)
  const segment = (label: string, folderId: string, isCurrent: boolean) => (
    <DndDroppable key={folderId || 'root'} id={`crumb-drop:${folderId || 'root'}`} data={{ type: 'folder-drop', folderId }}>
      {({ setNodeRef, isOver }) => (
        <button
          ref={setNodeRef}
          type="button"
          disabled={isCurrent}
          onClick={() => onNavigate(folderId)}
          className={`px-1.5 py-0.5 rounded text-sm bg-transparent border-none transition-colors ${
            isCurrent
              ? 'text-text-strong font-medium cursor-default'
              : 'text-muted hover:text-text cursor-pointer'
          } ${isOver ? 'ring-2 ring-accent/40 text-text' : ''}`}
        >
          {label}
        </button>
      )}
    </DndDroppable>
  )
  return (
    <nav aria-label={i18nT('pages.artifactsPage.folder_breadcrumb')} className="flex items-center flex-wrap gap-0.5 mb-3">
      {segment(i18nT('pages.artifactsPage.all_artifacts'), '', chain.length === 0)}
      {chain.map((f, i) => (
        <span key={f.id} className="flex items-center gap-0.5">
          <ChevronRight size={12} className="text-muted" />
          {segment(f.name, f.id, i === chain.length - 1)}
        </span>
      ))}
    </nav>
  )
}
