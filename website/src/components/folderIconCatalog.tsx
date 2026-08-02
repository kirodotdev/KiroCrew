import { BookOpen, Bot, Brain, Briefcase, Bug, Calendar, ChartColumn, CircleCheck, ClipboardList, Cloud, Code, Cpu, Database, FlaskConical, Flame, Gamepad2, Globe, GraduationCap, Hammer, Heart, House, KeyRound, Layers, Lightbulb, Lock, Map, Microscope, Music, Package, Palette, PartyPopper, PenLine, Pin, Puzzle, Rainbow, Rocket, Search, Settings, Shield, Sparkles, Star, Target, Terminal, Trophy, Users, Wrench, Zap, type LucideIcon } from 'lucide-react'
import { i18nT } from '../i18n/t'

/** Curated lucide icon vocabulary for the chat-folder `icon` field.
 *
 *  Values are stored as `lucide:<name>` (kebab-case lucide name). The field is
 *  data-model only — the sidebar glyph renders the folder's palette color, not
 *  an icon — and this catalog backs the boot migration's emoji conversion
 *  targets and the PATCH/regenerate validation surface.
 *
 *  KEEP IN SYNC with `_FOLDER_LUCIDE_NAMES` in
 *  src/kiro_crew/dashboard/chat_folders.py — the backend allowlist that
 *  validates PATCHed icons and constrains the migration generator. */
export const FOLDER_LUCIDE_ICONS: Record<string, LucideIcon> = {
  'rocket': Rocket,
  'globe': Globe,
  'briefcase': Briefcase,
  'house': House,
  'star': Star,
  'flame': Flame,
  'target': Target,
  'lightbulb': Lightbulb,
  'sparkles': Sparkles,
  'zap': Zap,
  'trophy': Trophy,
  'heart': Heart,
  'bug': Bug,
  'wrench': Wrench,
  'flask-conical': FlaskConical,
  'terminal': Terminal,
  'code': Code,
  'cpu': Cpu,
  'database': Database,
  'cloud': Cloud,
  'shield': Shield,
  'key-round': KeyRound,
  'package': Package,
  'layers': Layers,
  'settings': Settings,
  'search': Search,
  'chart-column': ChartColumn,
  'book-open': BookOpen,
  'graduation-cap': GraduationCap,
  'pen-line': PenLine,
  'palette': Palette,
  'music': Music,
  'gamepad-2': Gamepad2,
  'map': Map,
  'calendar': Calendar,
  'users': Users,
  'clipboard-list': ClipboardList,
  'circle-check': CircleCheck,
  'microscope': Microscope,
  'brain': Brain,
  'hammer': Hammer,
  'lock': Lock,
  'rainbow': Rainbow,
  'party-popper': PartyPopper,
  'bot': Bot,
  'puzzle': Puzzle,
  'pin': Pin,
}

export const LUCIDE_ICON_PREFIX = 'lucide:'

/** Folder color palette — the identity mark a user picks for a folder in
 *  the config modal. Shares the Artifacts page's FOLDER_COLORS hues so the
 *  two folder systems speak one visual language. KEEP IN SYNC with
 *  `_FOLDER_COLOR_PALETTE` in src/kiro_crew/dashboard/chat_folders.py.
 *  Thunks with literal keys for the same static-resolvability reason as
 *  FOLDER_ICON_LABELS below. */
export const FOLDER_COLOR_PALETTE: { value: string; label: () => string }[] = [
  { value: '#ef4444', label: () => i18nT('components.folderColorNames.red') },
  { value: '#f97316', label: () => i18nT('components.folderColorNames.orange') },
  { value: '#f59e0b', label: () => i18nT('components.folderColorNames.amber') },
  { value: '#84cc16', label: () => i18nT('components.folderColorNames.lime') },
  { value: '#22c55e', label: () => i18nT('components.folderColorNames.green') },
  { value: '#14b8a6', label: () => i18nT('components.folderColorNames.teal') },
  { value: '#06b6d4', label: () => i18nT('components.folderColorNames.cyan') },
  { value: '#3b82f6', label: () => i18nT('components.folderColorNames.blue') },
  { value: '#6366f1', label: () => i18nT('components.folderColorNames.indigo') },
  { value: '#8b5cf6', label: () => i18nT('components.folderColorNames.violet') },
  { value: '#ec4899', label: () => i18nT('components.folderColorNames.pink') },
  { value: '#94a3b8', label: () => i18nT('components.folderColorNames.gray') },
]

