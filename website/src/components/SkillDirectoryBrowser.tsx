import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, FileText, Folder, FolderOpen, AlertCircle } from 'lucide-react'
import { api } from '../api/client'
import MarkdownRenderer from './MarkdownRenderer'
import { CodeBlock } from './CodeBlock'
import { parseFrontmatter } from './SkillForm'
import type { Skill, SkillTreeEntry } from '../types'

import { i18nT } from '../i18n/t'
import { fmtBytes } from '../i18n/format'
interface TreeNode {
  name: string                   // basename
  path: string                   // relative path from skill root
  type: 'file' | 'dir'
  size: number
  children: TreeNode[]
}

/** Build a nested tree from the flat list returned by /api/skills/<name>/tree.
 *  Each entry's path is posix-style and relative to the skill root. */
function buildTree(entries: SkillTreeEntry[]): TreeNode {
  const root: TreeNode = { name: '', path: '', type: 'dir', size: 0, children: [] }
  const dirs = new Map<string, TreeNode>()
  dirs.set('', root)

  // Process in order — directories declared before files within them.
  // The backend already returns dirs first via os.walk's pre-order.
  // BYTE order, deliberately not a Collator: this sort exists so a parent
  // directory is always seen before its children (`dirs.get(parentPath)` below
  // silently `continue`s on a miss, dropping files from the tree). Collation
  // reorders `/` against `.` and `-`, so a locale-aware sort could break that
  // invariant in some languages and not others.
  const sorted = [...entries].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  for (const e of sorted) {
    const parts = e.path.split('/')
    const name = parts[parts.length - 1]
    const parentPath = parts.slice(0, -1).join('/')
    const parent = dirs.get(parentPath)
    if (!parent) continue  // orphan — backend invariant violated; skip
    const node: TreeNode = { name, path: e.path, type: e.type, size: e.size, children: [] }
    parent.children.push(node)
    if (e.type === 'dir') dirs.set(e.path, node)
  }
  return root
}

/** Pick the language identifier for syntax highlighting based on filename. */
function langForFile(path: string): string {
  const lower = path.toLowerCase()
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown'
  if (lower.endsWith('.py')) return 'python'
  if (lower.endsWith('.sh') || lower.endsWith('.bash') || lower.endsWith('.zsh')) return 'bash'
  if (lower.endsWith('.js') || lower.endsWith('.mjs') || lower.endsWith('.cjs')) return 'javascript'
  if (lower.endsWith('.ts') || lower.endsWith('.tsx')) return 'typescript'
  if (lower.endsWith('.json')) return 'json'
  if (lower.endsWith('.yml') || lower.endsWith('.yaml')) return 'yaml'
  if (lower.endsWith('.toml')) return 'toml'
  if (lower.endsWith('.html') || lower.endsWith('.htm')) return 'html'
  if (lower.endsWith('.css')) return 'css'
  if (lower.endsWith('.rs')) return 'rust'
  if (lower.endsWith('.go')) return 'go'
  return 'plaintext'
}

/** Delegates to the shared `fmtBytes` so byte sizes format consistently
 *  (spacing, precision and capitalisation). */
const humanSize = (bytes: number): string => fmtBytes(bytes)

/** Labeled frontmatter strip — the skill's contract (description, triggers,
 *  tags, loaded-by-agents). Shared between the installed-skill viewer and
 *  the Add Skill browser's detail preview so both surfaces present skill
 *  metadata identically. */
