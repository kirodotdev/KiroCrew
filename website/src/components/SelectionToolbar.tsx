import { Fragment, useState, useEffect, useId, useLayoutEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageSquareQuote, MessageCircleQuestionMark, MessageSquarePlus, X, Copy, Check } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { isTouchDevice } from '../utils/isTouchDevice'
import { containedSelectionRange } from '../utils/selectionContainment'
import { useImeGuard } from '../hooks/useImeGuard'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

/**
 * Opt-in annotation input for the toolbar. When a host passes one, selecting
 * text opens a focused comment box straight away — no "Comment" button to find
 * first — and the host's `actions` shrink to icon buttons beside it (below it
 * on a narrow viewport). Chat's toolbar passes none and is unchanged.
 */
export interface SelectionComposer {
  /**
   * Fires each time the composer opens for a selection, BEFORE focus moves
   * into the input. Focusing the input collapses the document selection, so
   * this is the host's one chance to resolve the anchor from the live range
   * and paint its own highlight over it.
   */
  onOpen?: (text: string) => void
  /** The typed comment plus the selection it annotates. */
  onSubmit: (comment: string, text: string) => void
  /** Closed without a submit: Escape, the close button, click-away, selection lost, unmount. */
  onClose?: () => void
  /**
   * Whether the input holds unsaved text. A host that can be re-targeted to
   * another file (the file viewer's rail click replaces a CLEAN tab in place)
   * treats an open draft like a dirty buffer: the tab is not clean, so the
   * new file opens beside it instead of under the draft's anchor.
   */
  onDraftChange?: (hasDraft: boolean, passage: { anchor: string; start: number } | null) => void
  /**
   * Asked before Escape / the close button discards a typed draft; resolve
   * `true` to discard. Omit to discard without asking. The host owns the
   * dialog so the wording matches its other discard prompts.
   */
  confirmDiscard?: () => Promise<boolean>
  /**
   * Durable home for the draft, so a teardown the toolbar cannot guard — a
   * chat-slot switch replaces the whole side panel — does not lose it. The
   * toolbar writes on every keystroke (the text AND the selection it was
   * written for), and reads it back when the box next opens on this host for
   * THE SAME selection text — a comment about sentence A must not come back
   * anchored to sentence B, so a non-matching selection leaves the saved draft
   * where it is until its passage is selected again. Cleared on submit or a
   * confirmed discard; the host clears it too when its own guards discard.
   * Every operation must be non-throwing (a full or refusing storage is a
   * mundane condition, not a crash).
   */
  draftStore?: {
    /** The draft saved for THIS passage (text + character offset in the
     *  container), or null. Keyed per passage, so drafts on two passages of one
     *  file coexist and neither can overwrite or clear the other. */
    read: (anchor: string, start: number) => string | null
    write: (text: string, anchor: string, start: number) => void
    clear: (anchor: string, start: number) => void
  }
}

/** Below this viewport width the composer stacks its icon row under a full-width input. */
const COMPOSER_STACK_BELOW_PX = 480

/**
 * Rebuild a DOM range from character offsets in `root`'s text content. Used to
 * put the document selection back after the composer closes on Escape: focus
 * in the input collapsed it, and a live `Range` saved before the host painted
 * its highlight `<mark>`s does not survive that DOM surgery (`surroundContents`
 * deletes the marked characters from the original text node, which collapses
 * any live range that covered them). Text OFFSETS do survive — wrapping and
 * `normalize()` move node boundaries, never characters.
 */
export function rangeFromTextOffset(root: Node, start: number, length: number): Range | null {
  if (length <= 0) return null
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const range = document.createRange()
  let seen = 0
  let startSet = false
  const end = start + length
  let node: Node | null
  while ((node = walker.nextNode())) {
    const len = (node as Text).data.length
    if (!startSet && start < seen + len) {
      range.setStart(node, start - seen)
      startSet = true
    }
    if (startSet && end <= seen + len) {
      range.setEnd(node, end - seen)
      return range
    }
    seen += len
  }
  return null
}

export interface SelectionAction {
  id: string
  icon: React.ReactNode
  label: string
  /** Tooltip shown on hover when the label alone does not say where the
   *  action lands. Falls back to the label; never the accessible name. */
  hint?: string
  /** Called with selected text and the bounding rect of the selection. A copy
   *  action may return `copyToClipboard`'s promise so the toolbar shows its
   *  checkmark only once the clipboard actually took the text. */
  onClick: (text: string, rect: DOMRect) => void | Promise<boolean>
}

/**
 * One piece of selected text, or a boundary between two blocks of it.
 *
 * `pre` marks a fragment taken from preformatted context, where whitespace is
 * content: the assembly pass drops whitespace-only fragments as markup
 * indentation, and must not do that to the blank lines inside a code block.
 */
type TextToken = { text: string; pre?: boolean } | { breakLevel: 1 | 2 }

/**
 * Tag names treated as block-level regardless of what CSS says.
 *
 * `getComputedStyle` is the primary signal, but under test no stylesheet is
 * applied, so a `<span class="block">` reports `display: inline`. Consulting the
 * tag as well keeps separator behaviour the same in the browser and in tests —
 * otherwise the tests would happily agree with an implementation that loses
 * paragraph boundaries only in production, which is the exact blind spot that
 * let the previous version of this function ship broken.
 */
const BLOCK_TAGS = new Set([
  'P', 'DIV', 'LI', 'UL', 'OL', 'BLOCKQUOTE', 'PRE', 'HR', 'TABLE', 'TR',
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
])

function isBlockLevel(el: Element): boolean {
  if (BLOCK_TAGS.has(el.tagName)) return true
  const display = getComputedStyle(el).display
  // `inline-flex` and `inline-block` are deliberately absent: an unfurled chip
  // is inline-flex, and treating that as a block is precisely what made the
  // browser inject newlines into the middle of a sentence.
  return display === 'block' || display === 'flow-root' || display === 'list-item' ||
    display === 'flex' || display === 'grid' || display.startsWith('table')
}

/**
 * Walk the LIVE nodes a range covers, emitting tokens in document order.
 *
 * Live nodes, not `range.cloneContents()`: `getComputedStyle` needs an element
 * that is actually in the document, and the clone is detached. The clone is only
 * useful for the cheap "is anything unfurled in here?" test.
 */
