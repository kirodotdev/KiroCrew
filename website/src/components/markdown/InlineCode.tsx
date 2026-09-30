import React, { useContext } from 'react'
import { Check, ExternalLink, Folder, MessageSquare } from 'lucide-react'
import { fileIcon } from '../../utils/fileIcons'
import { wholeMatchAutolinkHref } from '../../utils/autolinkRules'
import { useGatewayPlatform, type GatewayPlatform } from '../../hooks/useGatewayPlatform'
import { useBranding } from '../../hooks/useBranding'
import { InstantTip, useInstantTip } from '../InstantTip'
import ErrorNotice from '../ErrorNotice'
import FilePathMenu, { useRevealFailure } from '../FilePathMenu'
import { i18nT } from '../../i18n/t'
import { InsideLinkCtx, PathActionCtx, PathProbeCtx, SessionActionCtx, SidebarFolderCtx } from './contexts'
import { activatePath, basenameOf, usePathResolution } from './pathReferences'
import { folderSegmentsOf, resolveFolderChip, resolveSessionChip } from './linkTargets'
import { CopyFailedNotice, useCopiedFlash, useTitleCuedCopy } from './copyFeedback'
import { ELEMENT_OVERRIDES, isElementWithProps } from './elements'

/**
 * Inline `code` spans and the chips they become: a confirmed path or directory
 * (open, reveal, copy), an open session (switch, copy), a span that is wholly an
 * autolinked work item (link out), and otherwise a click-to-copy span.
 * `codeTextOf` decides which text a span can vouch for; every copy goes through
 * `copyFeedback`.
 */

/**
 * The text of an inline code span AS THE READER SEES IT — what its chip copies,
 * names itself after, and probes as a path — or `null` when that cannot be
 * vouched for from the tree, in which case there is no chip.
 *
 * Not `String(children)`: a raw-HTML `<code>npm <b>test</b></code>` arrives as
 * an array of nodes, which stringifies to "npm ,[object Object]". Not `textOf`
 * either: that is the heading-slug reader, where a `<br>` may vanish, but here
 * `<code>printf a<br>printf b</code>` RENDERS two lines, so the clipboard must
 * hold two lines — `printf aprintf b` is a different command.
 *
 * And not `textContent` over the raw subtree. The sanitizer admits `class` on
 * every element and the utilities are global, so `<span class="hidden">` (or
 * `sr-only`, `opacity-0`, …) hides text the reader never sees; an `<img>` shows
 * a picture, not its `alt`. Text like that must never ride into the clipboard
 * on a click that looked like "copy this command", and copying LESS than the
 * visible text would be a different lie. So the payload is vouched for, not
 * filtered: only text, a class-less `<br>` and class-less inline formatting
 * elements whose rendering IS their text (`VOUCHED_INLINE_TAGS`) count;
 * anything else — a styled child or break, an image, a media element, a
 * component — makes the whole span unvouchable, and it stays an inert,
 * selectable code span. (jsdom has no `innerText`, and `innerText` could not
 * see `sr-only` anyway. A block child such as `<details>` is no vector: the
 * HTML parser splits the code span around it.)
 */
const VOUCHED_INLINE_TAGS = new Set([
  'b', 'strong', 'i', 'em', 'u', 's', 'del', 'ins', 'mark', 'sub', 'sup', 'small',
  'kbd', 'samp', 'var', 'code', 'span', 'abbr', 'cite', 'dfn', 'time', 'bdi', 'bdo', 'wbr',
])
function codeTextOf(node: React.ReactNode): string | null {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) {
    let out = ''
    for (const child of node) {
      const text = codeTextOf(child)
      if (text === null) return null
      out += text
    }
    return out
  }
  if (isElementWithProps(node)) {
    // The class guard comes FIRST, before any tag is trusted: a styled `<br>`
    // (`<br class="hidden">`) renders no break, so the one command the reader
    // sees would copy as two lines with the second one live.
    const props = node.props as { className?: unknown; class?: unknown; children?: React.ReactNode }
    if (props.className != null || props.class != null) return null
    if (node.type === 'br') return '\n'
    // The renderer's own `strong` / `em` overrides render exactly their text.
    const vouched = typeof node.type === 'string'
      ? VOUCHED_INLINE_TAGS.has(node.type)
      : node.type === ELEMENT_OVERRIDES.strong || node.type === ELEMENT_OVERRIDES.em
    if (!vouched) return null
    return props.children == null ? '' : codeTextOf(props.children)
  }
  return null
}

