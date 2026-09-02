export interface TreeEntry {
  name: string
  path: string
  type: 'file' | 'dir' | 'symlink' | 'missing' | 'error' | 'truncated' | 'other'
  size?: number
  mtime?: number
  isGitRoot?: boolean
  children?: TreeEntry[]
  loading?: boolean
  error?: string
}

export interface FolderTab {
  id: string
  rootPath: string
  label: string
  expanded: Record<string, boolean>
  showSearch: boolean
}

export interface FileTab {
  id: string
  path: string
  folderId: string
}

export interface FileMeta {
  size?: number
  mtime?: number
  mime?: string
  encoding?: string
  binary?: boolean
  truncated?: boolean
  content?: string
}

export interface GitInfo {
  repoRoot: string
  branch?: string
  statuses: Record<string, string>
}

export interface SearchResult {
  file: string
  line: number
  col: number
  preview: string
}
