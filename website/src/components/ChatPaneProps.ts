import type { ComponentType } from 'react'
import type { ThreadHooks } from '../app-sdk/messageRenderers'
import type { ComposerBusyMode } from './ChatInput'
import type { ChatInputProps } from './chat-input/props'
import type { CrewmateIdentity } from '../pages/chat/CrewmateMessage'

/** What the top-left split pane does about the shell's sidebar toggle — see
 *  ChatPane's `leading` prop. */
export type PaneLeading = { inset?: boolean; control?: React.ReactNode }

/** The props of `ChatPane`: one live chat session inside a host's layout (a
 *  split-view pane, a crewmate's DM thread, a page-embedded chat). */
export interface ChatPaneProps {
  slotKey: string
  focused?: boolean
  onFocus?: () => void
  onRemove?: () => void
  onSplitRight?: () => void
  onSplitDown?: () => void
  /** Hands this pane's slot to the full session, leaving split view. Without it
   *  the earlier-messages row is hidden rather than shown inert. The optional ts
   *  anchors the destination near the pane's oldest message, not the newest. */
  onOpenFull?: (slot: string, anchorTs?: string, anchorMid?: string) => void
  /** The host declares the slot's agent server-pinned (member DM threads):
   *  the agent picker is not offered at all, instead of offering a control
   *  whose every selection the backend 409s. */
  agentLocked?: boolean
  /** The input the pane's composer draws, fed every ChatInput prop. Absent:
   *  the ordinary ChatInput. The Crewmates page passes its own CrewComposer,
   *  which reuses ChatInput without the session toolbar line. */
  composerInput?: ComponentType<ChatInputProps>
  /** Split view: this pane owns the surface's top-left corner, where the shell
   *  keeps the sessions-sidebar toggle. `inset` reserves that toggle's column
   *  (desktop: the toggle is the shell's absolutely positioned button, and the
   *  header would otherwise run under it); `control` renders the toggle inline
   *  (mobile, where the single-chat title row that normally carries it is not
   *  rendered in split view). Undefined = the header starts at its own inset. */
  leading?: PaneLeading
  /** Embedded-in-a-page mode (member DM threads): the HOST renders the
   *  identity header, so the pane's own title bar and its card chrome
   *  (border, rounded corners) would duplicate it. Split-view panes keep
   *  the chrome — there the bar IS the pane's identity. */
  frameless?: boolean
  /** Height, in px, of a host band that floats OVER the pane's top edge (the
   *  Members DM header, #18325): the scroller pads its rows below it, so at
   *  scroll top nothing hides under the band while the conversation still
   *  scrolls under it; the pinned-prompt band hangs under it too. Measured
   *  by the host, never a constant. 0 (default): the pane's top edge is the
   *  host's own content edge, as in a split pane under its title bar. */
  topInset?: number
  /** The pane follows the user's Content width setting (transcript AND
   *  composer, both halves of CONTENT_WIDTH), resolved from the pane's own
   *  live chatConfig. Defaults to false = both variables pinned to '100%':
   *  a split-view pane is already narrow, so capping inside it wastes
   *  width. A full-width host (the Members page's DM column) sets it so
   *  long transcripts keep the same user-configured measure as the main
   *  chat. */
  followContentWidth?: boolean
  /** This pane's composer owns the document-wide "Start Dictation with a Key"
   *  binding. Defaults to false: the key listens on the whole document, so
   *  only a host that shows ONE composer may set it (N armed panes would
   *  open N captures on one keystroke). The Members page's DM pane sets it;
   *  split-view panes leave it off and ChatPage keeps its own. */
  pushToTalk?: boolean
  /** Suppress the "Session ready. Type a message to start." hint on an empty
   *  transcript. A host that is showing its own verdict about this thread
   *  above the pane (the Members page's "Couldn't reconnect" notice) sets it,
   *  so the pane does not say "go" one line under a host that says "broken". */
  hideEmptyHint?: boolean
  /** ISO time of a fresh start on this thread. Rows written before it fold
   *  under one closed "Earlier conversation" divider the reader can open.
   *  Display only: the rows stay in the transcript. */
  foldBefore?: string
  /** Bring a Side Chat surface for this pane's slot on screen. The selection
   *  toolbar offers "Ask" only when the host provides it: the pane owns its
   *  composer (so Quote is always there) but no Side Chat of its own — the
   *  split view's lives in the chat page's activity panel, the Members page's
   *  in its detail drawer. Capability by omission, like `onOpenFull`. */
  openSideChat?: (slot: string) => boolean | void | Promise<boolean | void>
  /** Open a file in the host's file viewer, the way the main chat does.
   *  Without it an attachment card / @mention chip renders without an opener:
   *  the pane shows every attachment but cannot open one. Capability by
   *  omission, like `openSideChat`. */
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  /** Open an artifact named in a reply (`/artifacts/<slug>` link) in the
   *  host's side panel, the way the main chat does. Without it the link is a
   *  plain `target="_blank"` anchor to the standalone artifact view -- a
   *  crewmate's reply that names the artifact it just made then opens it in
   *  another browser tab, or nowhere when the shell swallows the popup
   *  (#18320). Capability by omission, like
   *  `onFileOpen`: the Members page wires its panel's artifact opener, a
   *  split-view pane passes nothing (its dock is `activeSlot`-keyed). */
  onArtifactOpen?: (slug: string) => void
  /** What the composer's send does while the slot is busy. Defaults to
   *  `'split'` — the same Steer/Queue split button as the main chat, which
   *  split-view (⌘D) panes keep: they are the main chat's own sessions seen
   *  side by side. A host that presents a conversation with ONE named peer
   *  may pass `'steer-only'`. A `crewmate` host instead uses `'responsive'`:
   *  always ordinary Send, with priority owned by the gateway. Other panes
   *  retain the explicit busy mode the host passed. */
  busyMode?: ComposerBusyMode
  /** This pane is a CREWMATE's chat (a member-mode slot on the Members page):
   *  the transcript shows only what the crewmate says to the user — the
   *  auto-nudge turns, cron and sub-agent envelopes, tool rows and say-nothing
   *  rows stay in the slot's history for the Work log but are not drawn — and
   *  the assistant rows draw as a run of bubbles under the crewmate's avatar
   *  (components/chat/crewmateBubbles). Undefined = an ordinary transcript;
   *  decided by the host, never inferred from the slot. */
  crewmate?: CrewmateIdentity
  /** A message from the crewmate the HOST draws after the last row (the
   *  Members page's warm resume / cold welcome, `mateGreeting.ts`). It sits in
   *  the transcript like a reply but is not one: never a slot row, never sent,
   *  so it costs no model call and the model never reads it back. Only
   *  meaningful with `crewmate`. */
  crewmateGreeting?: React.ReactNode
  /** Whether a person can see this crewmate's thread right now: the window is
   *  visible AND focused and nothing covers the chat. The unread landing marks
   *  a message read only while this is true; a reply landing in a parked or
   *  covered window stays unseen, so the next visit still lands on it. Default
   *  `true`. Only meaningful with `crewmate`. */
  crewmateShown?: boolean
  /** Focus the crewmate's Work log tab (the side-panel tab that holds what
   *  the filter hid). When given, the quiet hint's "where the work went" line
   *  is a link that opens it, so the words that read as a destination are one;
   *  without it the line is plain text. Only meaningful with `crewmate`. */
  onOpenCrewWorkLog?: () => void
  /** Reply threads on this pane's messages. Only a host presenting a
   *  crewmate's chat (the Members page) passes it; absent, the rows draw no
   *  thread footer and no "Reply in thread" action. */
  threads?: ThreadHooks
  /** Session routing for this pane's transcript: switch to the session a
   *  `/chat?sid=…` link, a slot-key chip or a short name names, plus the roster
   *  that decides whether such a chip is offered at all and the key the reader
   *  is already in. Capability by omission, like `onFileOpen`: a host that
   *  passes none of it renders those links exactly as before, because the
   *  markdown renderer gates on (`onSessionOpen` AND `sessions`) -- so a side
   *  chat and an embedded chat are unchanged. `sessions` must be WITHHELD, not
   *  emptied, while the host does not know which sessions exist (see
   *  `markdown/contexts.ts`), and it should hold only slots the handler can
   *  actually land on (`utils/sessionRoster.sessionTitleRoster`): a chip to a
   *  destination the switch clears is worse than plain text. */
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
}
