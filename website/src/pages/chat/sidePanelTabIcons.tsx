import type { ReactNode } from 'react'
import { FileText, FileDiff, Bot, Workflow, ScrollText, MessageCircleQuestionMark, TerminalSquare, GitCompare, GitPullRequest, GitBranch, History, PanelRight, Globe, CircleDot, Folder, Folders, Link as LinkIcon, Layers, ListTree, Pin, Component } from 'lucide-react'
import type { TabKind } from '../../hooks/usePanelTabs'
import { panelTabDescriptor, isPanelTabKind, type PanelTabDescriptor } from '../../hooks/panelTabRegistry'
import { appIcon } from '../../apps/appIcons'

// Every non-app tab kind maps to a glyph; app-contributed kinds (`app:<…>`) are
// excluded so this stays an EXHAUSTIVE map a forgotten built-in fails to satisfy
// — their icon comes from the manifest descriptor via `iconForKind` instead.
type BuiltinTabKind = Exclude<TabKind, `app:${string}`>
const KIND_ICON: Record<BuiltinTabKind, ReactNode> = {
  'command-center': <PanelRight size={16} />,
  changes: <GitPullRequest size={16} />, issues: <CircleDot size={16} />, files: <Folders size={16} />, links: <LinkIcon size={16} />, artifacts: <Component size={16} />, subagents: <Bot size={16} />, workflows: <Workflow size={16} />,
  logs: <ScrollText size={16} />, crewlog: <History size={16} />, context: <Layers size={16} />, side: <MessageCircleQuestionMark size={16} />, terminal: <TerminalSquare size={16} />, browser: <Globe size={16} />,
  summary: <ListTree size={16} />,
  pins: <Pin size={16} />,
  file: <FileText size={16} />, diff: <GitCompare size={16} />, wtdiff: <FileDiff size={16} />, artifact: <Component size={16} />, folder: <Folder size={16} />,
  app: <PanelRight size={16} />, git: <GitBranch size={16} />,
}

/** The strip/menu glyph for a tab kind. A built-in reads `KIND_ICON`; an
 *  app-contributed kind resolves its manifest lucide icon NAME through the
 *  app-facing icon set, falling back to a generic panel glyph. */
export function iconForKind(kind: TabKind, descriptors: readonly PanelTabDescriptor[]): ReactNode {
  if (isPanelTabKind(kind)) {
    return appIcon(panelTabDescriptor(kind, descriptors)?.icon)
  }
  return KIND_ICON[kind]
}