function collectSelectionTokens(node: Node, range: Range, out: TextToken[]): void {
  if (!range.intersectsNode(node)) return

  if (node.nodeType === Node.TEXT_NODE) {
    const whole = node.textContent || ''
    const from = node === range.startContainer ? range.startOffset : 0
    const to = node === range.endContainer ? range.endOffset : whole.length
    const slice = whole.slice(from, to)
    if (!slice) return
    // A wrapped paragraph's source newlines render as a single space, so they
    // are collapsed here too — except under `pre`, where the whitespace IS the
    // content and collapsing it would corrupt a quoted code block. The ancestor
    // tag is checked alongside the computed style for the same reason as
    // `BLOCK_TAGS`: `white-space: pre` on `<pre>` comes from the UA stylesheet,
    // which a test environment does not apply, so style alone would make this
    // branch unverifiable outside a real browser.
    const parent = node.parentElement
    const preformatted = !!parent && (
      !!parent.closest('pre') || getComputedStyle(parent).whiteSpace.startsWith('pre')
    )
    out.push(preformatted ? { text: slice, pre: true } : { text: slice.replace(/\s+/g, ' ') })
    return
  }

  if (node.nodeType !== Node.ELEMENT_NODE) return
  const el = node as Element

  // An unfurled link contributes the URL THE MODEL WROTE, and nothing else: its
  // subtree — fetched title, description, domain — is skipped whole.
  //
  // This is a POSITIONAL substitution: the node is replaced where it sits. The
  // previous version instead searched `Selection.toString()` for the element's
  // rendered text, which was unsound three ways. It could land on an earlier
  // identical run of prose and rewrite text the user really had selected; it
  // could not match a card at all, because a card's three block spans put
  // newlines in `toString()` that its `textContent` does not have; and its
  // whitespace absorption could not tell a newline invented around an
  // inline-flex chip from a real paragraph break around a block card, so it ate
  // both. Replacing by position removes all three: there is no text to match.
  //
  // A partially selected link still yields its whole URL — half a URL is not
  // useful to paste, and the alternative is handing over half a page title.
  const url = el.getAttribute('data-unfurl-url')
  if (url) { out.push({ text: url }); return }

  if (getComputedStyle(el).display === 'none') return
  if (el.tagName === 'BR') { out.push({ breakLevel: 1 }); return }

  const block = isBlockLevel(el)
  // A block that contributes no TEXT must contribute no break either. The copy
  // button is the case that forced this: it centres its icon with `display:
  // grid`, which is block-level by any honest reading, but it holds no text — so
  // emitting boundaries around it dropped a paragraph break into the middle of a
  // sentence, right after the URL it sits next to. Rather than special-casing
  // the tag, the breaks are rolled back when the subtree turns out to be empty,
  // which covers every icon-only control and layout spacer.
  const mark = out.length
  if (block) out.push({ breakLevel: 2 })
  for (const child of Array.from(el.childNodes)) collectSelectionTokens(child, range, out)
  if (!block) return
  const contributed = out.slice(mark).some(tok => 'text' in tok && tok.text.trim() !== '')
  if (contributed) out.push({ breakLevel: 2 })
  else out.length = mark
}

/**
 * The unfurled link that wholly CONTAINS a range, if any.
 *
 * `cloneContents()` only ever reveals descendants, so it cannot see the link a
 * selection sits inside. That gap matters for a selection confined to a chip's
 * title: the clone comes back holding bare text, and the fast path would hand
 * over the fetched title — precisely what this function exists to prevent.
 *
 * Matched as `a[data-unfurl-url]`, not on the attribute alone, so the walk up the
 * tree cannot be captured by some future non-anchor ancestor that happens to
 * carry the attribute. Only the chip and the card publish it, and both put it on
 * their anchor.
 */
function enclosingUnfurl(range: Range): Element | null {
  const node = range.commonAncestorContainer
  const el = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement
  return el ? el.closest('a[data-unfurl-url]') : null
}

/** Whether a range contains, or sits inside, an unfurled link. */
function rangeTouchesUnfurl(range: Range): boolean {
  return !!range.cloneContents().querySelector('[data-unfurl-url]') || !!enclosingUnfurl(range)
}

/** Reconstruct one range's text, substituting unfurled links for their URLs. */
function textFromRange(range: Range): string {
  // A selection that lies entirely within one unfurled link cannot express
  // anything except that link, so it IS the URL — however little of the title
  // the user happened to sweep.
  const enclosing = enclosingUnfurl(range)
  if (enclosing) return (enclosing.getAttribute('data-unfurl-url') || range.toString()).trim()

  const root = range.commonAncestorContainer
  const tokens: TextToken[] = []
  collectSelectionTokens(
    root.nodeType === Node.ELEMENT_NODE ? root : root.parentNode ?? root,
    range,
    tokens,
  )

  const parts: string[] = []
  let pendingBreak = 0
  let pendingSpace = false
  for (const tok of tokens) {
    if ('breakLevel' in tok) {
      // A break supersedes a space: the whitespace between two block elements is
      // markup indentation, and keeping it renders as "…sentence.\n\n \n\nhttps://…",
      // a line holding a single space.
      pendingBreak = Math.max(pendingBreak, tok.breakLevel)
      pendingSpace = false
      continue
    }
    if (!tok.text) continue
    // Whitespace-only runs are held rather than emitted, so a break arriving on
    // EITHER side can absorb them. Text inside `pre` is exempt: there the
    // whitespace is the content. A held space between two inline pieces is
    // flushed normally, so the word gap in "a b" survives.
    if (!tok.pre && !tok.text.trim()) { pendingSpace = true; continue }
    if (parts.length) {
      // Leading breaks and spaces are dropped while `parts` is empty; a trailing
      // one is never flushed, because only real text flushes.
      if (pendingBreak) parts.push(pendingBreak === 2 ? '\n\n' : '\n')
      else if (pendingSpace) parts.push(' ')
    }
    pendingBreak = 0
    pendingSpace = false
    parts.push(tok.text)
  }
  return parts.join('').trim()
}

/**
 * The text a selection should hand to Quote / Ask / Copy.
 *
 * `Selection.toString()` returns what is RENDERED, which is wrong the moment a
 * link has been unfurled: the chip and card show the page's fetched title where
 * the URL used to be, so quoting a link produced the title and dropped the URL
 * entirely. An inline chip is also `inline-flex`, which `toString()` treats as a
 * block boundary, so it injected stray newlines mid-sentence.
 *
 * So for a selection touching an unfurled link the text is rebuilt from the DOM,
 * substituting each such node with the URL from its `data-unfurl-url` — which the
 * chip and card both publish for this purpose — and emitting block boundaries
 * explicitly, so a paragraph break survives and a chip does not invent one.
 *
 * Every OTHER selection returns the browser's own string untouched, including the
 * multi-range case: `toString()` already concatenates all ranges, and only the
 * reconstruction path has to walk them itself.
 */
