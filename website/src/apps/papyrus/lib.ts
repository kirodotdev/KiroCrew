// Pure, side-effect-free helpers + localStorage accessors for Papyrus.
// No React, no component imports — safe to pull into any module or test.

import { compareText } from '../../i18n/format'
import type { Diagnostic, GitStatus } from './api'

/** localStorage key holding the project the user last had open. */
export const LAST_PROJECT_KEY = 'kc:papyrus:project'

/** localStorage key prefix mapping a project to its co-author chat slot. */
export const SLOT_KEY_PREFIX = 'kc:papyrus:slot:'

// Workspace column geometry. Four surfaces share one row (file tree, editor, PDF,
// co-author), so each resizable column gets its OWN key: they hold different
// shapes of content, and one key would mean dragging one silently resized the
// others. Bounds are what keeps a drag from producing an unusable layout — the
// editor is the pane carrying the text being written, so the PDF and co-author
// maxima stop short of squeezing it out.

/** File-tree column: persisted width, and the collapsed flag kept apart from it
 *  so re-expanding returns the tree to the width the user chose. */
export const TREE_WIDTH_KEY = 'kc:papyrus:tree-width'
export const TREE_COLLAPSED_KEY = 'kc:papyrus:tree-collapsed'
/** 176px = the `w-44` this replaced, so an existing user's layout is unchanged. */
export const DEFAULT_TREE_WIDTH = 176
export const MIN_TREE_WIDTH = 120
export const MAX_TREE_WIDTH = 420
/** Collapsed strip: wide enough for the expand button's hit target alone. */
export const COLLAPSED_TREE_WIDTH = 28

/** PDF preview column. Fixed-width with the grip on its LEFT edge, which leaves
 *  the source column `flex-1` — so widening the window grows the editor rather
 *  than scaling the preview the user just sized. */
export const PDF_WIDTH_KEY = 'kc:papyrus:pdf-width'
export const DEFAULT_PDF_WIDTH = 520
export const MIN_PDF_WIDTH = 280
/** The ceiling when the window cannot be measured (a test renderer with no
 *  layout, a server render): the share below has nothing to divide, so fall back
 *  to the widest preview a laptop can give without erasing the editor. */
export const MAX_PDF_WIDTH = 900
/** The ceiling is a SHARE of the window, not a pixel count.
 *
 *  Before this column became resizable the source pane was 50% and the preview
 *  took the rest, so ANY fixed cap silently removes reach as the window grows: at
 *  2560px a 900px cap stops the preview where the old layout gave it 1280, and
 *  the surplus lands on an editor that has no grip of its own to give it back —
 *  the preview is what the author drags, the editor only absorbs the remainder.
 *  Half the window reproduces the old reach at every size, and the same rule cuts
 *  the other way on a narrow one: a 1280px window caps the preview at 640 rather
 *  than 900, so a drag can no longer crush the editor. */
export const PDF_MAX_VIEWPORT_SHARE = 0.5
export const maxPdfWidth = (viewportWidth: number): number => (
  viewportWidth > 0
    ? Math.max(MIN_PDF_WIDTH, Math.round(viewportWidth * PDF_MAX_VIEWPORT_SHARE))
    : MAX_PDF_WIDTH
)

/** The starting width, bounded by the ceiling the window allows.
 *
 *  `loadColumnWidth` returns its fallback UNCHANGED — by design, since an
 *  out-of-range stored value says more about stale bounds than about the width
 *  the user wants. So the fallback has to arrive already legal: an unbounded 520
 *  on a 900px window would render 70px past the 450px ceiling the grip announces
 *  and the drag enforces, until the first drag pulled it back into range. */
export const defaultPdfWidth = (viewportWidth: number): number => (
  Math.min(maxPdfWidth(viewportWidth), DEFAULT_PDF_WIDTH)
)

/** Co-author column. The default is the width the panel opened at before it
 *  became resizable. */
export const CHAT_WIDTH_KEY = 'kc:papyrus:chat-width'
export const DEFAULT_CHAT_WIDTH = 420
export const MIN_CHAT_WIDTH = 280
export const MAX_CHAT_WIDTH = 720

/** The narrowest the editor may be squeezed to before the co-author panel gives
 *  ground — the one bound the editor has, since it owns no grip and takes what
 *  the other three columns leave.
 *
 *  280 is the minimum the two other CONTENT columns already use, and the
 *  arithmetic leaves little choice: on a 1280px window with the tree and preview
 *  at their defaults (176 + 520) the room left is 584px, and the co-author panel
 *  cannot go under its own 280px minimum — so 304px is the most any floor can
 *  actually be given. A floor of 400 would simply not be honoured.
 *
 *  It is a budget for the editor COLUMN: the three 6px grips are drawn in the
 *  same row, so at the boundary the editor measures ~262px of text. */
export const MIN_EDITOR_WIDTH = 280

/** The co-author ceiling: its own maximum, or the room left beside the editor's
 *  floor, whichever is smaller.
 *
 *  Persisting the open state turned a transient squeeze into the layout the
 *  author lands in on every return — with the defaults on a 1280px window
 *  (176 + 520 + 420 = 1116) the editor measured 164px. The panel is the column
 *  to take it from: it is the last one opened, and the only one whose content
 *  (a chat transcript) reads acceptably at its minimum.
 *
 *  Deliberately a CEILING applied where the width is consumed, not a discard on
 *  load the way an out-of-range stored width is treated: the room here depends on
 *  the other two columns, not only on the window, so a panel narrowed beside a
 *  wide preview must come back at its full width once the preview is dragged in
 *  or the tree collapsed. Yielding is not forgetting. */
export const maxChatWidth = (
  viewportWidth: number, treeWidth: number, pdfWidth: number,
): number => (
  viewportWidth > 0
    ? Math.max(MIN_CHAT_WIDTH, Math.min(
      MAX_CHAT_WIDTH, viewportWidth - treeWidth - pdfWidth - MIN_EDITOR_WIDTH,
    ))
    : MAX_CHAT_WIDTH
)
/** Whether the co-author panel is open. Persisting only the WIDTH still lost the
 *  layout on every return to a paper: the panel came back closed, so the
 *  workspace the author left was not the one restored. Absent means closed,
 *  which is the state the panel has always opened at.
 *
 *  A DESKTOP preference, read and written only while the viewport is wide, the
 *  same rule `useColumnResize.persist` applies to its own collapsed flag: while
 *  narrow the panel covers the pane, so an imported desktop flag would hide the
 *  editor and the PDF, and dismissing it there would rewrite a desktop layout. */
export const CHAT_OPEN_KEY = 'kc:papyrus:chat-open'

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
