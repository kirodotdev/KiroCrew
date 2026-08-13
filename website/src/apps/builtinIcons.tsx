/**
 * Builtin App Nav-Icon Registry
 *
 * Maps an app manifest's `ui.icon` name (a string) to the rendered Lucide
 * element shown in the left-nav rail for builtin apps. Lifting this out of
 * App.tsx makes it an extension seam: a downstream edition that bundles its
 * own builtin apps registers their icons via `registerBuiltinIcons()` from its
 * entry module instead of editing App.tsx on every upstream sync.
 *
 * App.tsx reads it through `getBuiltinIcon(name)`, so adding an app's icon
 * never requires touching App.tsx.
 *
 * Scope: registration is expected at module-load time (edition composition),
 * before App mounts — this registry is not reactive, so registering after the
 * nav has rendered will not appear until the next unrelated re-render.
 */
import {
  Users,
  Inbox,
  Gamepad2,
  MessageSquareDot,
  ClipboardCheck,
  BookOpen,
  BookOpenText,
  Brain,
  FolderTree,
  FlaskConical,
  ScanSearch,
  ScrollText,
  Contact,
  ShoppingBag,
} from 'lucide-react'
import type { ReactElement } from 'react'
import { reportSeamCollision } from './seamCollision'

/**
 * Registry mapping manifest icon names to rendered nav icons. The core's own
 * builtin apps seed it; downstream bundles extend it via
 * `registerBuiltinIcons()`.
 */
const BUILTIN_ICON_REGISTRY: Record<string, ReactElement> = {
  Users: <Users size={16} />,
  Inbox: <Inbox size={16} />,
  Gamepad2: <Gamepad2 size={16} />,
  MessageSquareDot: <MessageSquareDot size={16} />,
  ClipboardCheck: <ClipboardCheck size={16} />,
  BookOpen: <BookOpen size={16} />,
  BookOpenText: <BookOpenText size={16} />,
  Brain: <Brain size={16} />,
  FolderTree: <FolderTree size={16} />,
  FlaskConical: <FlaskConical size={16} />,
  ScanSearch: <ScanSearch size={16} />,
  ScrollText: <ScrollText size={16} />,
  Contact: <Contact size={16} />,
  ShoppingBag: <ShoppingBag size={16} />,
}

/**
 * Register additional manifest-icon-name → rendered-icon mappings at runtime.
 * Duplicate names are ignored (core registrations win) and log a warning.
 */
export function registerBuiltinIcons(entries: Record<string, ReactElement>): void {
  for (const [name, element] of Object.entries(entries)) {
    if (name in BUILTIN_ICON_REGISTRY) {
      reportSeamCollision('builtinIcons', `icon ${name} already registered; ignoring duplicate`)
      continue
    }
    BUILTIN_ICON_REGISTRY[name] = element
  }
}

/** Resolve a manifest icon name to its rendered nav icon, or undefined. */
export function getBuiltinIcon(name: string): ReactElement | undefined {
  return BUILTIN_ICON_REGISTRY[name]
}