export function selectionTextFrom(sel: Selection): string {
  const ranges: Range[] = []
  for (let i = 0; i < sel.rangeCount; i++) {
    try {
      ranges.push(sel.getRangeAt(i))
    } catch {
      // A range that cannot be read is skipped rather than failing the whole
      // selection; the remaining ranges still produce usable text.
    }
  }
  // Nothing unfurled here: hand back exactly what the browser would have, byte
  // for byte. Every ordinary message takes this path — and with previews off,
  // which is the default, every message does.
  if (!ranges.length || !ranges.some(rangeTouchesUnfurl)) return sel.toString().trim()

  // Firefox is the one engine that gives a Selection more than one range
  // (ctrl+drag). Reconstructing only range 0 would silently DROP the rest, which
  // is worse than the wrong-text bug this function fixes: `toString()` at least
  // returned every selected character.
  //
  // The chunks are joined with a NEWLINE rather than reproducing the stringifier's
  // delimiter-free concatenation, and that is deliberate. Each chunk here can end
  // in a substituted URL, so concatenating without a separator welds the next
  // chunk onto it — `"first https://a.example" + "second …"` yields
  // `https://a.examplesecond`, a URL that no longer resolves and cannot be pasted.
  // Matching the browser byte-for-byte would therefore defeat the one thing this
  // function exists to guarantee. A newline is the least invented separator that
  // keeps every URL intact, and discontiguous chunks were never one sentence.
  return ranges.map(textFromRange).filter(Boolean).join('\n').trim()
}

interface SelectionToolbarProps {
  /** Container element to listen for text selection within */
  containerRef: React.RefObject<HTMLElement | null>
  /** Actions to show in the toolbar */
  actions: SelectionAction[]
  /** External trigger (e.g. from the code editor) — shows toolbar at given position with given text */
  externalSelection?: { text: string; x: number; y: number } | null
  /** Type-first annotation input; see `SelectionComposer`. Omit for the plain action row. */
  composer?: SelectionComposer
  /**
   * Hide the toolbar without discarding its state. The file viewer keeps
   * inactive tabs MOUNTED (display:none), but this toolbar portals to
   * `document.body`, so without this an open composer in a hidden tab would
   * stay on screen and submit against a file the user is no longer looking at.
   * The draft survives the suspension and comes back with the tab.
   */
  suspended?: boolean
}

/** Generic floating toolbar that appears when user selects text within a container.
 *  Extensible — pass any actions (quote, copy, etc.) via the `actions` prop. */