/** What every inline-code chip shares: the geometry and the mono face that make
 *  a span read as CODE. Deliberately no text colour and no hover underline —
 *  those are the parts that tell a reader what a click will do, so each chip
 *  class adds its own (`CHIP_ACTIONABLE` below, or the plain code look of
 *  `CopyableCode`). One constant carried both for a long time, which dressed
 *  every copy chip as a link: readers clicked expecting navigation and got a
 *  silent clipboard write. */
const CHIP_BASE = 'bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono'

/** The look of a chip whose click NAVIGATES or OPENS something — a confirmed
 *  path, a session, an autolinked work item: the accent colour and the hover
 *  underline that links wear, the pointer hand, and (at each call site) a
 *  leading glyph. A chip that only copies must NOT use this: looking like a
 *  link is a promise to go somewhere. */
const CHIP_ACTIONABLE = `${CHIP_BASE} text-accent cursor-pointer hover:underline`

/** Geometry of a path chip's leading glyph, shared by the confirmed chip and by
 *  the reserve that stands in for it while the path is unconfirmed.
 *
 *  Both sites MUST read these two values, because equal width in every state is
 *  the whole mechanism: the glyph is an inline atom, so 16px (12px box + 4px
 *  margin) appearing mid-paragraph can push a line over and change the row's
 *  height. Measured in a browser at phone widths, that re-wrap costs 24px — one
 *  line — and it lands under a reader who is scrolling history, because a path
 *  is probed the first time its row mounts. Same rule the image reserve follows
 *  (`reservedImageStyle`): reserve the box before the async answer arrives, so
 *  the answer restyles instead of reflowing. */
const CHIP_GLYPH_SIZE = 12
const CHIP_GLYPH_GEOMETRY = 'inline align-middle mr-1'

/**
 * Invisible stand-in for the chip glyph, for a path-shaped span that is not (or
 * not yet) a confirmed path.
 *
 * It renders the same icon element at the same size and margin, so it occupies
 * the confirmed chip's width exactly rather than an approximation of it — the
 * geometry cannot drift because a different icon or a different margin would
 * have to be written at both sites. `opacity-0` rather than a blank span keeps
 * the line box identical too: an empty inline-block contributes a different
 * baseline than an svg does.
 *
 * Blank, deliberately NOT a dimmed glyph: `InlineCode`'s glyph is what tells a
 * reader at rest which paths the backend actually confirmed, and a placeholder
 * glyph would erase that distinction to buy nothing — the reserve only needs the
 * space, not a mark.
 */
function ChipGlyphReserve({ path }: { path: string }) {
  const Glyph = fileIcon(path)
  return <Glyph size={CHIP_GLYPH_SIZE} aria-hidden="true" className={`${CHIP_GLYPH_GEOMETRY} opacity-0`} />
}

/** The same stand-in for a FOLDER-shaped span (`goal/worker`) whose roster answer
 *  has not arrived; see the reserve note in `InlineCode`. The folder chip's glyph
 *  is always `Folder`, so this needs no path. */
function FolderGlyphReserve() {
  return <Folder size={CHIP_GLYPH_SIZE} aria-hidden="true" className={`${CHIP_GLYPH_GEOMETRY} opacity-0`} />
}

/**
 * The chip's hover instruction, naming the application shift+click will actually
 * open.
 *
 * `api.revealPath` runs on the GATEWAY, so the host to name is that one — a
 * dashboard opened from a Mac against a Linux gateway must not promise Finder.
 * Anything we could not read (the `'gateway'` sentinel a non-owner gets, a failed
 * probe, Linux with no single file manager) takes the generic wording.
 *
 * Six whole sentences rather than one sentence with the label interpolated in:
 * the app name sits in a different case and position per language ("im
 * Dateimanager", "dans le gestionnaire de fichiers", "ファイルマネージャーに表示"),
 * which a placeholder cannot carry.
 */