export function SkillMetaStrip({
  description, triggers, tags, loadedByAgents,
}: {
  description?: string
  triggers?: string
  tags?: string
  loadedByAgents?: string[]
}) {
  const show = !!(description || triggers || tags || (loadedByAgents && loadedByAgents.length > 0))
  if (!show) return null
  return (
    <div className="space-y-2 mb-3 pb-3 border-b border-border" data-testid="frontmatter-strip">
      {description && (
        <div>
          <div className="text-[11px] text-muted uppercase tracking-wide font-semibold mb-0.5">{i18nT('components.skillDirectoryBrowser.description')}</div>
          <div className="text-[13px] text-text leading-relaxed">{description}</div>
        </div>
      )}
      {triggers && (
        <div>
          <div className="text-[11px] text-muted uppercase tracking-wide font-semibold mb-0.5">{i18nT('components.skillDirectoryBrowser.triggers')}</div>
          <div className="flex gap-1 flex-wrap">{triggers.split(',').map((t, i) => (
            <span key={i} className="text-[12px] px-1.5 py-[1px] rounded bg-bg-elevated text-muted border border-border font-mono">{t.trim()}</span>
          ))}</div>
        </div>
      )}
      {tags && (
        <div>
          <div className="text-[11px] text-muted uppercase tracking-wide font-semibold mb-0.5">{i18nT('components.skillDirectoryBrowser.tags')}</div>
          <div className="flex gap-1 flex-wrap">{tags.replace(/[[\]]/g, '').split(',').map((t, i) => (
            <span key={i} className="text-[12px] px-1.5 py-[1px] rounded bg-accent-subtle text-accent font-mono">{t.trim()}</span>
          ))}</div>
        </div>
      )}
      {loadedByAgents && loadedByAgents.length > 0 && (
        <div>
          <div className="text-[11px] text-muted uppercase tracking-wide font-semibold mb-0.5">{i18nT('components.skillDirectoryBrowser.loaded_by_agents')}</div>
          <div className="flex gap-1 flex-wrap">{loadedByAgents.map((a, i) => (
            <span key={i} className="text-[12px] px-1.5 py-[1px] rounded bg-bg-elevated text-text border border-border font-mono">{a}</span>
          ))}</div>
        </div>
      )}
    </div>
  )
}

