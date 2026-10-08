import type React from 'react'
import { createContext } from 'react'

/**
 * The React contexts the markdown pipeline's modules share.
 *
 * The six exported through `MarkdownRenderer.tsx` are public API: callers wrap
 * the renderer in their providers and the element overrides consume them. Each
 * is created exactly once, here, so a provider and the component reading it can
 * never hold two different objects. The other four carry the renderer root's
 * (and `MdAnchor`'s) state down to overrides that react-markdown instantiates
 * deep inside its own tree, where no prop can reach. The one context kept out of
 * this module, `MediaApprovedCtx`, is private to `remoteMedia.tsx`: its provider
 * and its only reader are both there.
 */

/** Context providing the viewed file's directory path for resolving bare relative image paths. */
export const BasePathCtx = createContext<string | null>(null)

/**
 * When true, markdown images render as small previews (a compact thumbnail the
 * user can still click to open the full-size lightbox) instead of the default
 * large inline size. User-message ("sent prompt") rendering turns this on so
 * an attached screenshot doesn't dominate the bubble, while assistant/response
 * images keep the full inline size. Default false = full size.
 */
export const CompactImagesCtx = createContext<boolean>(false)

/**
 * When true, every fenced code block in the subtree renders copy-only (the
 * plain CodeBlock), as the `readOnlyCode` prop does for one renderer. For a
 * transcript whose code is another machine's (a crew window): Edit and Run in
 * terminal would act on THIS machine. Default false.
 */
export const ReadOnlyCodeCtx = createContext<boolean>(false)

/**
 * A per-message token appended to local image URLs.
 *
 * `/api/file-raw?path=…` addresses a file by PATH, so every impression of a file
 * an agent rewrites across turns resolves to one URL — and a browser treats one
 * URL in one document as one resource. The second `<img>` is then served from the
 * in-document memory cache with no network request at all, so the new message
 * paints the OLD bytes. Measured in Chrome: without a distinct URL the edited
 * file is never re-fetched, and no HTTP cache header changes that — `ETag`,
 * `Cache-Control: no-cache` and even `no-store` are not consulted, because the
 * request is never made.
 *
 * Making the URL per-message gives each impression its own cache entry, so a new
 * message shows the current bytes while an earlier one keeps what it fetched.
 * Stable within a message, so re-renders and streaming do not re-request.
 */
export const ImageVersionCtx = createContext<string | null>(null)

/** The exact markdown string handed to ReactMarkdown, so components can map a
 *  node's source position back to the original text. ImgWithFallback uses it
 *  to see whether an image destination was `<…>`-wrapped — micromark strips
 *  the wrap and percent-encodes BOTH forms identically, so the parsed url
 *  alone cannot distinguish producer-encoded content from a legacy raw path
 *  that happens to contain `%XX` (which must be preserved verbatim). */
export const MdSourceCtx = createContext<string | null>(null)

/**
 * Per-consumer override for rendered markdown LINKS.
 *
 * A provider returns its own element for the hrefs it wants to own, or null to
 * fall through to the default anchor. Issue Radar uses it to render same-repo
 * issue/PR references as in-app affordances (dashed accent underline + hover
 * preview) without the renderer knowing anything about issues — and without any
 * consumer having to post-process React-owned DOM.
 *
 * Only the anchor is delegated; the surrounding markdown pipeline is untouched.
 */
export type LinkOverride = (link: { href: string; children: React.ReactNode }) => React.ReactNode | null
export const LinkOverrideCtx = createContext<LinkOverride | null>(null)

/**
 * Link-unfurl gate for the markdown subtree.
 *
 * `enabled` mirrors `cfg.dashboard.link_previews` (default OFF): the user has to
 * opt in before this machine will fetch a URL the model wrote.
 *
 * `live` means the block is STILL STREAMING. It is a hard, independent gate: a
 * URL in the streaming tail may be half-typed (`https://exa`), and resolving
 * that would send the model's in-progress text to a host nobody named. Nothing
 * is fetched while `live` is true — the chip/card simply appears once the block
 * settles.
 *
 * Both default to false, so any markdown rendered outside a provider (file
 * previews, artifact pages, app-embedded chat) keeps today's plain anchors.
 */
