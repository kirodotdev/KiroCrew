/**
 * Icon names an APP may name in its manifest.
 *
 * Deliberately separate from `builtinIcons.tsx`. That registry is seeded by core
 * and extended only through `registerBuiltinIcons()`, which its own header
 * documents as read at module-load during edition composition — so a
 * runtime-installed app can never register into it, and resolving a manifest icon
 * through it would silently render the fallback for every app that is not part of
 * the build. This map is the app-reachable counterpart: a fixed, documented set
 * that any installed app can name without registering anything.
 *
 * Bounded rather than a lookup over lucide's full export on purpose: the whole
 * icon set is ~1k components, and importing it to resolve one manifest string
 * would put all of them in the bundle. Names are PascalCase, matching the
 * `lucide-react` component names and `ui.pages[].icon`.
 *
 * The set is documented in `docs/app-kit/manifest-reference.md`; adding a name
 * here is what widens it, and the doc table must move in the same commit.
 */
import {
  Activity,
  Bell,
  BookOpen,
  Bot,
  Boxes,
  Bug,
  Cloud,
  Code,
  Database,
  FileText,
  Files,
  Folder,
  FolderTree,
  GitBranch,
  Globe,
  Inbox,
  Layers,
  Link,
  ListTodo,
  MessageSquare,
  Package,
  PanelRight,
  Pin,
  Search,
  Settings,
  Shield,
  Sparkles,
  Star,
  Table,
  Tag,
  Terminal,
  Users,
  Wrench,
  Zap,
} from 'lucide-react'
import type { ReactElement } from 'react'

const APP_ICONS: Record<string, ReactElement> = {
  Activity: <Activity size={16} />,
  Bell: <Bell size={16} />,
  BookOpen: <BookOpen size={16} />,
  Bot: <Bot size={16} />,
  Boxes: <Boxes size={16} />,
  Bug: <Bug size={16} />,
  Cloud: <Cloud size={16} />,
  Code: <Code size={16} />,
  Database: <Database size={16} />,
  FileText: <FileText size={16} />,
  Files: <Files size={16} />,
  Folder: <Folder size={16} />,
  FolderTree: <FolderTree size={16} />,
  GitBranch: <GitBranch size={16} />,
  Globe: <Globe size={16} />,
  Inbox: <Inbox size={16} />,
  Layers: <Layers size={16} />,
  Link: <Link size={16} />,
  ListTodo: <ListTodo size={16} />,
  MessageSquare: <MessageSquare size={16} />,
  Package: <Package size={16} />,
  PanelRight: <PanelRight size={16} />,
  Pin: <Pin size={16} />,
  Search: <Search size={16} />,
  Settings: <Settings size={16} />,
  Shield: <Shield size={16} />,
  Sparkles: <Sparkles size={16} />,
  Star: <Star size={16} />,
  Table: <Table size={16} />,
  Tag: <Tag size={16} />,
  Terminal: <Terminal size={16} />,
  Users: <Users size={16} />,
  Wrench: <Wrench size={16} />,
  Zap: <Zap size={16} />,
}

/** Every name an app may use, for docs and tests. */
export const APP_ICON_NAMES: readonly string[] = Object.keys(APP_ICONS)

/**
 * The glyph for a manifest icon name, or the generic panel glyph when the name is
 * absent or not in the set. Falls back rather than throwing: the name is
 * third-party data, and a tab that renders with the wrong glyph is a better
 * outcome than a tab that does not render.
 */
export function appIcon(name: string | undefined): ReactElement {
  return (name && APP_ICONS[name]) || <PanelRight size={16} />
}