export default function SelectionToolbar({ containerRef, actions, externalSelection, composer, suspended = false }: SelectionToolbarProps) {
  const [visible, setVisible] = useState(false)
  // Mirrors for the document listeners (bound once): whether the box is up,
  // and whether the host has hidden it.
  const visibleRef = useRef(false)
  visibleRef.current = visible
  const suspendedRef = useRef(suspended)
  suspendedRef.current = suspended
  // The composer's draft text lives HERE, not in `ComposerBox`, so it survives
  // a suspension (the box unmounts while its tab is hidden) and comes back
  // with the tab. Reset on submit and on every close.
  const [composerText, setComposerText] = useState('')
  const [pos, setPos] = useState({ x: 0, y: 0 })
  // Id prefix for the per-action hint nodes (`aria-describedby` targets);
  // several toolbars can be mounted at once (split view), so it is per instance.
  const hintIdBase = useId()
  // Clamped top-left, computed after measuring the toolbar so it never clips
  // the viewport edges. The layout effect below corrects this before paint,
  // and framer-motion's `initial opacity: 0` hides the mount frame, so there's
  // no visible jump from the pre-measure value.
  const [clampedPos, setClampedPos] = useState({ x: 0, y: 0 })
  // Mirrors clampedPos so the layout effect can compare against the last value
  // without listing the effect's own output in its dependency array (which
  // would fire the effect a second, redundant time on every reposition).
  const clampedRef = useRef({ x: 0, y: 0 })
  const [copiedId, setCopiedId] = useState<string | null>(null)
  // A refused clipboard write, reported inside the composer (the plain row
  // shows the same notice under its buttons).
  const [copyFailed, setCopyFailed] = useState(false)
  // Tracks the "copied!" reset timer so it can be cancelled on unmount — a late
  // setCopiedId firing after the host/jsdom is torn down would touch `window`
  // via React DOM and throw (an uncaught post-teardown ReferenceError).
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const selectedTextRef = useRef('')
  const toolbarRef = useRef<HTMLDivElement>(null)
  const sourceRef = useRef<'dom' | 'external' | null>(null)

  const selectionRectRef = useRef<DOMRect | null>(null)

  const lastMouseRef = useRef({ x: 0, y: 0 })
  const triggeredByMouseRef = useRef(false)

  // ── Composer bookkeeping ──────────────────────────────────────────────────
  // Latest composer callbacks, so the document listeners (bound once) and the
  // visibility effect never call a stale closure.
  const composerRef = useRef(composer)
  useEffect(() => { composerRef.current = composer }, [composer])
  // `onClose` fires exactly once per open. Starts "closed" so the mount-time
  // run of the visibility effect below does not fire it for a composer that
  // was never open. A submit sets it before hiding, so the host is told about
  // the submit OR the close, never both.
  const composerClosedRef = useRef(true)
  // The selection as text offsets in the container, saved BEFORE `onOpen` so
  // Escape can put it back (see `rangeFromTextOffset`).
  const savedSelectionRef = useRef<{ start: number; length: number } | null>(null)
  // Bumped when the input auto-grows, so the clamp/flip below re-measures and
  // a box that grew past the bottom edge flips above the selection.
  const [composerGrowTick, setComposerGrowTick] = useState(0)
  // Whether the CURRENT open should put the caret in the input at once. True
  // for a mouse selection — the gesture is over on mouseup — and false for the
  // incremental modalities: a Shift+Arrow selection is still being extended
  // (focus would turn the next arrow into typing), and a touch selection is
  // still under its drag handles (focus collapses them and summons the
  // keyboard). Those open the box unfocused; Enter, or a tap, moves in.
  const [composerAutoFocus, setComposerAutoFocus] = useState(true)
  // The composer's textarea, for the Enter-to-focus path above.
  const composerInputRef = useRef<HTMLTextAreaElement>(null)
  // Whether the input holds unsaved text. A click-away with a draft keeps the
  // box open instead of silently discarding what was typed.
  const composerDraftRef = useRef(false)

  const fireComposerClose = useCallback(() => {
    if (composerClosedRef.current) return
    composerClosedRef.current = true
    composerRef.current?.onClose?.()
  }, [])

  // Draft flag, mirrored to the host (see `SelectionComposer.onDraftChange`).
  const onComposerDraftChange = useCallback((hasDraft: boolean) => {
    if (composerDraftRef.current === hasDraft) return
    composerDraftRef.current = hasDraft
    // The passage rides along so a host guard that discards can clear THIS
    // draft's slot and no other passage's.
    const passage = hasDraft && savedSelectionRef.current
      ? { anchor: selectedTextRef.current, start: savedSelectionRef.current.start }
      : null
    composerRef.current?.onDraftChange?.(hasDraft, passage)
  }, [])

  const checkSelection = useCallback(() => {
    // A hidden tab's toolbar must not react to selections made in whatever is
    // on screen instead.
    if (suspendedRef.current) return
    // A typed draft is anchored to the selection it was written for. A new
    // selection while it is open must NOT silently move the anchor under the
    // draft (the text would be submitted against the wrong passage): the box
    // stays where it is until the draft is submitted or discarded.
    if (visibleRef.current && composerRef.current && composerDraftRef.current) return
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      // Only dismiss if toolbar was shown by DOM selection (not external/editor).
      // An OPEN composer is the exception: its selection is collapsed by design
      // (focus, or the host's <mark> wrapping), so an empty selection is not a
      // dismissal there — the box closes through ✕ / Escape / click-away.
      if (sourceRef.current === 'dom' && !(composerRef.current && visibleRef.current)) setVisible(false)
      return
    }

    const container = containerRef.current
    if (!container) { setVisible(false); return }

    // Ensure selection is within our container. Containment cannot be judged by
    // `commonAncestorContainer` alone — see `containedSelectionRange`, which
    // carries the boundary-normalization mechanism and both rejection tiers.
    //
    // `measureRange` is what the toolbar is positioned from, and the helper
    // returns it clamped to the container: an accepted overhang's boundary point
    // would otherwise pull the next block's line box into the rect and park the
    // toolbar one line below the selection on the touch/keyboard paths.
    const range = sel.getRangeAt(0)
    const measureRange = containedSelectionRange(range, container)
    if (!measureRange) {
      setVisible(false)
      return
    }

    // The composer anchors its comment by offsets taken from `measureRange`
    // (below), so its text must come from that same contiguous range: on a
    // Firefox ctrl+drag multi-range selection the aggregated text would
    // describe passages the offsets do not, and the highlight would land on
    // the wrong span. Plain actions (Copy) keep every selected chunk.
    const composerMode = !!composerRef.current
    const text = composerMode && sel.rangeCount > 1
      ? measureRange.toString().trim()
      : selectionTextFrom(sel)
    if (!text) { setVisible(false); return }

    selectedTextRef.current = text

    const rect = measureRange.getBoundingClientRect()
    selectionRectRef.current = rect
    // The composer is a comment box, not a pointer-anchored pill: it hangs from
    // the selection's left edge, where a margin note would sit, rather than
    // centring on wherever the mouse happened to let go (which parked a
    // 460px box half a panel to the left of the text it annotates).
    const x = composerMode
      ? rect.left
      : triggeredByMouseRef.current
        ? lastMouseRef.current.x
        : rect.left + rect.width / 2
    const y = triggeredByMouseRef.current && !composerMode
      ? lastMouseRef.current.y + 8
      : rect.bottom + 8
    setPos({ x, y })
    sourceRef.current = 'dom'

    const activeComposer = composerRef.current
    if (activeComposer) {
      // Offsets first, then the host's `onOpen`: the host may wrap the selection
      // in <mark>s, and a live range would not survive that (offsets do).
      try {
        const pre = document.createRange()
        pre.setStart(container, 0)
        pre.setEnd(measureRange.startContainer, measureRange.startOffset)
        savedSelectionRef.current = { start: pre.toString().length, length: measureRange.toString().length }
      } catch {
        savedSelectionRef.current = null
      }
      // A re-open over an already-open composer (the user re-selected) is a
      // fresh open for the host too: it resolves the new anchor and re-paints.
      composerClosedRef.current = false
      setComposerAutoFocus(triggeredByMouseRef.current && !isTouchDevice())
      // A notice from an earlier copy attempt does not belong to this selection.
      setCopyFailed(false)
      activeComposer.onOpen?.(text)
      // A draft interrupted by a teardown (slot switch) comes back into the box
      // — but only over the passage it was written for: same text AND same
      // position, so a repeated sentence elsewhere in the file does not inherit
      // a comment about its twin.
      if (!visibleRef.current && activeComposer.draftStore) {
        const saved = activeComposer.draftStore.read(text, savedSelectionRef.current?.start ?? -1)
        if (saved && saved.trim()) {
          setComposerText(saved)
          onComposerDraftChange(true)
        }
      }
    }
    setVisible(true)
  }, [containerRef, onComposerDraftChange])

  // After the toolbar mounts/repositions, measure it and clamp its position so
  // it stays fully inside the viewport. We position by the top-left (left/top)
  // and deliberately do NOT use a CSS translate to center it: this is a
  // framer-motion element, and framer-motion owns the `transform` property for
  // its mount animation (scale/y) — it silently drops any `translate(-50%)` we
  // set, which left the toolbar's left edge (not its center) at the anchor and
  // clipped it by half its width near the right edge.
  // `pos.x` is the desired horizontal center, so convert to a left edge
  // (`pos.x - w/2`) and clamp into [margin, viewportWidth - w - margin].
  // `offsetWidth/Height` report the layout footprint independent of the in-flight
  // scale animation, so the clamp uses the toolbar's true size. Runs in a layout
  // effect so the corrected position commits before paint — no visible jump.
  useLayoutEffect(() => {
    if (!visible) return
    const el = toolbarRef.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const margin = 8
    const vw = window.innerWidth
    const vh = window.innerHeight
    // `pos.x` is the pill's CENTRE, but the composer's LEFT edge (see checkSelection).
    const desiredLeft = composer ? pos.x : pos.x - w / 2
    const left = Math.max(margin, Math.min(desiredLeft, vw - w - margin))
    let top: number
    if (composer && selectionRectRef.current) {
      // The composer sits ABOVE the selection when there is room: a 520px box
      // below it hides the lines that follow, and a reader commenting on a
      // claim usually needs the sentence after it, while the lines above have
      // already been read. Below the selection (`pos.y`) is the fallback when
      // the selection is too close to the top edge; a box that would still
      // overflow the bottom there is pushed up as far as the top margin.
      const above = selectionRectRef.current.top - margin - h
      if (above >= margin) top = above
      else top = pos.y + h + margin > vh ? Math.max(margin, vh - h - margin) : pos.y
    } else {
      // The pill flips above its anchor only when it would overflow the bottom edge.
      top = pos.y + h + margin > vh ? Math.max(margin, pos.y - h - margin) : pos.y
    }
    // Compare against the ref (not state) so clampedPos stays out of the deps —
    // the effect runs once per pos change instead of twice.
    if (left !== clampedRef.current.x || top !== clampedRef.current.y) {
      clampedRef.current = { x: left, y: top }
      setClampedPos({ x: left, y: top })
    }
    // `composerGrowTick` and `copyFailed` are not read: they re-run the
    // measurement after the box changed height (the input grew, or the
    // copy-failed notice appeared), since the flip decision depends on it.
    // `suspended` likewise: a hidden tab does not render the box, so a resize
    // while hidden leaves `pos` untouched, and resuming must re-clamp against
    // the viewport it comes back to rather than the one it left.
  }, [visible, pos, composerGrowTick, composer, copyFailed, suspended])

  // External trigger (editor selections that don't use window.getSelection)
  useEffect(() => {
    if (externalSelection) {
      selectedTextRef.current = externalSelection.text
      selectionRectRef.current = new DOMRect(externalSelection.x, externalSelection.y, 0, 0)
      setPos({ x: externalSelection.x, y: externalSelection.y + 8 })
      sourceRef.current = 'external'
      setVisible(true)
    }
  }, [externalSelection])

  useEffect(() => {
    // Every deferred selection check has to be cancellable. These fire 0-50ms
    // after a pointer/key event, so an unmount inside that window leaves a
    // check running for a component that is gone. In a browser that is benign
    // but wrong — `setVisible` on an unmounted component is a no-op and the
    // stale `checkSelection` just reads the live selection for nothing. Where
    // it actually breaks is host teardown: with the document/window already
    // gone (jsdom between tests), the same late callback throws an uncaught
    // `ReferenceError: window is not defined` from `window.getSelection()`.
    // That is the identical failure mode `copyTimerRef` above already guards;
    // only the touch-path `selectionChangeTimer` below was ever cleared here.
    const pending = new Set<ReturnType<typeof setTimeout>>()
    const defer = (fn: () => void, ms: number) => {
      const id = setTimeout(() => { pending.delete(id); fn() }, ms)
      pending.add(id)
    }

    const onMouseUp = (e: MouseEvent) => {
      if (suspendedRef.current) return
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      triggeredByMouseRef.current = true
      lastMouseRef.current = { x: e.clientX, y: e.clientY }
      // Small delay to let selection finalize
      defer(checkSelection, 50)
    }

    const onKeyUp = (e: KeyboardEvent) => {
      // A hidden tab's toolbar owns no key: its composer may still be `visible`
      // (a draft kept it open across the switch), and reacting to a global
      // Escape from here would pop its discard prompt over the tab in front.
      if (suspendedRef.current) return
      // Keys typed INTO the composer are not selection gestures: a shifted
      // letter or Shift+Enter would otherwise re-run the check against the
      // now-collapsed document selection and close the box mid-sentence. The
      // composer owns Escape on its own root (see `ComposerBox`).
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      if (e.key === 'Escape') {
        // With the composer open but focus elsewhere (a click-away that kept a
        // draft), Escape takes the composer's own path: the discard prompt over
        // a draft, the selection handed back — not a silent close.
        if (composerRef.current && visibleRef.current) { void composerEscapeRef.current() } else setVisible(false)
        return
      }
      // Check selection on Shift+Arrow keys (keyboard selection)
      if (e.shiftKey) {
        triggeredByMouseRef.current = false
        defer(checkSelection, 50)
      }
    }

    const onMouseDown = (e: MouseEvent) => {
      if (suspendedRef.current) return
      // Don't dismiss if clicking inside the toolbar
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      // A typed draft is not discarded by a stray click: the box stays until the
      // user submits it, or closes it deliberately (Escape, the close button).
      if (composerDraftRef.current) return
      // Clicking inside the container clears the selection (cursor reposition) —
      // dismiss after a tick so the new (empty) selection state is readable.
      if (containerRef.current && containerRef.current.contains(e.target as Node)) {
        defer(() => { if (!window.getSelection()?.toString().trim()) setVisible(false) }, 0)
        return
      }
      setVisible(false)
    }

    // Keyboard path into an UNFOCUSED composer (opened by Shift+Arrow or touch,
    // see `composerAutoFocus`): a bare Enter with the selection still in the
    // document moves the caret into the input. Only while the box is open, only
    // from a non-editable target, and never with modifiers, so it cannot steal a
    // real Enter from a form field or a shortcut.
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Enter' || e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return
      const input = composerInputRef.current
      if (!input || !composerRef.current) return
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      const target = e.target as HTMLElement | null
      if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return
      // A control the user Tabbed to (Submit All, a tree row, a link in the
      // preview) keeps its own Enter: only a bare document target is ours.
      if (target && target.closest('button, a, summary, [role="button"], [role="link"], [role="treeitem"], [role="menuitem"], [role="tab"], [tabindex]')) return
      if (suspendedRef.current) return
      e.preventDefault()
      input.focus()
    }

    // Touch devices never fire `mouseup` for text selection — the selection is
    // made by long-press then adjusted with drag handles, so the mouse-based
    // triggers above never run and the toolbar never appears. `selectionchange`
    // is the reliable cross-mobile signal: it fires as the selection grows and
    // again each time a handle settles. Debounce so the toolbar only appears
    // once the user stops adjusting (avoids flicker mid-drag), and gate to touch
    // so desktop drag-select — which already works via `mouseup` and would show
    // the toolbar prematurely mid-drag under this path — is left unchanged.
    let selectionChangeTimer: ReturnType<typeof setTimeout> | null = null
    const onSelectionChange = () => {
      if (!isTouchDevice()) return
      // The caret moving inside the composer's input fires this too; that is
      // typing, not a new text selection, and must not close the box.
      if (toolbarRef.current && document.activeElement && toolbarRef.current.contains(document.activeElement)) return
      // With the box open (and, on touch, NOT focused), the host's own <mark>
      // wrapping collapses the document selection and fires this event. An
      // EMPTY selection is therefore not a dismissal here — the box has its own
      // ways out (✕, Escape, tap-away); only a NEW non-empty selection re-runs
      // the check.
      if (visibleRef.current && composerRef.current && !window.getSelection()?.toString().trim()) return
      if (selectionChangeTimer) clearTimeout(selectionChangeTimer)
      // No mouse anchor on touch — checkSelection falls back to the selection
      // rect for positioning when triggeredByMouse is false.
      triggeredByMouseRef.current = false
      selectionChangeTimer = setTimeout(checkSelection, 350)
    }

    document.addEventListener('mouseup', onMouseUp)
    document.addEventListener('keyup', onKeyUp)
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('selectionchange', onSelectionChange)
    return () => {
      document.removeEventListener('mouseup', onMouseUp)
      document.removeEventListener('keyup', onKeyUp)
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('selectionchange', onSelectionChange)
      if (selectionChangeTimer) clearTimeout(selectionChangeTimer)
      // Cancel every deferred check still in flight. `checkSelection` depends
      // only on the stable `containerRef`, so this effect does not re-run after
      // mount and this cleanup is effectively unmount-only.
      for (const id of pending) clearTimeout(id)
      pending.clear()
    }
    // `containerRef` is a stable RefObject (its identity never changes across
    // renders), so listing it does not re-run the effect; it satisfies the
    // linter without changing the listener lifecycle.
  }, [checkSelection, containerRef])

  const flashCopied = useCallback(() => {
    setCopiedId('copy')
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    copyTimerRef.current = setTimeout(() => {
      copyTimerRef.current = null
      setCopiedId(null)
    }, 1500)
  }, [])

  const handleAction = useCallback((action: SelectionAction) => {
    const text = selectedTextRef.current
    if (!text) return
    const rect = selectionRectRef.current || new DOMRect(0, 0, 0, 0)
    const outcome = action.onClick(text, rect)
    if (action.id === 'copy') {
      setCopyFailed(false)
      // A copy action that reports its result gets a truthful checkmark: shown
      // only when the clipboard took the text, and a refusal is surfaced (the
      // composer renders it; the plain row simply shows no checkmark). A void
      // action keeps the optimistic flash it always had.
      if (outcome && typeof (outcome as Promise<boolean>).then === 'function') {
        // `copyToClipboard` REJECTS when the execCommand fallback throws (its
        // documented contract), so a rejection is a failure too, not a crash.
        void (outcome as Promise<boolean>).then(
          ok => { if (ok) flashCopied(); else setCopyFailed(true) },
          () => setCopyFailed(true),
        )
      } else {
        // A void copy action gives no result to wait for. Every in-repo copy
        // action now returns the promise; this is the contract's floor.
        flashCopied()
      }
    } else {
      // In composer mode the host's remaining actions are secondary; the box
      // closing here is a close, not a submit, and the visibility effect
      // reports it as such.
      setVisible(false)
      window.getSelection()?.removeAllRanges()
    }
  }, [flashCopied])

  // Cancel a pending "copied!" reset timer on unmount so it can never fire
  // after the component (or a test's jsdom environment) is torn down.
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
  }, [])

  // Every way the box can go away that is not a submit — click-away, the
  // selection collapsing, a non-copy action, Escape, unmount — ends up here
  // with `visible === false`, and the host hears `onClose` once.
  useEffect(() => {
    // A stale "Copy failed" must not greet the NEXT selection either.
    if (!visible) { onComposerDraftChange(false); setComposerText(''); setCopyFailed(false); fireComposerClose() }
  }, [visible, fireComposerClose, onComposerDraftChange])
  // Unmount (the host switched to edit/fullscreen, the file changed, or the
  // slot switched) is a close AND the end of any draft the host was told
  // about; otherwise its "not clean" flag would outlive the box that carried
  // the draft. The draft TEXT is not touched here: it lives in the host's
  // `draftStore` precisely so an unguarded teardown cannot lose it, and the
  // host clears it itself when one of its own guards discards.
  useEffect(() => () => { onComposerDraftChange(false); fireComposerClose() }, [fireComposerClose, onComposerDraftChange])

  const handleComposerSubmit = useCallback((comment: string) => {
    const text = selectedTextRef.current
    const trimmed = comment.trim()
    const activeComposer = composerRef.current
    // `composerClosedRef` also makes this idempotent: the exiting box is still
    // rendered with its last props for the length of the exit animation, so a
    // second click inside that window must not append the comment twice.
    if (!text || !trimmed || !activeComposer || composerClosedRef.current) return
    // Submit and close are exclusive: mark closed BEFORE hiding so the
    // visibility effect does not also report a close.
    composerClosedRef.current = true
    onComposerDraftChange(false)
    setComposerText('')
    activeComposer.draftStore?.clear(text, savedSelectionRef.current?.start ?? -1)
    activeComposer.onSubmit(trimmed, text)
    setVisible(false)
    window.getSelection()?.removeAllRanges()
  }, [onComposerDraftChange])

  // Escape (or the close button) closes the box but KEEPS the selection: focus
  // in the input had collapsed it, so it is rebuilt from the saved offsets —
  // after the host's `onClose` has removed its <mark>s, so the range is built
  // over the restored text nodes rather than around wrappers that are about to
  // be unwrapped.
  const composerEscapeRef = useRef<() => Promise<void>>(async () => {})
  const handleComposerEscape = useCallback(async () => {
    // Same idempotence as submit: a box already on its way out ignores ✕/Escape.
    if (composerClosedRef.current) return
    // A typed draft gets the same "discard?" the host asks before Edit /
    // full screen / close: Escape is the most-pressed key on the box, and
    // without this it was the one path that lost a multi-line comment silently.
    const activeComposer = composerRef.current
    if (composerDraftRef.current && activeComposer?.confirmDiscard) {
      const discard = await activeComposer.confirmDiscard()
      if (!discard) { composerInputRef.current?.focus(); return }
    }
    // Escape/✕ (confirmed when there was a draft) is a deliberate discard — of
    // THIS passage's draft. A draft saved for another passage is not touched.
    if (composerDraftRef.current) activeComposer?.draftStore?.clear(selectedTextRef.current, savedSelectionRef.current?.start ?? -1)
    fireComposerClose()
    onComposerDraftChange(false)
    setComposerText('')
    setVisible(false)
    const saved = savedSelectionRef.current
    const container = containerRef.current
    if (!saved || !container) return
    const range = rangeFromTextOffset(container, saved.start, saved.length)
    const sel = window.getSelection()
    if (!range || !sel) return
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    sel.removeAllRanges()
    sel.addRange(range)
  }, [containerRef, fireComposerClose, onComposerDraftChange])

  useEffect(() => { composerEscapeRef.current = handleComposerEscape }, [handleComposerEscape])

  // Cmd/Ctrl+C in an EMPTY input copies the document selection the box is
  // annotating. Focus stole that selection from the browser, so without this
  // the reflex "select, copy" would copy an empty textarea — the one regression
  // a type-first box must not introduce. Result-aware: the checkmark shows only
  // when the clipboard actually took the text; a refusal is reported by the box.
  const handleComposerCopyShortcut = useCallback(async (): Promise<boolean> => {
    const text = selectedTextRef.current
    if (!text) return false
    setCopyFailed(false)
    // A rejection (execCommand fallback threw) is a failed copy, not a crash.
    const ok = await copyToClipboard(text).catch(() => false)
    if (ok) flashCopied(); else setCopyFailed(true)
    return ok
  }, [flashCopied])

  const onComposerGrow = useCallback(() => setComposerGrowTick(t => t + 1), [])
  const dismissCopyFailed = useCallback(() => setCopyFailed(false), [])
  const onComposerTextChange = useCallback((next: string) => {
    setComposerText(next)
    onComposerDraftChange(next.trim().length > 0)
    const store = composerRef.current?.draftStore
    if (store) {
      const start = savedSelectionRef.current?.start ?? -1
      if (next.trim()) store.write(next, selectedTextRef.current, start); else store.clear(selectedTextRef.current, start)
    }
  }, [onComposerDraftChange])

  // d. The box is `position: fixed`, so a scroll moves the selected text out
  // from under it. Re-anchor from the saved text offsets on every scroll of
  // any ancestor (capture phase catches the panel's own scroll box), throttled
  // to a frame. The old popover simply dismissed on scroll; a typed draft must
  // not be lost to that, so the box follows instead.
  useEffect(() => {
    if (!visible || !composer) return
    let frame: number | null = null
    const onScroll = () => {
      if (frame != null) return
      frame = requestAnimationFrame(() => {
        frame = null
        const saved = savedSelectionRef.current
        const container = containerRef.current
        if (!saved || !container) return
        const range = rangeFromTextOffset(container, saved.start, saved.length)
        if (!range) return
        const rect = range.getBoundingClientRect()
        if (rect.width === 0 && rect.height === 0) return
        selectionRectRef.current = rect
        setPos({ x: rect.left, y: rect.bottom + 8 })
      })
    }
    document.addEventListener('scroll', onScroll, { capture: true, passive: true })
    // A viewport resize moves the text too (reflow) and can strand a fixed box
    // past the new edge; same re-anchor, and the clamp effect re-measures.
    window.addEventListener('resize', onScroll)
    return () => {
      document.removeEventListener('scroll', onScroll, { capture: true })
      window.removeEventListener('resize', onScroll)
      if (frame != null) cancelAnimationFrame(frame)
    }
  }, [visible, composer, containerRef])

  return createPortal(
    <AnimatePresence>
      {visible && !suspended && (
        <motion.div
          ref={toolbarRef}
          initial={{ opacity: 0, y: 4, scale: 0.95 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 4, scale: 0.95 }}
          transition={{ duration: 0.15 }}
          className="fixed z-[9999] pointer-events-auto"
          // `clampedPos` is the true top-left after measurement. No CSS
          // translate — framer-motion owns `transform` for its animation and
          // would drop it (see the layout effect above).
          style={{ left: clampedPos.x, top: clampedPos.y }}
        >
          {composer ? (
            <ComposerBox
              inputRef={composerInputRef}
              autoFocus={composerAutoFocus}
              actions={actions}
              copiedId={copiedId}
              hintIdBase={hintIdBase}
              onAction={handleAction}
              onSubmit={handleComposerSubmit}
              onEscape={handleComposerEscape}
              onCopyShortcut={handleComposerCopyShortcut}
              onGrow={onComposerGrow}
              text={composerText}
              onTextChange={onComposerTextChange}
              copyFailed={copyFailed}
              onDismissCopyFailed={dismissCopyFailed}
            />
          ) : (
          <div className="flex flex-wrap items-center gap-0.5 p-0.5 rounded-lg bg-bg-elevated border border-border shadow-lg">
            {actions.map(action => (
              <Fragment key={action.id}>
                <button
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium text-text hover:text-accent hover:bg-bg-hover transition-colors cursor-pointer whitespace-nowrap"
                  onMouseDown={e => e.preventDefault()}
                  onClick={() => handleAction(action)}
                  aria-label={action.label}
                  // The hint is the tooltip AND the accessible description: a
                  // keyboard or screen-reader user never sees a hover title.
                  aria-describedby={action.hint ? `${hintIdBase}-${action.id}` : undefined}
                  title={action.hint ?? action.label}
                >
                  {copiedId === action.id ? <Check size={12} className="text-ok" /> : action.icon}
                  {action.label}
                </button>
                {/* Sibling, not child: the button's own text stays the label
                    (the capture harnesses and tests read it as such). */}
                {action.hint && <span id={`${hintIdBase}-${action.id}`} className="sr-only">{action.hint}</span>}
              </Fragment>
            ))}
            {/* No hand-off: this row floats over a chat whose composer may hold
                an unsent draft; the hand-off navigates and would discard it. A
                refused clipboard write is recoverable in place anyway. */}
            {copyFailed && (
              <ErrorNotice
                variant="inline"
                className="basis-full px-2 py-1"
                message={i18nT('components.selectionToolbar.copy_failed')}
                onDismiss={dismissCopyFailed}
              />
            )}
          </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body
  )
}

/** `true` below `COMPOSER_STACK_BELOW_PX`; tracks window resizes. */
function useStackedComposer(): boolean {
  const [stacked, setStacked] = useState(() => window.innerWidth < COMPOSER_STACK_BELOW_PX)
  useEffect(() => {
    const onResize = () => setStacked(window.innerWidth < COMPOSER_STACK_BELOW_PX)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  return stacked
}

const COMPOSER_MAX_INPUT_H = 160

/**
 * The type-first annotation box: the comment input, a labelled "Add comment"
 * button next to it, and — past a divider, so they read as acting on the
 * SELECTION rather than on the draft — the host's actions as icon buttons plus
 * a close button. Stacked (narrow viewport), the input takes the full width and
 * that row sits beneath it.
 *
 * Enter submits, Shift+Enter breaks the line, Escape or ✕ closes and hands the
 * selection back. The icon buttons work with an empty input — a user who only
 * wants Copy loses nothing to the box being there — and `onMouseDown` prevents
 * default on every one of them so a click never pulls focus out of the input.
 *
 * `autoFocus` is false when the selection is still being made by an
 * incremental modality (Shift+Arrow, touch handles): the box then opens with
 * its placeholder naming the way in (Enter), and the toolbar's document-level
 * Enter handler moves focus here.
 */
function ComposerBox({ inputRef, autoFocus, actions, copiedId, hintIdBase, onAction, onSubmit, onEscape, onCopyShortcut, onGrow, text, onTextChange, copyFailed, onDismissCopyFailed }: {
  inputRef: React.RefObject<HTMLTextAreaElement>
  autoFocus: boolean
  actions: SelectionAction[]
  copiedId: string | null
  hintIdBase: string
  onAction: (action: SelectionAction) => void
  onSubmit: (comment: string) => void
  onEscape: () => void | Promise<void>
  /** Resolves true when the clipboard took the text. */
  onCopyShortcut: () => Promise<boolean>
  onGrow: () => void
  /** Draft text, owned by the toolbar so it survives a suspension. */
  text: string
  onTextChange: (next: string) => void
  /** A clipboard write (icon or shortcut) was refused; owned by the toolbar. */
  copyFailed: boolean
  onDismissCopyFailed: () => void
}) {
  const ime = useImeGuard()
  const stacked = useStackedComposer()
  // The placeholder names the way IN while the caret is elsewhere, and reads
  // as an ordinary prompt once it is here — whichever modality opened the box
  // (a keyboard open that the user then Enters into must not keep saying
  // "Press Enter").
  const [focused, setFocused] = useState(false)

  // Focus on the frame after mount — the box is the point of the feature, and
  // waiting one frame lets framer-motion place it before the caret lands. A
  // later flip of `autoFocus` (Shift+Arrow open, then a mouse re-select) focuses
  // too; the reverse never blurs, since the caret was the user's own doing.
  useEffect(() => {
    if (!autoFocus) return
    const frame = requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      // A restored draft continues from its end, not from before its first word.
      const end = el.value.length
      el.setSelectionRange(end, end)
    })
    return () => cancelAnimationFrame(frame)
  }, [autoFocus, inputRef])

  const autoGrow = useCallback((el: HTMLTextAreaElement) => {
    el.style.height = 'auto'
    const next = Math.min(el.scrollHeight, COMPOSER_MAX_INPUT_H)
    el.style.height = next + 'px'
    el.style.overflowY = el.scrollHeight > COMPOSER_MAX_INPUT_H ? 'auto' : 'hidden'
    onGrow()
  }, [onGrow])

  const canSubmit = text.trim().length > 0
  const submitLabel = i18nT('components.commentOverlay.add_comment')
  const iconBtn = 'flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-accent hover:bg-bg-hover transition-colors cursor-pointer bg-transparent border-none'

  const controls = (
    <div className={`flex items-center gap-1 shrink-0 ${stacked ? 'justify-between' : ''}`}>
      <button
        type="button"
        className="flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] font-medium text-accent hover:bg-bg-hover transition-colors cursor-pointer bg-transparent border-none whitespace-nowrap disabled:opacity-40 disabled:cursor-default"
        onMouseDown={e => e.preventDefault()}
        onClick={() => canSubmit && onSubmit(text)}
        disabled={!canSubmit}
        aria-label={submitLabel}
        title={submitLabel}
      >{/* A plus, not a paper plane: this ADDS to the pending list; the list's
          own Submit All is what sends. */}<MessageSquarePlus size={13} />{submitLabel}</button>
      <div
        role="group"
        aria-label={i18nT('components.selectionToolbar.more_actions')}
        className="flex items-center gap-0.5 shrink-0 pl-1 border-l border-border"
      >
        {actions.map(action => (
          <Fragment key={action.id}>
            <button
              type="button"
              className="flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium text-muted hover:text-accent hover:bg-bg-hover transition-colors cursor-pointer bg-transparent border-none whitespace-nowrap"
              onMouseDown={e => e.preventDefault()}
              onClick={() => onAction(action)}
              aria-label={action.label}
              aria-describedby={action.hint ? `${hintIdBase}-${action.id}` : undefined}
              title={action.hint ?? action.label}
            >
              {copiedId === action.id ? <Check size={14} className="text-ok" /> : action.icon}
              {/* The verb stays visible: an unlabelled two-squares glyph next to a
                  comment box reads as "copy the comment", and a first-time user
                  avoids it rather than guess. */}
              {action.label}
            </button>
            {action.hint && <span id={`${hintIdBase}-${action.id}`} className="sr-only">{action.hint}</span>}
          </Fragment>
        ))}
        <button
          type="button"
          className={iconBtn}
          onMouseDown={e => e.preventDefault()}
          onClick={() => { void onEscape() }}
          aria-label={i18nT('components.commentOverlay.close')}
          title={i18nT('components.commentOverlay.close')}
        ><X size={14} /></button>
      </div>
    </div>
  )

  return (
    // The box owns Escape for everything inside it (input AND buttons), since
    // the document-level Escape handler deliberately ignores keys that
    // originate here.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      data-testid="selection-composer"
      data-layout={stacked ? 'stack' : 'row'}
      className={`rounded-lg bg-bg-elevated border border-border shadow-lg p-1.5 flex gap-1.5 ${stacked ? 'flex-col w-[calc(100vw-16px)]' : 'items-start flex-wrap w-[520px] max-w-[calc(100vw-16px)]'}`}
      onKeyDown={e => {
        if (e.key !== 'Escape') return
        // An Escape that cancels an IME candidate list belongs to the IME:
        // closing on it would discard the draft mid-composition.
        if (!ime.claimKey(e)) return
        ime.reset()
        e.preventDefault()
        e.stopPropagation()
        void onEscape()
      }}
    >
      <textarea
        ref={inputRef}
        aria-label={i18nT('components.selectionToolbar.comment_input')}
        placeholder={focused || autoFocus
          ? i18nT('components.commentOverlay.write_a_comment')
          : isTouchDevice() ? i18nT('components.selectionToolbar.tap_to_comment') : i18nT('components.selectionToolbar.press_enter_to_comment')}
        value={text}
        rows={1}
        onChange={e => { onTextChange(e.target.value); autoGrow(e.target) }}
        {...ime.bindComposition({ onFocus: () => setFocused(true), onBlur: () => setFocused(false) })}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey) {
            // An empty Enter is swallowed (nothing to submit, and the newline
            // it would insert reads as a phantom submit). Shift+Enter is the
            // newline and falls through to the textarea.
            if (!canSubmit) { e.preventDefault(); return }
            if (ime.claimEnter(e)) { e.stopPropagation(); onSubmit(text) }
            return
          }
          // Only an EMPTY input hands Copy to the document selection; once a
          // draft exists the shortcut is the textarea's own.
          if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'c' && text.length === 0) {
            e.preventDefault()
            void onCopyShortcut()
          }
        }}
        className={`bg-bg border border-border rounded-md px-2.5 py-1.5 text-text text-[13px] font-body outline-none focus-ring resize-none leading-[20px] overflow-hidden placeholder:whitespace-nowrap placeholder:overflow-hidden placeholder:text-ellipsis ${stacked ? 'w-full' : 'flex-1 min-w-[140px]'}`}
      />
      {controls}
      {/* No hand-off: the comment draft in this box's textarea is unsaved, and
          the agent hand-off navigates to the chat, which would discard it. A
          refused clipboard write is also recoverable in place (select and copy
          again), so there is nothing an agent could do for it. */}
      {copyFailed && (
        <ErrorNotice
          variant="inline"
          className="basis-full"
          message={i18nT('components.selectionToolbar.copy_failed')}
          onDismiss={onDismissCopyFailed}
        />
      )}
    </div>
  )
}

