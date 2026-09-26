import {
  BookOpen,
  Box,
  CalendarDays,
  Database,
  File,
  FileArchive,
  FileAudio,
  FileCode,
  FileCog,
  FileDiff,
  FileImage,
  FileJson,
  FileKey,
  FileSpreadsheet,
  FileTerminal,
  FileText,
  FileType,
  FileVideo,
  Image,
  NotebookText,
  Package,
  Paintbrush,
  Presentation,
  Settings,
  Terminal,
  type LucideIcon,
} from 'lucide-react'

/** Per-extension color tokens for file-type icons in tiles, lists, the inline browser, and chips. */
export const FILE_COLORS: Record<string, string> = {
  // TypeScript / JavaScript
  ts: 'text-blue-400',
  tsx: 'text-blue-400',
  js: 'text-yellow-400',
  jsx: 'text-yellow-400',
  // Python
  py: 'text-green-500',
  // Rust / Go
  rs: 'text-orange-500',
  go: 'text-cyan-500',
  // JVM
  java: 'text-red-500',
  kt: 'text-purple-400',
  // Ruby / PHP
  rb: 'text-red-400',
  php: 'text-indigo-400',
  // C / C++
  c: 'text-blue-300',
  h: 'text-blue-300',
  cpp: 'text-blue-300',
  hpp: 'text-blue-300',
  // Web
  css: 'text-pink-400',
  scss: 'text-pink-400',
  html: 'text-orange-400',
  // Config / data
  json: 'text-yellow-300',
  yaml: 'text-purple-300',
  yml: 'text-purple-300',
  toml: 'text-purple-300',
  // Docs
  md: 'text-emerald-400',
  txt: 'text-muted',
  log: 'text-muted',
  // Shell
  sh: 'text-green-400',
  bash: 'text-green-400',
}

export type FileFamily =
  | 'document'
  | 'spreadsheet'
  | 'slides'
  | 'image'
  | 'video'
  | 'audio'
  | 'code'
  | 'data'
  | 'log'
  | 'archive'
  | 'key'
  | 'installer'
  | 'database'
  | 'patch'
  | 'font'
  | 'config'
  | 'notebook'
  | 'model3d'
  | 'ebook'
  | 'calendar'
  | 'unknown'

const FAMILY_ICONS: Record<FileFamily, LucideIcon> = {
  document: FileText,
  spreadsheet: FileSpreadsheet,
  slides: Presentation,
  image: FileImage,
  video: FileVideo,
  audio: FileAudio,
  code: FileCode,
  data: FileJson,
  log: FileTerminal,
  archive: FileArchive,
  key: FileKey,
  installer: Package,
  database: Database,
  patch: FileDiff,
  font: FileType,
  config: FileCog,
  notebook: NotebookText,
  model3d: Box,
  ebook: BookOpen,
  calendar: CalendarDays,
  unknown: File,
}

const FAMILY_TONES: Record<FileFamily, string> = {
  document: 'text-info',
  spreadsheet: 'text-ok',
  slides: 'text-warn',
  image: 'text-accent',
  video: 'text-accent',
  audio: 'text-accent',
  code: 'text-text',
  data: 'text-text',
  log: 'text-muted',
  archive: 'text-muted',
  key: 'text-warn',
  installer: 'text-muted',
  database: 'text-muted',
  patch: 'text-ok',
  font: 'text-muted',
  config: 'text-muted',
  notebook: 'text-warn',
  model3d: 'text-accent',
  ebook: 'text-info',
  calendar: 'text-info',
  unknown: 'text-muted',
}

/** One extension-to-family table shared by every file-icon surface. */
const extensionFamilies: [FileFamily, RegExp][] = [
  ['document', /^(?:pdf|doc|docx|odt|rtf|txt|md|mdx|markdown|pages)$/],
  ['spreadsheet', /^(?:xls|xlsx|xlsm|csv|tsv|ods|numbers)$/],
  ['slides', /^(?:ppt|pptx|odp)$/],
  ['image', /^(?:png|jpg|jpeg|gif|webp|svg|heic|heif|bmp|tiff|tif|avif|ico)$/],
  ['video', /^(?:mp4|mov|webm|mkv|avi|m4v)$/],
  ['audio', /^(?:mp3|wav|m4a|flac|ogg|aac|opus)$/],
  ['code', /^(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|java|kt|swift|c|h|cpp|hpp|cs|rb|php|sh|bash|zsh|ps1|html|css|scss|less|vue|svelte)$/],
  ['data', /^(?:json|yaml|yml|toml|xml|jsonl|ndjson)$/],
  ['log', /^(?:log|out|err|trace)$/],
  ['archive', /^(?:zip|tar|gz|tgz|bz2|xz|7z|rar|zst)$/],
  ['key', /^(?:pem|key|crt|cer|der|p12|pfx|pub|asc|gpg)$/],
  ['installer', /^(?:dmg|pkg|exe|msi|deb|rpm|apk|appimage|whl)$/],
  ['database', /^(?:sqlite|sqlite3|db|sql|parquet)$/],
  ['patch', /^(?:patch|diff)$/],
  ['font', /^(?:ttf|otf|woff|woff2)$/],
  ['config', /^(?:env|ini|conf|cfg|properties)$/],
  ['notebook', /^(?:ipynb|rmd)$/],
  ['model3d', /^(?:stl|obj|glb|gltf|fbx|dwg|dxf)$/],
  ['ebook', /^(?:epub|mobi|azw3)$/],
  ['calendar', /^(?:ics|vcf)$/],
]

/** Keep existing fine-grained glyphs while sharing the family table. */
const iconOverrides: Record<string, LucideIcon> = {
  json: FileJson,
  yaml: Settings,
  yml: Settings,
  toml: Settings,
  ini: Settings,
  md: FileText,
  mdx: FileText,
  txt: FileText,
  csv: FileText,
  log: FileText,
  css: Paintbrush,
  scss: Paintbrush,
  less: Paintbrush,
  png: Image,
  jpg: Image,
  jpeg: Image,
  gif: Image,
  svg: Image,
  webp: Image,
  sh: Terminal,
  bash: Terminal,
  zsh: Terminal,
}

export function fileExtension(path: string): string {
  const base = path.split('/').pop() ?? path
  const dot = base.lastIndexOf('.')
  if (dot <= 0) return base.startsWith('.') ? base.slice(1).toLowerCase() : ''
  return base.slice(dot + 1).toLowerCase()
}

export function fileFamilyForPath(path: string): FileFamily {
  const extension = fileExtension(path)
  return extensionFamilies.find(([, pattern]) => pattern.test(extension))?.[0] ?? 'unknown'
}

export function fileIconForFamily(family: FileFamily): LucideIcon {
  return FAMILY_ICONS[family]
}

export function fileToneForFamily(family: FileFamily): string {
  return FAMILY_TONES[family]
}

export function colorForExt(path: string): string {
  return FILE_COLORS[fileExtension(path)] || 'text-muted'
}

/** Single icon lookup used by file chips, tiles, browsers and attachment cards. */
export function fileIcon(path: string): LucideIcon {
  const extension = fileExtension(path)
  return iconOverrides[extension] ?? FAMILY_ICONS[fileFamilyForPath(path)]
}