export interface LinkUnfurl {
  enabled: boolean
  live: boolean
}
export const LinkUnfurlCtx = createContext<LinkUnfurl>({ enabled: false, live: false })

/**
 * True for the markdown subtree rendered INSIDE an anchor's own text.
 *
 * `InlineCode` consults it so a code span used as a link label —
 * ``[`https://example.com/x`](https://example.com/x)`` — stays inert instead of
 * becoming a click-to-copy chip. The chip's handler calls `preventDefault`, and
 * that cancels the anchor's default action from anywhere in propagation, so
 * without this the label copied and the link silently stopped navigating (a
 * regression from #4433, which gave non-path spans a primary-click copy).
 *
 * Provided only where `MdAnchor` places `children` inside an `<a>`. The Jira and
 * forge chips render a parsed label instead of `children`, and a `LinkOverride`
 * owns its element outright, so neither needs it.
 */
export const InsideLinkCtx = createContext(false)

/**
 * Whether inline-code chips may issue stat probes.
 *
 * False while a message streams. Mid-stream a path arrives one chunk at a time,
 * and the prefixes are themselves valid candidates — `/Users` is a real
 * directory on the way to `/Users/me/project/file.ts` — so probing every chunk
 * would burn requests and briefly render the wrong affordance before settling.
 * Chips stay inert until the text stops moving.
 */
export const PathProbeCtx = createContext<boolean>(true)

/**
 * Where a confirmed path chip sends its activation.
 *
 * A context because `MD_COMPONENTS` is module-level — the `code` renderer cannot
 * receive MarkdownRenderer's props directly. Both handlers are optional: most of
 * the ~30 MarkdownRenderer call sites pass neither, and those fall back to the
 * OS file manager.
 */
export type PathActions = { onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void; onFolderOpen?: (path: string) => void }
export const PathActionCtx = createContext<PathActions>({})

/**
 * Where a session chip sends its activation, plus the roster that decides whether
 * a chip is offered at all.
 *
 * `sessions` ABSENT is deliberately not the same as an empty map: a caller that
 * never wired it (most of the ~30 call sites) does not KNOW which sessions exist,
 * so no chip is offered. An empty map is the opposite claim — a caller that does
 * know, and has nothing open.
 *
 * The value is the display title, for the tooltip only. It is never substituted
 * for the chip's text, which would make the visible span disagree with what
 * Ctrl+click copies.
 */
export type SessionActions = {
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
  /** Epoch seconds this message was written at, when the host knows it. Only the
   *  SHORT-name lookup uses it, to refuse a slot minted after the text naming it
   *  (see `sessionKeyFromShort`). Absent on surfaces that render markdown with no
   *  message identity, and there NO short name resolves — the full key still does. */
  writtenAtEpoch?: number
}
export const SessionActionCtx = createContext<SessionActions>({})

/**
 * Where a SIDEBAR-folder chip sends its activation, plus the folder list that
 * decides whether a chip is offered at all.
 *
 * The sidebar's folder tree, not a filesystem directory: `PathActions.onFolderOpen`
 * browses a directory on disk, this reveals a row in the chat sidebar. An agent
 * that files a team into `goal/worker` names that folder in prose by its human
 * path -- the same `/`-joined spelling `chat_folder_tree` and `session_create`
 * take, or the ` › ` breadcrumb the header shows -- and the chip turns the name
 * into the click the sidebar's own rows already answer.
 *
 * Same absent-vs-empty rule as `SessionActions.sessions`: `folders` ABSENT means
 * the host does not know the tree (most MarkdownRenderer call sites), so no chip;
 * an empty list is a host that knows there are none. Provided ONCE by the page,
 * not threaded through MarkdownRenderer props: the folder tree is page state,
 * and every renderer under the chat page -- message bodies, subagent and workflow
 * cards, system notices -- should offer the same chip without each re-plumbing it.
 */
export type SidebarFolderActions = {
  /** Reveal the folder in the sidebar: expand its ancestors, scroll to it, flash it. */
  onFolderReveal?: (folderId: string) => void
  /** The sidebar's folder rows, from the shared `['chat-folders']` query. */
  folders?: readonly { id: string; name: string; parent_id?: string }[]
}
export const SidebarFolderCtx = createContext<SidebarFolderActions>({})
