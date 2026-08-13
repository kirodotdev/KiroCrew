/**
 * FileTree — the project's source files, as a collapsible tree.
 *
 * Build artifacts (`.aux`, `.log`, `.pdf`, …) are filtered out: they are never
 * editable and they bury the two or three files a writer actually opens. The tree
 * shape is computed by the pure `buildTree`/`flattenTree` helpers in `lib.ts`, so
 * ordering and collapse behaviour are testable without rendering.
 *
 * Every row is a `<Clickable>` rather than a bare clickable div, so the tree is fully
 * keyboard-navigable, and the delete affordance is a labelled icon button rather
 * than the upstream app's right-click-to-delete — a context menu is invisible to
 * a keyboard user and undiscoverable to everyone else.
 */
import { useMemo, useState } from 'react'
import { useIsMobile } from '../../hooks/useIsMobile'
import { ChevronDown, ChevronRight, FileText, Folder, Image, Library, Plus, Settings2, Trash2 } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { buildTree, flattenTree, sourceFiles, type TreeNode } from './lib'

import { i18nT } from '../../i18n/t'

export interface FileTreeProps {
  files: string[]
  currentFile: string
  mainFile: string
  onOpenFile: (path: string) => void
  onCreateFile: () => void
  onDeleteFile: (path: string) => void
}

/** Lucide glyph for a file, by extension. Never an emoji (see AUTOSDE `no-emoji-as-icons`). */
function FileGlyph({ name }: { name: string }) {
  const lower = name.toLowerCase()
  if (lower.endsWith('.bib')) return <Library className="lucide-inline shrink-0 opacity-70" />
  if (lower.endsWith('.sty') || lower.endsWith('.cls')) {
    return <Settings2 className="lucide-inline shrink-0 opacity-70" />
  }
  if (/\.(png|jpe?g|gif|svg|webp|eps)$/.test(lower)) {
    return <Image className="lucide-inline shrink-0 opacity-70" />
  }
  return <FileText className="lucide-inline shrink-0 opacity-70" />
}

export default function FileTree({
  files,
  currentFile,
  mainFile,
  onOpenFile,
  onCreateFile,
  onDeleteFile,
}: FileTreeProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())

  const visible = useMemo(() => sourceFiles(files), [files])
  const tree = useMemo(() => buildTree(visible), [visible])
  const rows = useMemo(() => flattenTree(tree, collapsed), [tree, collapsed])

  const toggle = (path: string) =>
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  const renderRow = (node: TreeNode, depth: number) => {
    const indent = { paddingLeft: `${0.5 + depth * 0.85}rem` }

    if (node.isFolder) {
      const isCollapsed = collapsed.has(node.path)
      return (
        <Clickable
          key={`d:${node.path}`}
          style={indent}
          onClick={() => toggle(node.path)}
          title={node.path}
          aria-expanded={!isCollapsed}
          className="flex items-center gap-1 py-1 pr-2 text-[12px] text-muted hover:bg-bg-hover cursor-pointer select-none focus-ring"
        >
          {isCollapsed
            ? <ChevronRight className="lucide-inline shrink-0" />
            : <ChevronDown className="lucide-inline shrink-0" />}
          <Folder className="lucide-inline shrink-0 opacity-70" />
          <span className="truncate">{node.name}</span>
        </Clickable>
      )
    }

    const isOpen = node.path === currentFile
    const isMain = node.path === mainFile
    return (
      <div
        key={`f:${node.path}`}
        className={`group flex items-center gap-1 pr-1 ${isOpen ? 'bg-bg-hover' : 'hover:bg-bg-hover'}`}
      >
        <Clickable
          style={indent}
          onClick={() => onOpenFile(node.path)}
          title={node.path}
          aria-current={isOpen ? 'true' : undefined}
          className={`flex-1 min-w-0 flex items-center gap-1 py-1 text-[12px] cursor-pointer focus-ring ${
            isOpen ? 'text-text-strong font-medium' : 'text-text'
          }`}
        >
          <FileGlyph name={node.name} />
          <span className="truncate">{node.name}</span>
          {isMain && (
            <span className="ml-1 shrink-0 text-[10px] uppercase tracking-[.04em] text-accent">
              {i18nT('apps.papyrus.fileTree.main')}
            </span>
          )}
        </Clickable>
        {!isMain && (
          <button
            type="button"
            aria-label={i18nT('apps.papyrus.fileTree.delete_file', { file: node.name })}
            title={i18nT('apps.papyrus.fileTree.delete_file', { file: node.name })}
            onClick={() => onDeleteFile(node.path)}
            className="p-1 rounded text-muted opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-danger hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors"
          >
            <Trash2 className="lucide-inline" />
          </button>
        )}
      </div>
    )
  }

  const isMobile = useIsMobile()
  return (
    <div className="h-full min-h-0 flex flex-col border-r border-border bg-bg-subtle" data-testid="papyrus-file-tree">
      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0">
        {/* While narrow the page puts a labelled disclosure bar directly above
            this row, so repeating the word here is two adjacent headings saying
            the same thing. The spacer keeps the new-file button where it is. */}
        <span className={`flex-1 text-[12px] font-medium text-muted uppercase tracking-[.04em] ${isMobile ? 'sr-only' : ''}`}>
          {i18nT('apps.papyrus.fileTree.files')}
        </span>
        {isMobile && <span className="flex-1" />}
        <button
          type="button"
          aria-label={i18nT('apps.papyrus.fileTree.new_file')}
          title={i18nT('apps.papyrus.fileTree.new_file')}
          onClick={onCreateFile}
          className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
        >
          <Plus className="lucide-inline" />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto py-1">
        {rows.length === 0 ? (
          <div className="px-2 py-3 text-[12px] text-muted">
            {i18nT('apps.papyrus.fileTree.no_source_files')}
          </div>
        ) : (
          rows.map(({ node, depth }) => renderRow(node, depth))
        )}
      </div>
    </div>
  )
}
