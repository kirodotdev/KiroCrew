import { File, FileCode, FileJson, FileText, Image, Paintbrush, Settings, Terminal, type LucideIcon } from 'lucide-react'

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

export function colorForExt(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return FILE_COLORS[ext] || 'text-muted'
}

/** Per-extension lucide icon component class. Single source of truth for the
 *  whole app — file chips, activity Files tab tiles, inline file browser, etc.
 *  Returns the component class (not a JSX element) so consumers can apply
 *  their own size and className. */
export const FILE_ICONS: Record<string, LucideIcon> = {
  // Code
  ts: FileCode, tsx: FileCode, js: FileCode, jsx: FileCode, py: FileCode, rs: FileCode,
  go: FileCode, java: FileCode, kt: FileCode, rb: FileCode, c: FileCode, cpp: FileCode, h: FileCode,
  // Data / config
  json: FileJson, yaml: Settings, yml: Settings, toml: Settings, ini: Settings,
  // Docs
  md: FileText, mdx: FileText, txt: FileText, csv: FileText, log: FileText,
  // Styling
  css: Paintbrush, scss: Paintbrush, less: Paintbrush,
  // Images
  png: Image, jpg: Image, jpeg: Image, gif: Image, svg: Image, webp: Image,
  // Shell
  sh: Terminal, bash: Terminal, zsh: Terminal,
}

export function fileIcon(path: string): LucideIcon {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return FILE_ICONS[ext] || File
}