function revealHintFor(isDir: boolean, platform: GatewayPlatform, directLocal: boolean): string {
  // On a remote or tunneled session /api/reveal cannot drive the gateway host's
  // file manager, so shift+click degrades to a clipboard copy (files.py answers
  // the copy-degrade branch). Naming Finder/Explorer here would promise an action
  // the backend no longer performs, so the hint tells the truth: shift+click
  // copies the path. The click (open/browse) arm is unchanged — it drives the
  // in-app viewer, which works remotely — so only the shift+click clause differs.
  if (!directLocal) {
    return isDir
      ? i18nT('components.markdownRenderer.click_to_browse_shift_click_to_copy_path')
      : i18nT('components.markdownRenderer.click_to_open_shift_click_to_copy_path')
  }
  if (isDir) {
    if (platform === 'darwin') return i18nT('components.markdownRenderer.click_to_browse_shift_click_to_reveal_in_finder')
    if (platform === 'windows') return i18nT('components.markdownRenderer.click_to_browse_shift_click_to_open_in_file_explorer')
    return i18nT('components.markdownRenderer.click_to_browse_shift_click_to_show_in_file_manager')
  }
  if (platform === 'darwin') return i18nT('components.markdownRenderer.click_to_open_shift_click_to_reveal_in_finder')
  if (platform === 'windows') return i18nT('components.markdownRenderer.click_to_open_shift_click_to_open_in_file_explorer')
  return i18nT('components.markdownRenderer.click_to_open_shift_click_to_show_in_file_manager')
}

/**
 * Click-to-copy inline code chip for a span that names nothing the dashboard
 * can open — a command, an env var, an identifier.
 *
 * Its click copies, so it wears plain CODE styling in the NEUTRAL text colour:
 * `CHIP_BASE` with no accent, plus a dotted underline and the copy cursor —
 * unlike the `CHIP_ACTIONABLE` look (accent, solid underline on hover, pointer,
 * glyph) of the chips whose click navigates. Dressed as a link, this chip gets
 * clicked for navigation and answers with a silent clipboard write; dressed in
 * a second purple next to the path chip's (the Kiro inline-code colour), it is
 * taken for the same kind of chip. The dotted underline is the affordance the
 * rest of the app gives a term with an explanation on hover (`CrewLogPanel`,
 * `OAuthRelayAffordance`), which is what the tooltip is; it is deliberately
 * not a link's solid one. `index.css` keeps the colour neutral on the Kiro
 * themes, whose inline-code rule would otherwise paint it purple (see
 * `data-chip-action` below).
 *
 * Every cue is NON-LAYOUT, because the chip must stay a plain inline `<code>`
 * so a long span still breaks across lines: an `inline-flex` chip is atomic and
 * overflows its container, and an in-flow icon appended on copy pushes the
 * rest of the line over for 1.5s and back. So:
 *
 *  - the tooltip is the shared portal-rendered `InstantTip` (pointer after a
 *    short intent delay, keyboard focus at once), which paints in its own layer;
 *  - BOTH outcomes land in that same bubble: it flips to "Copied!" for
 *    `COPIED_FLASH_MS`, or to the `CopyFailedNotice` (ErrorNotice: icon, danger
 *    tone) for `COPY_FAILED_FLASH_MS`. One place to look, whatever happened, and
 *    nothing in the text flow — a red notice injected mid-sentence pushed the
 *    prose around for as long as it showed;
 *  - the bubble is HELD while an outcome shows (`useInstantTip({ hold })`): it
 *    is the only visible confirmation, and a mouse user moves on right after
 *    clicking, so a leave must not take it away; the flash's own timer ends it;
 *  - assistive tech hears "Copied!" from an `sr-only` status region that sits
 *    OUTSIDE the `<code>` (a `role="button"` element's children are
 *    presentational, so a live region inside it would never be announced) and
 *    is always mounted (a region that appears already filled is not reliably
 *    read). The refusal is announced by the `ErrorNotice` itself — its
 *    `role="alert"` is the accessible error surface, as everywhere else in the
 *    app (`errors-use-error-notice`) — and by nothing else, so it is heard
 *    once; the status region stays empty then. The bubble is normally already
 *    open when a click settles (hover intent, or focus), so the notice lands
 *    in an existing container rather than arriving with a fresh portal.
 *
 * The confirmation is gated on `copyToClipboard`'s boolean and a refused write
 * is rendered, not swallowed — see `useCopiedFlash` and `CopyFailedNotice`.
 *
 * The accessible name says what the click does and to what ("Copy npm test").
 * The text alone named a button whose purpose a screen-reader user could only
 * guess at; the tooltip still reaches them as the description.
 *
 * `data-chip-action` is what the stylesheet keys the chip colours on
 * (`index.css`, the inline-code rules): the Kiro themes paint every inline code
 * span at a specificity the `text-accent` utility cannot beat, so the actionable
 * chips need a rule of their own, and this attribute is how a chip says which
 * kind it is. Two values, because two kinds are told apart: `copy` (this chip)
 * and `navigate` (the path, session and link chips, whose click goes
 * somewhere). WHAT a navigating chip opens is already stated by `data-path*`,
 * `data-session-key` and the accessible name; a finer vocabulary here would
 * duplicate them for no consumer. Set AFTER the inbound props, so raw HTML
 * cannot claim another.
 */