/** Localized human display name per catalog icon, for tooltips/aria-labels —
 *  "Game controller", not the raw lucide identifier "gamepad-2". Thunks with
 *  literal keys (not `i18nT(\`…${name}\`)`) so every reference stays statically
 *  resolvable by the i18n key checker. KEEP IN SYNC with FOLDER_LUCIDE_ICONS. */
export const FOLDER_ICON_LABELS: Record<string, () => string> = {
  'rocket': () => i18nT('components.folderIconCatalog.rocket'),
  'globe': () => i18nT('components.folderIconCatalog.globe'),
  'briefcase': () => i18nT('components.folderIconCatalog.briefcase'),
  'house': () => i18nT('components.folderIconCatalog.house'),
  'star': () => i18nT('components.folderIconCatalog.star'),
  'flame': () => i18nT('components.folderIconCatalog.flame'),
  'target': () => i18nT('components.folderIconCatalog.target'),
  'lightbulb': () => i18nT('components.folderIconCatalog.lightbulb'),
  'sparkles': () => i18nT('components.folderIconCatalog.sparkles'),
  'zap': () => i18nT('components.folderIconCatalog.zap'),
  'trophy': () => i18nT('components.folderIconCatalog.trophy'),
  'heart': () => i18nT('components.folderIconCatalog.heart'),
  'bug': () => i18nT('components.folderIconCatalog.bug'),
  'wrench': () => i18nT('components.folderIconCatalog.wrench'),
  'flask-conical': () => i18nT('components.folderIconCatalog.flask-conical'),
  'terminal': () => i18nT('components.folderIconCatalog.terminal'),
  'code': () => i18nT('components.folderIconCatalog.code'),
  'cpu': () => i18nT('components.folderIconCatalog.cpu'),
  'database': () => i18nT('components.folderIconCatalog.database'),
  'cloud': () => i18nT('components.folderIconCatalog.cloud'),
  'shield': () => i18nT('components.folderIconCatalog.shield'),
  'key-round': () => i18nT('components.folderIconCatalog.key-round'),
  'package': () => i18nT('components.folderIconCatalog.package'),
  'layers': () => i18nT('components.folderIconCatalog.layers'),
  'settings': () => i18nT('components.folderIconCatalog.settings'),
  'search': () => i18nT('components.folderIconCatalog.search'),
  'chart-column': () => i18nT('components.folderIconCatalog.chart-column'),
  'book-open': () => i18nT('components.folderIconCatalog.book-open'),
  'graduation-cap': () => i18nT('components.folderIconCatalog.graduation-cap'),
  'pen-line': () => i18nT('components.folderIconCatalog.pen-line'),
  'palette': () => i18nT('components.folderIconCatalog.palette'),
  'music': () => i18nT('components.folderIconCatalog.music'),
  'gamepad-2': () => i18nT('components.folderIconCatalog.gamepad-2'),
  'map': () => i18nT('components.folderIconCatalog.map'),
  'calendar': () => i18nT('components.folderIconCatalog.calendar'),
  'users': () => i18nT('components.folderIconCatalog.users'),
  'clipboard-list': () => i18nT('components.folderIconCatalog.clipboard-list'),
  'circle-check': () => i18nT('components.folderIconCatalog.circle-check'),
  'microscope': () => i18nT('components.folderIconCatalog.microscope'),
  'brain': () => i18nT('components.folderIconCatalog.brain'),
  'hammer': () => i18nT('components.folderIconCatalog.hammer'),
  'lock': () => i18nT('components.folderIconCatalog.lock'),
  'rainbow': () => i18nT('components.folderIconCatalog.rainbow'),
  'party-popper': () => i18nT('components.folderIconCatalog.party-popper'),
  'bot': () => i18nT('components.folderIconCatalog.bot'),
  'puzzle': () => i18nT('components.folderIconCatalog.puzzle'),
  'pin': () => i18nT('components.folderIconCatalog.pin'),
}

/** Display name for a catalog icon: localized when known, raw name otherwise. */
export function folderIconLabel(name: string): string {
  return FOLDER_ICON_LABELS[name]?.() ?? name
}

/** Resolve a folder `icon` value to its lucide component, or null when the
 *  value is absent, a legacy emoji, an unknown lucide name, or (from a
 *  corrupt folders.json) not a string at all — the type annotation is not a
 *  runtime guarantee for JSON-loaded data, and a TypeError here would crash
 *  the whole sidebar render. */
export function lucideFolderIcon(icon?: string): LucideIcon | null {
  if (typeof icon !== 'string' || !icon.startsWith(LUCIDE_ICON_PREFIX)) return null
  return FOLDER_LUCIDE_ICONS[icon.slice(LUCIDE_ICON_PREFIX.length)] ?? null
}