/** Pre-built actions for common use cases */
export function useSelectionActions(
  onQuote?: (text: string, rect: DOMRect) => void,
  onAsk?: (text: string, rect: DOMRect) => void,
): SelectionAction[] {
  const actions: SelectionAction[] = []

  if (onQuote) {
    actions.push({
      id: 'quote',
      icon: <MessageSquareQuote size={12} />,
      label: i18nT('components.selectionToolbar.quote'),
      // The pair's hints disambiguate each other: this one names the main
      // conversation as the destination, Ask's names the Side Chat.
      hint: i18nT('components.selectionToolbar.quote_hint'),
      onClick: onQuote,
    })
  }

  // "Ask" opens the isolated /side conversation seeded with the selection so
  // the user can ask a scoped follow-up WITHOUT polluting the main chat
  // context (unlike Quote, which injects into the main composer). The label
  // says what the action does to the selection ("ask about this"); WHERE the
  // question lands is the hint's job — a first-time reader has not met the
  // Side Chat panel yet, so naming it on the button explained nothing.
  if (onAsk) {
    actions.push({
      id: 'ask',
      icon: <MessageCircleQuestionMark size={12} />,
      label: i18nT('components.selectionToolbar.ask'),
      hint: i18nT('components.selectionToolbar.ask_hint'),
      onClick: onAsk,
    })
  }

  actions.push({
    id: 'copy',
    icon: <Copy size={12} />,
    label: i18nT('components.selectionToolbar.copy'),
    // Returns the clipboard result so the toolbar's checkmark is truthful
    // (and a refusal is reported rather than flashed as success).
    onClick: (text) => copyToClipboard(text),
  })

  return actions
}
