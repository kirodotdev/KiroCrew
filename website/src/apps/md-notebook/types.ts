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
