/** Shared types for the Notes builtin app. Mirrors the backend's JSON shapes. */

export interface Vault {
  id: string
  name: string
  repo: string
  branch: string
  localPath: string
  readOnly: boolean
  subfolder?: string
  /** Attached in place rather than cloned by the app. Computed by the backend. */
  external?: boolean
  /** Attached from a repo with no git remote: sync commits locally, never pushes. */
  localOnly?: boolean
  /** Registered as a Kiro Crew Knowledge source. */
  knowledge?: boolean
  knowledgeSourceId?: string | null
}

export interface Note {
  path: string
  title: string
  modifiedAt: number
  createdAt?: number
  syncStatus: 'synced' | 'pending'
}

export interface WikiLink {
  target: string
  alias?: string | null
  resolvedPath: string | null
}

export interface NoteMeta {
  frontmatter: Record<string, unknown>
  tags: string[]
  links: WikiLink[]
}

export interface Backlink {
  sourcePath: string
  line: number
  context: string
}

export interface NoteDoc {
  path: string
  content: string
  /** Snapshot token for the save guard. */
  mtime: number
  meta: NoteMeta
  backlinks: Backlink[]
}

export interface SearchHit {
  path: string
  title: string
  score: number
  snippet?: string | null
}

export interface FileChange {
  path: string
  kind: 'added' | 'modified' | 'deleted'
}

export interface ConflictVersions {
  path: string
  local: string
  remote: string
}

export interface SyncResult {
  pushed: boolean
  pulled: boolean
  committed: FileChange[]
  conflicts: ConflictVersions[]
  /** The vault has no remote: the run committed locally and stopped. */
  localOnly?: boolean
}

/** A recorded keyboard shortcut. */
export interface Shortcut {
  key: string
  meta: boolean
  ctrl: boolean
  alt: boolean
  shift: boolean
}

/** Folder tree built from the flat note list. */
export interface TreeNode {
  folders: Map<string, TreeNode>
  notes: Note[]
}

/** Source range of one rendered block, plus the caret column to land on. */
export interface EditRange {
  start: number
  end: number
  caret?: number
}

/**
 * Row-level affordances for a note in the panel: the hover action bar, inline
 * rename, and drag-to-file. Bundled into one object so the tree renderer can
 * pass them down without a prop per action.
 */
export interface NoteActions {
  isPinned: (path: string) => boolean
  onTogglePin: (path: string) => void
  onDuplicate: (path: string) => void
  /**
   * Absent when the backend cannot move a note to `.trash` — an older bundle
   * still running while this UI is new. Its DELETE unlinks the file outright, so
   * offering the action would break the confirmation's promise that the note is
   * restorable, and an uncommitted note would be gone for good. The row omits the
   * button rather than showing one that destroys.
   */
  onDelete?: (path: string, title: string) => void
  /** File a note into `folder` — '' is the vault root. */
  onMove: (from: string, folder: string) => void
  /** The row currently in inline-rename mode, if any. */
  renamingPath: string | null
  /** The row whose delete request is in flight, if any. */
  deletingPath: string | null
  onRenameStart: (path: string) => void
  onRenameEnd: () => void
  onRename: (path: string, nextName: string) => void
}
