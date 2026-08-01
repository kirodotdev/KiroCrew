// Pure, side-effect-free helpers + localStorage accessors for Papyrus.
// No React, no component imports — safe to pull into any module or test.

import { compareText } from '../../i18n/format'
import type { Diagnostic, GitStatus } from './api'

/** localStorage key holding the project the user last had open. */
export const LAST_PROJECT_KEY = 'kc:papyrus:project'

/** localStorage key prefix mapping a project to its co-author chat slot. */
export const SLOT_KEY_PREFIX = 'kc:papyrus:slot:'

/** Suffixes hidden from the file tree — LaTeX build artifacts, never editable. */
const ARTIFACT_SUFFIXES = [
  '.aux', '.bbl', '.blg', '.fdb_latexmk', '.fls', '.log', '.out',
  '.synctex.gz', '.toc', '.lof', '.lot', '.nav', '.snm', '.vrb', '.pdf',
]

/** True when a path is a build artifact rather than paper source. */
export function isArtifact(path: string): boolean {
  const lower = path.toLowerCase()
  return ARTIFACT_SUFFIXES.some(suffix => lower.endsWith(suffix))
}

/** Drop build artifacts from a flat file list. */
export function sourceFiles(files: string[]): string[] {
  return files.filter(f => !isArtifact(f))
}

/** Only `.tex` files can be the main document. */
export function texFiles(files: string[]): string[] {
  return files.filter(f => f.toLowerCase().endsWith('.tex'))
}

/**
 * The Monaco language id for a project file.
 *
 * Monaco ships no LaTeX or BibTeX grammar, so `.tex` and `.bib` resolve to
 * `plaintext` rather than being highlighted as something they are not — a wrong
 * grammar is worse than none, because it mis-colours every backslash and brace
 * in the document. Papyrus instead registers a small LaTeX tokenizer of its own
 * (see `latexLanguage.ts`) and returns its id here; if that registration ever
 * fails, Monaco falls back to plaintext, which is the correct degradation.
 *
 * Only ids Monaco actually bundles are returned for the other extensions —
 * claiming an id Monaco does not have buys nothing over `plaintext`.
 */
export const LATEX_LANGUAGE_ID = 'papyrus-latex'

export function monacoLanguage(path: string): string {
  const lower = path.toLowerCase()
  if (lower.endsWith('.tex') || lower.endsWith('.sty') || lower.endsWith('.cls')) {
    return LATEX_LANGUAGE_ID
  }
  if (lower.endsWith('.bib')) return LATEX_LANGUAGE_ID
  if (lower.endsWith('.md')) return 'markdown'
  if (lower.endsWith('.py')) return 'python'
  if (lower.endsWith('.sh')) return 'shell'
  if (lower.endsWith('.yaml') || lower.endsWith('.yml')) return 'yaml'
  if (lower.endsWith('.json')) return 'json'
  return 'plaintext'
}

/** One node of the file tree: a folder with children, or a leaf file. */
export interface TreeNode {
  /** Display name — the last path segment. */
  name: string
  /** Full relative path for a file; the folder path for a folder. */
  path: string
  isFolder: boolean
  children: TreeNode[]
}

/**
 * Build a hierarchical tree from a flat list of POSIX relative paths.
 * Folders sort before files; both alphabetically within their group, so the
 * ordering is stable regardless of the order the backend walked the tree.
 */
export function buildTree(paths: string[]): TreeNode[] {
  // Intermediate shape: a folder is a Map, a file is its full path string.
  type Entry = Map<string, Entry | string>
  const root: Entry = new Map()

  for (const path of paths) {
    const segments = path.split('/')
    let node = root
    for (let i = 0; i < segments.length - 1; i++) {
      const segment = segments[i]
      const existing = node.get(segment)
      if (!(existing instanceof Map)) {
        const created: Entry = new Map()
        node.set(segment, created)
        node = created
      } else {
        node = existing
      }
    }
    node.set(segments[segments.length - 1], path)
  }

  function toNodes(entry: Entry, parentPath: string): TreeNode[] {
    return [...entry.entries()]
      .sort(([aName, aVal], [bName, bVal]) => {
        const aFolder = aVal instanceof Map
        const bFolder = bVal instanceof Map
        if (aFolder !== bFolder) return aFolder ? -1 : 1
        // File and folder names are user-visible text, so they sort by the APP's
        // language rather than the browser's host locale.
        return compareText(aName, bName)
      })
      .map(([name, value]): TreeNode => {
        if (typeof value === 'string') {
          return { name, path: value, isFolder: false, children: [] }
        }
        const folderPath = parentPath ? `${parentPath}/${name}` : name
        return { name, path: folderPath, isFolder: true, children: toNodes(value, folderPath) }
      })
  }

  return toNodes(root, '')
}

