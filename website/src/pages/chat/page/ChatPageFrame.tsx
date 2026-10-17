import type { RefObject, ReactNode } from 'react'
import { SidebarFolderCtx, ClosedSessionCtx, type ClosedSessionActions, type SidebarFolderActions } from '../../../components/markdown/contexts'
import { RowDisclosureProvider } from '../rowDisclosure'
import { RunInTerminalScope } from '../../../components/runInTerminalScope'
import { JiraHostsCtx } from '../../../lib/jiraHosts'
import type { ChatConfig } from '../ChatSettings'
import { SidePanelDockHost } from '../../../components/SidePanelGlyph'
import { TagPopoverProvider } from '../../../hooks/useTagPopover'

interface ChatPageFrameProps {
  embedded?: boolean
  activeSlot: string | null
  chatConfig: ChatConfig
  jiraSourceHosts: string[]
  runInTerminalScope: string
  activitySlot: HTMLElement | null
  chatContainerRef: RefObject<HTMLDivElement>
  closedSessionActions: ClosedSessionActions
  sidebarFolderActions: SidebarFolderActions
  children: ReactNode
}

/**
 * The page's frame: the contexts every surface inside it reads (run-in-terminal scope, row disclosure, the side panel's dock host, tag popovers, Jira hosts, the sidebar folder and closed-session actions) and the root element both drawer swipes bind to, which carries the message font size the side panel inherits.
 */
export default function ChatPageFrame({
  embedded, activeSlot, chatConfig, jiraSourceHosts, runInTerminalScope, activitySlot, chatContainerRef, closedSessionActions, sidebarFolderActions, children,
}: ChatPageFrameProps) {
  return (
    <RunInTerminalScope.Provider value={runInTerminalScope}>
    <RowDisclosureProvider resetKey={activeSlot}>
    {/* The side panel can dock below the chat only while the shell's activity
        bar hosts it; the inline panel (mobile, embed, popout) always opens
        right, so every panel glyph on this page resolves against that. */}
    <SidePanelDockHost value={!!activitySlot}>
    <TagPopoverProvider>
    {/* Self-hosted Jira allowlist for every markdown anchor in the page --
        message bodies, previews, and panels alike -- so a pasted Jira URL
        chips identically wherever it renders. Cloud URLs need no provider. */}
    <JiraHostsCtx.Provider value={jiraSourceHosts}>
    <SidebarFolderCtx.Provider value={sidebarFolderActions}>
    <ClosedSessionCtx.Provider value={closedSessionActions}>
    <div
      ref={chatContainerRef}
      /* Both sides are this page's own: a rightward drag opens the sessions
         drawer and a leftward one the activity panel, so the app-wide nav
         gesture bound on the shell must not also arm here. Declared on the SAME
         element the two instances below bind, which is what lets them read the
         claim as their own and proceed.

         Withheld when `embedded`, because that is exactly when this page binds
         NOTHING (both instances are gated on `!embedded`) — and an embedded chat
         renders INSIDE the shell (the artifact companion, the Papyrus co-author
         panel, an app SDK panel), at full width on mobile. A claim that outlives
         its ownership there suppressed the nav swipe while serving nothing: a
         dead gesture across the whole screen, on the chat-shaped surface where
         the gesture was most likely to be tried. The claim has to track what is
         actually bound, or the fail-open default is defeated by the one page
         that declares.

         Not also gated on `isMobile`: the app-wide instance is mobile-only, so a
         desktop claim suppresses nothing, and adding the term would imply this
         attribute carries a guarantee about a case it cannot affect. */
      data-owns-swipe={embedded ? undefined : 'left right'}
      className="flex flex-1 min-h-0 h-full overflow-hidden relative"
      /* Published here, not on the chat pane, because the side panel (Activity,
         Browser, SideChat's composer and follow-up chips) is this element's
         child and the chat pane's SIBLING. Anything that reads the setting via
         `var(--mc-message-font-size, 14px)` from inside the panel would
         otherwise silently take the fallback and look right at the default
         only. Content width stays on the pane: it is a column geometry, and the
         panel has its own. */
      style={{ '--mc-message-font-size': `${chatConfig.messageFontSize}px` } as React.CSSProperties}
    >
      {children}
    </div>
    </ClosedSessionCtx.Provider>
    </SidebarFolderCtx.Provider>
    </JiraHostsCtx.Provider>
    </TagPopoverProvider>
    </SidePanelDockHost>
    </RowDisclosureProvider>
    </RunInTerminalScope.Provider>
  )
}