/** Recursive tree row.  Folders toggle on click; files select. */
function TreeRow({
  node, depth, expanded, selected, onToggle, onSelect,
}: {
  node: TreeNode
  depth: number
  expanded: Set<string>
  selected: string
  onToggle: (path: string) => void
  onSelect: (path: string) => void
}) {
  const isOpen = expanded.has(node.path)
  const indent = { paddingLeft: `${depth * 14 + 8}px` }
  if (node.type === 'dir') {
    return (
      <>
        <button
          type="button"
          onClick={() => onToggle(node.path)}
          className="flex items-center gap-1 w-full text-left text-[13px] py-1 hover:bg-bg-elevated rounded cursor-pointer"
          style={indent}
          aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${node.name || 'root'}`}
        >
          <ChevronRight size={12} className={`shrink-0 text-muted transition-transform ${isOpen ? 'rotate-90' : ''}`} />
          {isOpen ? <FolderOpen size={14} className="shrink-0 text-muted" /> : <Folder size={14} className="shrink-0 text-muted" />}
          <span className="truncate">{node.name || i18nT('components.skillDirectoryBrowser.root')}</span>
          {node.children.length > 0 && (
            <span className="ml-auto text-[10px] text-muted/60 mr-2">{node.children.length}</span>
          )}
        </button>
        {isOpen && node.children.map(c => (
          <TreeRow
            key={c.path} node={c} depth={depth + 1}
            expanded={expanded} selected={selected}
            onToggle={onToggle} onSelect={onSelect}
          />
        ))}
      </>
    )
  }
  const isSelected = selected === node.path
  return (
    <button
      type="button"
      onClick={() => onSelect(node.path)}
      className={`flex items-center gap-1 w-full text-left text-[13px] py-1 rounded cursor-pointer ${
        isSelected ? 'bg-accent-subtle text-accent font-semibold' : 'hover:bg-bg-elevated'
      }`}
      style={indent}
      aria-label={i18nT('components.skillDirectoryBrowser.open', { path: node.path })}
      aria-current={isSelected ? 'true' : undefined}
    >
      <span className="w-3 shrink-0" aria-hidden />
      <FileText size={14} className="shrink-0 text-muted" />
      <span className="truncate">{node.name}</span>
      <span className="ml-auto text-[10px] text-muted/60 mr-2">{humanSize(node.size)}</span>
    </button>
  )
}

/** Renders a directory listing for one skill on the left and the selected
 *  file's content on the right.  When *skill* is provided, a frontmatter
 *  strip (description, triggers, tags, loaded_by_agents) is rendered on
 *  top of the right pane regardless of which file is selected — it's
 *  read from the SKILL.md fetch at the top level so users can see the
 *  skill's contract while exploring its files.
 *
 *  Read-only — edits route through the enclosing modal's existing
 *  edit/delete affordances on SKILL.md only. */
export default function SkillDirectoryBrowser({
  skillKey, skill,
}: {
  skillKey: string
  skill?: Skill
}) {
  const { data: tree, isLoading: treeLoading, error: treeErr } = useQuery({
    queryKey: ['skill-tree', skillKey],
    queryFn: () => api.skillTree(skillKey),
    enabled: !!skillKey,
  })

  const root = useMemo(() => {
    const entries: SkillTreeEntry[] = (tree && (tree as { entries?: SkillTreeEntry[] }).entries) || []
    return buildTree(entries)
  }, [tree])

  // Default selection: SKILL.md if present at root.
  const [selected, setSelected] = useState<string>('SKILL.md')
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['']))  // root open by default

  const toggle = (path: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const select = (path: string) => {
    setSelected(path)
  }

  const { data: fileData, isLoading: fileLoading, error: fileErr } = useQuery({
    queryKey: ['skill-file', skillKey, selected],
    queryFn: () => api.skillFile(skillKey, selected),
    enabled: !!skillKey && !!selected,
  })

  const rawContent = (fileData && (fileData as { content?: string }).content) ?? ''
  const lang = langForFile(selected)

  // Strip leading YAML frontmatter from any markdown file before rendering.
  // CommonMark interprets ``---\nkey: value\n---`` as a setext H2 heading
  // (with the keys becoming the heading text) — that's noise in any
  // rendered context.  Other file types pass through unchanged.
  const fileBody = useMemo(() => {
    if (lang !== 'markdown') return rawContent
    return parseFrontmatter(rawContent).body
  }, [rawContent, lang])

  // Frontmatter strip is the skill's *contract* — it must stay stable while
  // the user browses other files, so it reads from SKILL.md specifically,
  // not the currently-selected file.  React Query dedupes this with the
  // file query above when SKILL.md is the selected file (no extra fetch).
  const { data: skillMdData } = useQuery({
    queryKey: ['skill-file', skillKey, 'SKILL.md'],
    queryFn: () => api.skillFile(skillKey, 'SKILL.md'),
    enabled: !!skillKey,
  })
  const skillMdContent = (skillMdData as { content?: string } | undefined)?.content ?? ''
  const skillMeta = useMemo(() => parseFrontmatter(skillMdContent).meta, [skillMdContent])

  // Prefer the Skill object's canonical fields (already redacted/normalized
  // by the backend); fall back to SKILL.md's frontmatter when no Skill is passed.
  const stripDescription = skill?.description ?? skillMeta.description ?? ''
  const stripTriggers = skillMeta.triggers ?? ''
  const stripTags = skillMeta.tags ?? ''

  return (
    <div className="flex gap-3 h-full min-h-0" data-testid="skill-directory-browser">
      {/* File-tree pane (pane 2 of the master-detail layout) */}
      <div className="w-[200px] shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md bg-bg-elevated/50 p-1">
        {treeLoading && <div className="text-muted text-[12px] p-2 animate-pulse">{i18nT('components.skillDirectoryBrowser.loading_tree')}</div>}
        {treeErr && <div className="text-danger text-[12px] p-2 flex items-start gap-1.5"><AlertCircle size={14} className="shrink-0" />{i18nT('components.skillDirectoryBrowser.failed_to_load_tree')}</div>}
        {!treeLoading && !treeErr && root.children.length === 0 && (
          <div className="text-muted/70 text-[12px] italic p-2">{i18nT('components.skillDirectoryBrowser.empty_skill_folder')}</div>
        )}
        {!treeLoading && root.children.map(c => (
          <TreeRow
            key={c.path} node={c} depth={0}
            expanded={expanded} selected={selected}
            onToggle={toggle} onSelect={select}
          />
        ))}
      </div>

      {/* File viewer pane */}
      <div className="flex-1 overflow-y-auto scrollbar-overlay border border-border rounded-md bg-card p-3">
        <SkillMetaStrip
          description={stripDescription}
          triggers={stripTriggers}
          tags={stripTags}
          loadedByAgents={skill?.loaded_by_agents}
        />
        <div className="text-[11px] font-mono text-muted mb-2 truncate" title={selected}>{selected || i18nT('components.skillDirectoryBrowser.no_file_selected')}</div>
        {fileLoading && <div className="text-muted text-[12px] animate-pulse">{i18nT('components.skillDirectoryBrowser.loading_file')}</div>}
        {fileErr && (
          <div className="text-danger text-[12px] flex items-start gap-1.5">
            <AlertCircle size={14} className="shrink-0" />
            <span>{(fileErr as Error)?.message || i18nT('components.skillDirectoryBrowser.failed_to_load_file')}</span>
          </div>
        )}
        {!fileLoading && !fileErr && lang === 'markdown' && (
          <div className="text-sm leading-relaxed"><MarkdownRenderer content={fileBody} /></div>
        )}
        {!fileLoading && !fileErr && lang !== 'markdown' && (
          <CodeBlock code={rawContent} lang={lang} complete={true} />
        )}
      </div>
    </div>
  )
}