function CopyableCode({ className, safeProps, text, children }: {
  className: string
  safeProps: Record<string, unknown>
  text: string
  children: React.ReactNode
}) {
  const value = text.trim()
  const { copied, failed, flashSeq, copy } = useCopiedFlash(value)
  // Held by the outcome's attempt number, not a boolean: a second refusal is a
  // new outcome that must reopen a bubble a scroll or Escape closed, and a
  // boolean that stays true has no edge for it. `arm` names the pressed chip
  // for that reopen — after an Escape no enter or focus fires for the retry.
  // `flow`: above from the message's first line, below from any lower one, so
  // the bubble never covers the words that lead up to the chip.
  const { tip, tipHandlers, tipId, arm } = useInstantTip({ hold: flashSeq, placement: 'flow' })
  const handleCopy = (e: React.MouseEvent | React.KeyboardEvent) => {
    e.preventDefault()
    e.stopPropagation()
    arm(e.currentTarget as HTMLElement)
    copy(value)
  }
  const cue = failed
    ? <CopyFailedNotice message={i18nT('components.markdownRenderer.couldnt_copy_select_the_text_to_copy_it_manually')} />
    : copied
      ? i18nT('components.markdownRenderer.copied')
      : i18nT('components.markdownRenderer.click_to_copy')
  return (
    <>
      <code
        className={`${className} cursor-copy underline decoration-dotted decoration-muted underline-offset-2`}
        // Inbound props first, so a `<code>` arriving from raw HTML cannot
        // overwrite the role, the name or the handlers that make this honest.
        {...safeProps}
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-to-interactive-role -- <code> is intentionally interactive (click-to-copy)
        role="button"
        tabIndex={0}
        aria-label={i18nT('components.markdownRenderer.copy_chip_name', { text: value })}
        data-chip-action="copy"
        // No native title, whatever raw HTML asked for: the bubble is this
        // chip's one tooltip, and a `title="Open file"` beside "Click to copy"
        // would name an action the click does not perform.
        title={undefined}
        onClick={handleCopy}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') handleCopy(e) }}
        {...tipHandlers}
      >
        {children}
      </code>
      <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[calc(100vw-1rem)]">{cue}</InstantTip>
      {/* Always mounted, so the region exists before its text changes — a live
          region that appears already filled is not reliably read. Empty, it
          costs one out-of-flow node. Only the short, transient "Copied!" lives
          here: the refusal's accessible surface is the ErrorNotice itself. */}
      <span role="status" aria-live="polite" className="sr-only">
        {copied ? i18nT('components.markdownRenderer.copied') : ''}
      </span>
    </>
  )
}

/**
 * Click-to-switch inline chip for a confirmed dashboard session key.
 *
 * Deliberately shaped like the confirmed PATH chip rather than like the
 * click-to-copy fallback it replaces: same `<code>` element and `CHIP_BASE`, a
 * leading glyph so "this is actionable" is legible at rest rather than only on
 * hover, and Ctrl/Cmd+click reserved for copying. A reader who has learned what a
 * file chip does therefore already knows what this does.
 *
 * `stopPropagation` keeps the container's artifact-link delegation from also
 * firing for a click this chip has handled.
 */
function SessionChip({ sessionKey, sessionTitle, label, safeProps, onOpen, children }: {
  sessionKey: string
  sessionTitle: string
  /** The span's visible text — the author's spelling of the key or short name. */
  label: string
  safeProps: Record<string, unknown>
  onOpen: (key: string) => void
  children: React.ReactNode
}) {
  const { copied, press, failureBubble } = useTitleCuedCopy(sessionKey, i18nT('components.markdownRenderer.the_copy_failed_ctrl_cmd_click_copies_the_full_session_id_for_label', { label }))
  const act = (e: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    // The NORMALISED key, not the author's spelling: `?sid=` rejects a
    // `dashboard_`-prefixed transcript filename.
    if (e.ctrlKey || e.metaKey) { press(e.currentTarget); return }
    onOpen(sessionKey)
  }
  return (
    <>
    <code
      className={CHIP_ACTIONABLE}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-to-interactive-role -- <code> is intentionally interactive (click-to-switch)
      role="button"
      tabIndex={0}
      onClick={act}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') act(e) }}
      {...safeProps}
      data-session-key={sessionKey}
      data-chip-action="navigate"
      // The name states the action, same rule as the copy and path chips, and
      // keeps the VISIBLE text: an `aria-label` replaces the content as the
      // name, so naming only the title would drop the key the message is
      // about and make two sessions with one title indistinguishable. The
      // title rides in the three-line description below.
      aria-label={i18nT('components.markdownRenderer.switch_to_session_chip_name', { label })}
      // Title leads: the key alone does not say which conversation this is.
      title={copied
        ? i18nT('components.markdownRenderer.copied')
        : `${sessionTitle}\n${i18nT('components.markdownRenderer.click_to_switch_to_this_session')}\n${i18nT('components.markdownRenderer.ctrl_click_to_copy')}`}
    >
      <MessageSquare size={12} aria-hidden="true" className="inline align-middle mr-1 opacity-70" />
      {children}
      {copied && <Check size={12} aria-hidden="true" className="inline align-middle ml-0.5 opacity-70 pointer-events-none text-ok" />}
    </code>
    {failureBubble}
    </>
  )
}

