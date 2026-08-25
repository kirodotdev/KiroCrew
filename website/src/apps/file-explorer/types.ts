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
  /** Nanosecond mtime token from /read — lossless concurrency guard. */
  mtime_ns?: number
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

// ── Structured Office extraction (/extract) ──

export interface DocxBlock {
  type: string // 'p' | 'h1'..'h6' | 'table'
  text?: string
  rows?: string[][]
}

export interface XlsxSheet {
  name: string
  rows: string[][]
  truncated?: boolean
}

export interface PptxRun {
  t: string
  b?: boolean
  i?: boolean
  sz?: number
  c?: string
}

export interface PptxParagraph {
  algn: string
  lvl: number
  bullet: boolean
  runs: PptxRun[]
}

export interface PptxShape {
  kind: 'text' | 'image' | 'table'
  x?: number
  y?: number
  w?: number
  h?: number
  paras?: PptxParagraph[]
  fill?: string
  member?: string
  rows?: string[][]
}

export interface PptxSlide {
  n: number
  bg: string | null
  shapes: PptxShape[]
  lines: string[]
}

export interface OfficeExtract {
  kind: 'docx' | 'xlsx' | 'pptx'
  blocks?: DocxBlock[]
  sheets?: XlsxSheet[]
  slides?: PptxSlide[]
  slideW?: number
  slideH?: number
  truncated?: boolean
  path: string
  size: number
  mtime: number
}

export interface WriteResult {
  mtime_ns?: number
  ok: boolean
  size: number
  mtime: number
}