/** Flatten a tree into rows for rendering, honouring the collapsed-folder set. */
export interface TreeRow {
  node: TreeNode
  depth: number
}

export function flattenTree(nodes: TreeNode[], collapsed: ReadonlySet<string>, depth = 0): TreeRow[] {
  const rows: TreeRow[] = []
  for (const node of nodes) {
    rows.push({ node, depth })
    if (node.isFolder && !collapsed.has(node.path)) {
      rows.push(...flattenTree(node.children, collapsed, depth + 1))
    }
  }
  return rows
}

/** Count diagnostics per level, for the stat cards and the status bar. */
export interface DiagnosticCounts {
  errors: number
  warnings: number
  typesetting: number
}

export function countDiagnostics(diagnostics: Diagnostic[]): DiagnosticCounts {
  const counts: DiagnosticCounts = { errors: 0, warnings: 0, typesetting: 0 }
  for (const d of diagnostics) {
    if (d.level === 'error') counts.errors++
    else if (d.level === 'warning') counts.warnings++
    else counts.typesetting++
  }
  return counts
}

/**
 * Word count for a LaTeX document: prose only.
 *
 * Comments, math, and command tokens are dropped so the number tracks what a
 * reader would count rather than the size of the markup. It is an ESTIMATE by
 * construction — an exact count needs a TeX parser — but a stable one, which is
 * what makes it useful for tracking progress against a page limit.
 *
 * The precise rule, so the number is predictable rather than mysterious:
 *
 * - a `%` comment is dropped to end of line, but `\%` is a literal percent sign
 *   (extremely common in a results table) and is kept;
 * - `$…$`, `$$…$$` and the `equation`/`align`/`gather`/`multline` environments
 *   (starred or not) are dropped whole;
 * - any remaining whitespace-delimited token starting with `\` is dropped
 *   ENTIRELY — so `\section{Introduction}` contributes nothing, heading text
 *   included. Splitting the argument out would need brace matching, and a
 *   heading is a handful of words against a body of thousands;
 * - a token with no letter at all (`---`, `42`, `!!!`) is not a word.
 */
export function countWords(source: string): number {
  const withoutComments = source
    .split('\n')
    .map(line => line.replace(/(^|[^\\])%.*$/, '$1'))
    .join('\n')
  const withoutMath = withoutComments
    .replace(/\\begin\{(equation|align|gather|multline)\*?\}[\s\S]*?\\end\{\1\*?\}/g, ' ')
    .replace(/\$\$[\s\S]*?\$\$/g, ' ')
    .replace(/\$[^$\n]*\$/g, ' ')
  const words = withoutMath
    .split(/\s+/)
    .filter(token => token && !token.startsWith('\\') && /[A-Za-zÀ-ɏ]/.test(token))
  return words.length
}

/** Compose the toolbar's one-line git label, e.g. `main*` with ahead/behind counts. */
export function gitBranchLabel(status: GitStatus | undefined): string {
  if (!status?.is_git) return ''
  return `${status.branch || ''}${status.dirty ? '*' : ''}`
}

/** Read the last-open project name from localStorage (never throws). */
export function loadLastProject(): string | null {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY)
  } catch {
    return null
  }
}

/** Persist (or clear) the last-open project name (never throws). */
export function saveLastProject(name: string | null): void {
  try {
    if (name) localStorage.setItem(LAST_PROJECT_KEY, name)
    else localStorage.removeItem(LAST_PROJECT_KEY)
  } catch {
    /* storage blocked or full — the session still works, it just won't restore */
  }
}

/** Read a project's remembered co-author chat slot (never throws). */
export function loadSlot(project: string): string | null {
  try {
    return localStorage.getItem(SLOT_KEY_PREFIX + project)
  } catch {
    return null
  }
}

/** Remember a project's co-author chat slot (never throws). */
export function saveSlot(project: string, slot: string): void {
  try {
    localStorage.setItem(SLOT_KEY_PREFIX + project, slot)
  } catch {
    /* storage blocked or full — a new slot is created next time instead */
  }
}

/**
 * Drop remembered slots whose project no longer exists.
 *
 * Without this the keys accumulate forever, and a project name reused after a
 * delete would resurrect the OLD paper's conversation — which reads as the agent
 * hallucinating context it was in fact given.
 */
export function pruneSlots(liveProjects: string[]): void {
  try {
    const live = new Set(liveProjects.map(name => SLOT_KEY_PREFIX + name))
    const stale: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith(SLOT_KEY_PREFIX) && !live.has(key)) stale.push(key)
    }
    stale.forEach(key => localStorage.removeItem(key))
  } catch {
    /* storage unavailable — nothing to prune */
  }
}