/**
 * Click-to-reveal inline chip for a confirmed SIDEBAR folder path.
 *
 * The folder twin of `SessionChip`: a sidebar row a click can jump to, so it
 * wears the same actionable dress -- `<code>`, `CHIP_ACTIONABLE`, a leading
 * glyph, Ctrl/Cmd+click reserved for copying. The glyph is the sidebar's own
 * folder icon, so the chip reads as "that thing in the sidebar" rather than as a
 * directory on disk (the path chip's `Folder` glyph is monochrome too, but that
 * chip only exists for a path the backend stat-confirmed, which a sidebar folder
 * path never is -- the two cannot claim one span).
 *
 * The click REVEALS rather than navigates: the reader stays in this conversation
 * and the sidebar expands, scrolls to and flashes the folder, which is what
 * "where did you put my team" asks for. Opening a session would need a session
 * to pick; a folder holds many.
 */
function FolderChip({ folderId, folderPath, label, safeProps, onReveal, children }: {
  folderId: string
  /** The `/`-joined human path, what Ctrl/Cmd+click copies. */
  folderPath: string
  /** The span's visible text -- the author's spelling of the path. */
  label: string
  safeProps: Record<string, unknown>
  onReveal: (folderId: string) => void
  children: React.ReactNode
}) {
  const { copied, press, failureBubble } = useTitleCuedCopy(folderPath, i18nT('components.markdownRenderer.the_copy_failed_ctrl_cmd_click_copies_the_folder_path_label', { label }))
  const act = (e: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.ctrlKey || e.metaKey) { press(e.currentTarget); return }
    onReveal(folderId)
  }
  return (
    <>
    <code
      className={CHIP_ACTIONABLE}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-to-interactive-role -- <code> is intentionally interactive (click-to-reveal), same pattern as SessionChip
      role="button"
      tabIndex={0}
      onClick={act}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') act(e) }}
      {...safeProps}
      data-folder-id={folderId}
      data-chip-action="navigate"
      // The name states the action and keeps the visible text, same rule as the
      // session chip: two folders can share a leaf name, and the path is what
      // tells a screen-reader user which one this is.
      aria-label={i18nT('components.markdownRenderer.show_folder_chip_name', { label })}
      title={copied
        ? i18nT('components.markdownRenderer.copied')
        : `${i18nT('components.markdownRenderer.click_to_show_this_folder_in_the_sidebar')}\n${i18nT('components.markdownRenderer.ctrl_click_to_copy')}`}
    >
      <Folder size={CHIP_GLYPH_SIZE} aria-hidden="true" className={`${CHIP_GLYPH_GEOMETRY} opacity-70`} />
      {segmentedLabel(label, children)}
      {copied && <Check size={12} aria-hidden="true" className="inline align-middle ml-0.5 opacity-70 pointer-events-none text-ok" />}
    </code>
    {failureBubble}
    </>
  )
}

/**
 * The chip's text with a break opportunity after each separator and none inside
 * a segment. A folder path is long and the transcript column narrow, so the
 * span wraps often; the container's `overflow-wrap: anywhere` would break it at
 * whatever column runs out (`monitor-turn-budg / et`), which reads as a broken
 * word. Each segment is an unbreakable run and a `<wbr>` follows each separator,
 * so a wrapped chip reads `monitor-turn-budget/` then `worker`. Only a plain
 * string label is reshaped: a span whose children are richer nodes keeps them,
 * since `codeTextOf` already vouched that their text is the label.
 */
