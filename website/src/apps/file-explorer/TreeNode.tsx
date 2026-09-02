import { Fragment } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Folder, FolderOpen, FolderGit2, File, FileText, Image } from 'lucide-react'
import { IMAGE_EXTS } from './constants'
import { extOf } from './utils'
import { fileExplorerApi } from './api'
import type { TreeEntry, GitInfo } from './types'

import { i18nT } from '../../i18n/t'
function gitBadge(code: string | null) {
  if (!code) return null
  const c = code.trim()
  let color = 'var(--warn)'
  let label = c
  if (c === '??') { label = 'U'; color = 'var(--accent)' }
  else if (c.startsWith('M') || c.endsWith('M')) { label = 'M'; color = 'var(--warn)' }
  else if (c.startsWith('A') || c.endsWith('A')) { label = 'A'; color = 'var(--ok)' }
  else if (c.startsWith('D') || c.endsWith('D')) { label = 'D'; color = 'var(--danger)' }
  else if (c.startsWith('R')) { label = 'R'; color = 'var(--accent)' }
  else if (c === '!!') return null
  return (
    <span title={`git: ${c}`} style={{ fontSize: 9, fontWeight: 600, color, marginLeft: 4, border: `1px solid ${color}`, borderRadius: 3, padding: '0 3px', lineHeight: '12px' }}>
      {label}
    </span>
  )
}

interface TreeNodeProps {
  node: TreeEntry
  depth: number
  expanded: Record<string, boolean>
  toggleExpand: (node: TreeEntry) => void
  selectedPath: string
  onSelect: (node: TreeEntry) => void
  gitMap: Map<string, GitInfo>
  onContextMenu?: (e: React.MouseEvent, node: TreeEntry) => void
}

export default function TreeNode({ node, depth, expanded, toggleExpand, selectedPath, onSelect, gitMap, onContextMenu }: TreeNodeProps) {
  const isDir = node.type === 'dir' || node.type === 'symlink'
  const isOpen = expanded[node.path]
  const isSelected = selectedPath === node.path

  let gitCode: string | null = null
  if (gitMap && gitMap.size > 0) {
    let bestRoot = ''
    for (const root of gitMap.keys()) {
      if (node.path === root || node.path.startsWith(root + '/')) {
        if (root.length > bestRoot.length) bestRoot = root
      }
    }
    if (bestRoot) {
      const info = gitMap.get(bestRoot)
      const statuses = info?.statuses || {}
      if (node.path === bestRoot) {
        if (Object.keys(statuses).length > 0) gitCode = 'M'
      } else {
        const rel = node.path.slice(bestRoot.length + 1)
        if (statuses[rel]) gitCode = statuses[rel]
        else if (isDir) {
          for (const k of Object.keys(statuses)) {
            if (k.startsWith(rel + '/')) { gitCode = 'M'; break }
          }
        }
      }
    }
  }

  const IconComponent = isDir
    ? (node.isGitRoot ? FolderGit2 : (isOpen ? FolderOpen : Folder))
    : (IMAGE_EXTS.has(extOf(node.name))) ? Image
      : (extOf(node.name) === '.md') ? FileText
      : File

  // Lazy-load children via React Query subscription (enabled when expanded + no inline children)
  const needsFetch = isDir && isOpen && (!node.children || node.children.length === 0)
  const { data: lazyChildren } = useQuery({
    queryKey: ['file-explorer', 'tree-node', node.path],
    queryFn: () => fileExplorerApi.tree(node.path, 1),
    enabled: needsFetch,
    staleTime: 10_000,
  })

  const children = (Array.isArray(node.children) && node.children.length > 0)
    ? node.children
    : lazyChildren?.entries ?? null

  return (
    <Fragment>
      <div
        className={'mc-fe-tree-row' + (isSelected ? ' is-selected' : '')}
        style={{ paddingLeft: 6 + depth * 14 }}
        onClick={() => isDir ? toggleExpand(node) : onSelect(node)}
        onContextMenu={(e) => { if (onContextMenu) { e.preventDefault(); onContextMenu(e, node) } }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (isDir) toggleExpand(node); else onSelect(node) } }}
      >
        {isDir
          ? (isOpen ? <ChevronDown size={12} style={{ marginRight: 2, opacity: 0.55 }} /> : <ChevronRight size={12} style={{ marginRight: 2, opacity: 0.55 }} />)
          : <span style={{ width: 14, display: 'inline-block' }} />
        }
        <IconComponent size={13} style={{ marginRight: 4, opacity: 0.85, color: isDir ? 'var(--accent)' : 'var(--muted)' }} />
        {/* `min-w-0` is what lets `.mc-fe-tree-name`'s ellipsis engage: a flex
            child defaults to `min-width:auto`, which refuses to shrink below its
            content, so `text-overflow` never has an overflow to work with. */}
        <span className="mc-fe-tree-name min-w-0">{node.name}</span>
        {gitBadge(gitCode)}
      </div>
      {isDir && isOpen && Array.isArray(children) && children.map((c) => (
        <TreeNode
          key={c.path}
          node={c}
          depth={depth + 1}
          expanded={expanded}
          toggleExpand={toggleExpand}
          selectedPath={selectedPath}
          onSelect={onSelect}
          gitMap={gitMap}
          onContextMenu={onContextMenu}
        />
      ))}
      {isDir && isOpen && !children && (
        <div style={{ paddingLeft: 6 + (depth + 1) * 14 + 18, fontSize: 11, color: 'var(--muted)' }}>{i18nT('apps.fileExplorer.treeNode.loading')}</div>
      )}
    </Fragment>
  )
}