function segmentedLabel(label: string, children: React.ReactNode): React.ReactNode {
  if (typeof children !== 'string') return children
  const parts = label.split(/(\s*›\s*|\/)/)
  return parts.map((part, i) => (
    i % 2 === 1
      ? <React.Fragment key={i}>{part}<wbr /></React.Fragment>
      : <span key={i} className="whitespace-nowrap">{part}</span>
  ))
}

/**
 * Inline `code` span, upgraded to a click-to-open chip only once the backend has
 * confirmed the text names something that exists.
 *
 * The old behaviour linkified on regex match alone, which produced two bad
 * outcomes: a directory opened the file viewer and rendered "file not found"
 * (wrong — it exists), and non-paths that merely contain a slash (git refs,
 * repo slugs) became dead links. So the default is inverted here: plain text
 * unless proven otherwise.
 *
 * Binds its OWN click/key handlers rather than relying on delegation from the
 * container. That is what makes the affordance honest: the chip is the control
 * (`role="button"`, focusable, Enter/Space), the wrapper stays presentational,
 * and a `<code>` that arrives from raw HTML gets no handler at all — so a forged
 * chip cannot borrow the container's.
 */
export function InlineCode({ children, ...props }: { children?: React.ReactNode } & Record<string, unknown>) {
  // The span's TEXT as rendered (`codeTextOf`), not `String(children)`: a
  // raw-HTML `<code>npm <b>test</b></code>` arrives as an array of nodes, which
  // stringifies to "npm ,[object Object]" — and that string became the clipboard
  // text, the probe subject and, worst, the chip's accessible name, which
  // REPLACES the visible content a screen reader could read before. `null` means
  // the tree cannot vouch that its text is what the reader sees (a styled or
  // collapsible raw child, an image): no chip, no probe, no copy.
  const visibleText = codeTextOf(children)
  const codeStr = (visibleText ?? '').replace(/\n$/, '')
  const probeEnabled = useContext(PathProbeCtx)
  const actions = useContext(PathActionCtx)
  const sessionActions = useContext(SessionActionCtx)
  const folderActions = useContext(SidebarFolderCtx)
  const insideLink = useContext(InsideLinkCtx)
  const gatewayPlatform = useGatewayPlatform()
  const { directLocal } = useBranding()
  const raw = codeStr.trim()
  const pathResolution = usePathResolution(raw, probeEnabled)
  // Failure state for the chip's reveal (Shift+click / no handler wired); rendered
  // beside the chip. Declared before the early returns below (rules of hooks).
  const reveal = useRevealFailure(raw)
  // The confirmed path chip's Ctrl/Cmd+click copy — the same gated write every
  // other copy affordance in the renderer uses, and the same failure surface (the
  // bubble). Declared before the early returns below (rules of hooks).
  const pathCopy = useTitleCuedCopy(raw, i18nT('components.markdownRenderer.the_copy_failed_ctrl_cmd_click_copies_the_path_label', { label: basenameOf(raw.replace(/[\\/]+$/, '')) || raw }))

  // `data-path*` / `data-session-key` / `data-chip-action` describe a chip THIS
  // component rendered, so only it may set them. rehypeSanitize allowlists every
  // `data-*` attribute (isAllowedAttr: `k.startsWith('data')`), so raw HTML
  // arrives here with a forged pair intact; spreading it would publish
  // attributes claiming a backend-confirmed path that was never probed, or dress
  // a copy chip in the actionable colour. Drop any inbound copy.
  const safeProps = Object.fromEntries(
    Object.entries(props).filter(([k]) => {
      const name = k.toLowerCase()
      return !name.startsWith('data-path') && !name.startsWith('data-session') && !name.startsWith('data-folder') && name !== 'data-chip-action'
    }),
  )

  if (pathResolution.probePending
    || (pathResolution.kind !== 'file' && pathResolution.kind !== 'dir')) {
    // Keyed to `shaped`, not to `candidate` or `probePending`, so the reserve is
    // present in EVERY state this span can be in — streaming, probe in flight,
    // and probe answered "not a path". A reserve that appeared only while a probe
    // was pending would simply move the re-wrap to the moment it went away.
    // A session chip needs none: `isPathCandidate` demands a separator, a drive
    // or an extension, and a session key carries none of the three, so the two
    // chips cannot claim the same span.
    //
    // A FOLDER-shaped span (`goal/worker`, `goal › worker`) is reserved too, for
    // the same async reason with a different answer in flight: the folder roster
    // arrives from the `['chat-folders']` query, cold on first paint and
    // invalidated when a folder is created, so a chip can appear after the
    // paragraph is laid out. The reserve holds the glyph's width until then, and
    // the chip draws its own glyph at the same size and margin (`CHIP_GLYPH_SIZE`,
    // `CHIP_GLYPH_GEOMETRY`), so the answer restyles the span, it does not
    // reflow the row. Keyed to the SHAPE, not to the roster answering, for the
    // same reason as the path reserve above -- but only where a roster and a
    // handler are WIRED (`SidebarFolderCtx`): a host that offers no folder chip
    // (every renderer outside the chat page) has no answer in flight to hold
    // space for, and its `a/b` spans keep their exact width.
    const folderChipPossible = !!(folderActions.onFolderReveal && folderActions.folders)
    const folderReserve = folderChipPossible && folderSegmentsOf(raw) !== null ? <FolderGlyphReserve /> : null
    const reserve = pathResolution.shaped ? <ChipGlyphReserve path={pathResolution.splitPath} /> : folderReserve
    // Inside an anchor the link owns the click, so stay the inert span this was
    // before #4433 rather than cancelling the navigation to copy. Nothing is
    // lost: the browser's own "Copy link address" still reaches the URL. It IS
    // a link label, so it keeps the link colour (`navigate`: the stylesheet's
    // actionable rule — the class alone loses to the Kiro inline-code colour).
    if (insideLink) return <code className={`${CHIP_BASE} text-accent`} {...safeProps} data-chip-action="navigate">{reserve}{children}</code>
    // Nothing a chip could honestly act on: the tree cannot vouch that its text
    // is what the reader sees (`codeTextOf` -> null: a styled or collapsible
    // raw child, an image), or there is no visible text at all (an image-only
    // span, where a button would write '' — clearing the clipboard — and then
    // confirm it). Stay an inert, selectable code span.
    if (visibleText === null || raw === '') return <code className={CHIP_BASE} {...safeProps}>{reserve}{children}</code>
    const session = resolveSessionChip(raw, sessionActions)
    if (session) {
      return (
        <SessionChip
          sessionKey={session.key}
          sessionTitle={session.title}
          label={raw}
          safeProps={safeProps}
          onOpen={sessionActions.onSessionOpen!}
        >{children}</SessionChip>
      )
    }
    // A sidebar folder named by its full human path. After the session chip (a
    // session is the more specific target) and before the autolink rule (in-app
    // navigation over an external link, same order the session chip takes).
    // Most folder paths (`goal/worker`) are not path candidates -- no root, no
    // relative prefix, no extension -- so no probe runs and the chip is there on
    // first render. One that IS a candidate (`reports/weekly.md`) is not offered
    // while its stat probe is in flight, and becomes a folder chip only once the
    // probe said it is NOT on disk: a folder that shares its spelling with a real
    // directory keeps the confirmed path chip, and a click during the wait
    // cannot reveal a folder the probe is about to overrule.
    const folder = pathResolution.probePending ? null : resolveFolderChip(raw, folderActions)
    if (folder) {
      return (
        <FolderChip
          folderId={folder.id}
          folderPath={folder.path}
          label={raw}
          safeProps={safeProps}
          onReveal={folderActions.onFolderReveal!}
        >{children}</FolderChip>
      )
    }
    // A span whose WHOLE text matches an operator-configured autolink rule is
    // that work item (`PROJ-123`), so it links out
    // instead of only copying. `inlineCode` is opaque to `remarkAutolinkRules`
    // by design; whole-match keeps the chip atomic — `npm PROJ-123 run` stays a
    // plain copyable span — and the session chip wins first: in-app navigation
    // over an external link for a text both recognize. The native title
    // discloses the real target, same disclosure discipline as the path chip
    // below.
    const patternHref = wholeMatchAutolinkHref(raw)
    if (patternHref) {
      return (
        <a
          href={patternHref}
          target="_blank"
          rel="noopener noreferrer"
          title={patternHref}
          className="no-underline focus-ring"
        >
          {/* The glyph is what tells this chip apart from a copy chip at
              rest: without it the two are pixel-identical and the click
              outcome (open a tab vs copy) is a surprise. */}
          <code className={CHIP_ACTIONABLE} {...safeProps} data-chip-action="navigate">{reserve}{children}<ExternalLink className="lucide-inline ml-1" aria-hidden /></code>
        </a>
      )
    }
    return <CopyableCode className={CHIP_BASE} safeProps={safeProps} text={codeStr}>{reserve}{children}</CopyableCode>
  }
  const isDir = pathResolution.kind === 'dir'
  const { path, splitPath, kind, line: targetLine, endLine: targetEndLine } = pathResolution
  const revealHint = revealHintFor(isDir, gatewayPlatform, directLocal)
  // A leading glyph is what makes "this is actionable" legible at rest. Without
  // one, a confirmed chip and an inert one differ only on hover, so a reader
  // cannot tell which paths the backend actually resolved. Files use the same
  // per-extension icon set as the Files tab and the folder browser, so a .md and
  // a .json chip are distinguishable — but rendered monochrome at the folder
  // glyph's weight, because inline in prose this is an affordance marker, not
  // decoration. Decorative either way: the path text carries the meaning.
  //
  // The glyph is an INLINE atom and the chip stays a plain inline box. Making the
  // chip `inline-flex` to align the glyph turned it atomic, so a long path could
  // no longer break across lines and overflowed its container instead — the
  // render gate caught this as layout/unbreakable-token on the artifacts surface.
  const Glyph = isDir ? Folder : fileIcon(path)
  /** stopPropagation keeps the container's artifact-link delegation from also
   *  firing for a click that this chip has already handled. */
  const act = (e: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    // Ctrl/Cmd+Click copies the path text rather than opening/revealing.
    if (e.ctrlKey || e.metaKey) { pathCopy.press(e.currentTarget); return }
    activatePath(path, kind, e.shiftKey, actions, reveal.onError, targetLine, targetEndLine)
  }
  // Right-click opens the shared file-path menu (Open in default app / reveal /
  // copy path), additive to the existing click/shift-click activation. The menu
  // items self-gate on directLocal, so a remote session sees only Copy path.
  // `kind` is threaded through so a directory chip hides "Open with default
  // app" — the reveal endpoint 400s an `open` on a directory, which would land
  // the user on an error for a click they cannot fix.
  return (
    <>
    <FilePathMenu filePath={path} kind={kind}>
      <code
        className={CHIP_ACTIONABLE}
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-to-interactive-role -- <code> is intentionally interactive (click-to-open path chip), same pattern as CopyableCode
        role="button"
        tabIndex={0}
        onClick={act}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') act(e) }}
        {...safeProps}
        data-path={path}
        data-path-kind={kind}
        data-chip-action="navigate"
        data-path-line={targetLine}
        data-path-end-line={targetEndLine}
        // The name states what the click does and to what — "Open src/a.py:12"
        // for a file, "Browse src/" for a directory — so a screen-reader user
        // can tell this chip from the copy chip before pressing it. `raw`, for
        // the same reason the title uses it: the line suffix is the target.
        aria-label={isDir
          ? i18nT('components.markdownRenderer.browse_path_chip_name', { path: raw })
          : i18nT('components.markdownRenderer.open_path_chip_name', { path: raw })}
        // The resolved path leads the tooltip, not just the instruction. A native
        // tooltip paints in the browser's own layer, above page content, and any
        // element overlaying the chip must be pointer-events-none to let the click
        // reach it — so hovering always discloses the real target even when
        // surrounding markup visually covers the chip's text. It also shows a long
        // path in full when layout truncates it.
        //
        // `raw`, not `path`, so a `file:447` chip discloses the line it will jump
        // to. That keeps the disclosure honest without a second catalog string:
        // the location is already in the text the user is hovering.
        //
        // While a Ctrl/Cmd+click copy is confirmed the title says so, the same
        // acknowledgment the session chip gives the same gesture.
        title={pathCopy.copied
          ? i18nT('components.markdownRenderer.copied')
          : `${raw}\n${revealHint}\n${i18nT('components.markdownRenderer.ctrl_click_to_copy')}`}
      >
        <Glyph size={CHIP_GLYPH_SIZE} aria-hidden="true" className={`${CHIP_GLYPH_GEOMETRY} opacity-70`} />
        {targetLine != null && raw.length > splitPath.length
          // Keep the location suffix atomic. A range is the case that actually
          // misleads: broken across lines, `…2026.md:10-` / `16` reads as a citation
          // ending at line 10 until the eye reaches the next line. The path itself
          // stays breakable, since that is what lets a long citation wrap at all.
          ? <>{splitPath}<span className="whitespace-nowrap">{raw.slice(splitPath.length)}</span></>
          : children}
      </code>
    </FilePathMenu>
    {/* askAgent on: a transcript chip holds no draft; the host composer's
        draft is persisted per slot. */}
    {reveal.error && (
      <ErrorNotice variant="inline" className="ml-1.5 align-baseline" message={reveal.error} askAgent onDismiss={reveal.clear} testId="md-chip-reveal-error" />
    )}
    {pathCopy.failureBubble}
    </>
  )
}
