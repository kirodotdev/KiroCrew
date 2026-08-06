import { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageSquareQuote, MessageCircleQuestion, Copy, Check } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { isTouchDevice } from '../utils/isTouchDevice'

export interface SelectionAction {
  id: string
  icon: React.ReactNode
  label: string
  /** Called with selected text and the bounding rect of the selection */
  onClick: (text: string, rect: DOMRect) => void
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
  /** External trigger (e.g. from Monaco) — shows toolbar at given position with given text */
  externalSelection?: { text: string; x: number; y: number } | null
}

/** Generic floating toolbar that appears when user selects text within a container.
 *  Extensible — pass any actions (quote, copy, etc.) via the `actions` prop. */
export default function SelectionToolbar({ containerRef, actions, externalSelection }: SelectionToolbarProps) {
  const [visible, setVisible] = useState(false)
  const [pos, setPos] = useState({ x: 0, y: 0 })
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

  const checkSelection = useCallback(() => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      // Only dismiss if toolbar was shown by DOM selection (not external/Monaco)
      if (sourceRef.current === 'dom') setVisible(false)
      return
    }

    const container = containerRef.current
    if (!container) { setVisible(false); return }

    // Ensure selection is within our container
    const range = sel.getRangeAt(0)
    if (!container.contains(range.commonAncestorContainer)) {
      setVisible(false)
      return
    }

    const text = selectionTextFrom(sel)
    if (!text) { setVisible(false); return }

    selectedTextRef.current = text

    const rect = range.getBoundingClientRect()
    selectionRectRef.current = rect
    const x = triggeredByMouseRef.current
      ? lastMouseRef.current.x
      : rect.left + rect.width / 2
    const y = triggeredByMouseRef.current
      ? lastMouseRef.current.y + 8
      : rect.bottom + 8
    setPos({ x, y })
    sourceRef.current = 'dom'
    setVisible(true)
  }, [containerRef])

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
    const left = Math.max(margin, Math.min(pos.x - w / 2, vw - w - margin))
    // Flip above the anchor when it would overflow the bottom edge.
    const top = pos.y + h + margin > vh ? Math.max(margin, pos.y - h - margin) : pos.y
    // Compare against the ref (not state) so clampedPos stays out of the deps —
    // the effect runs once per pos change instead of twice.
    if (left !== clampedRef.current.x || top !== clampedRef.current.y) {
      clampedRef.current = { x: left, y: top }
      setClampedPos({ x: left, y: top })
    }
  }, [visible, pos])

  // External trigger (Monaco selections that don't use window.getSelection)
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
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      triggeredByMouseRef.current = true
      lastMouseRef.current = { x: e.clientX, y: e.clientY }
      // Small delay to let selection finalize
      defer(checkSelection, 50)
    }

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setVisible(false); return }
      // Check selection on Shift+Arrow keys (keyboard selection)
      if (e.shiftKey) {
        triggeredByMouseRef.current = false
        defer(checkSelection, 50)
      }
    }

    const onMouseDown = (e: MouseEvent) => {
      // Don't dismiss if clicking inside the toolbar
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      // Clicking inside the container clears the selection (cursor reposition) —
      // dismiss after a tick so the new (empty) selection state is readable.
      if (containerRef.current && containerRef.current.contains(e.target as Node)) {
        defer(() => { if (!window.getSelection()?.toString().trim()) setVisible(false) }, 0)
        return
      }
      setVisible(false)
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
      if (selectionChangeTimer) clearTimeout(selectionChangeTimer)
      // No mouse anchor on touch — checkSelection falls back to the selection
      // rect for positioning when triggeredByMouse is false.
      triggeredByMouseRef.current = false
      selectionChangeTimer = setTimeout(checkSelection, 350)
    }

    document.addEventListener('mouseup', onMouseUp)
    document.addEventListener('keyup', onKeyUp)
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('selectionchange', onSelectionChange)
    return () => {
      document.removeEventListener('mouseup', onMouseUp)
      document.removeEventListener('keyup', onKeyUp)
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

  const handleAction = useCallback((action: SelectionAction) => {
    const text = selectedTextRef.current
    if (!text) return
    const rect = selectionRectRef.current || new DOMRect(0, 0, 0, 0)
    action.onClick(text, rect)
    if (action.id === 'copy') {
      setCopiedId('copy')
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
      copyTimerRef.current = setTimeout(() => {
        copyTimerRef.current = null
        setCopiedId(null)
      }, 1500)
    } else {
      setVisible(false)
      window.getSelection()?.removeAllRanges()
    }
  }, [])

  // Cancel a pending "copied!" reset timer on unmount so it can never fire
  // after the component (or a test's jsdom environment) is torn down.
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
  }, [])

  return createPortal(
    <AnimatePresence>
      {visible && (
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
          <div className="flex items-center gap-0.5 p-0.5 rounded-lg bg-bg-elevated border border-border shadow-lg">
            {actions.map(action => (
              <button
                key={action.id}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium text-text hover:text-accent hover:bg-bg-hover transition-colors cursor-pointer whitespace-nowrap"
                onMouseDown={e => e.preventDefault()}
                onClick={() => handleAction(action)}
                aria-label={action.label}
                title={action.label}
              >
                {copiedId === action.id ? <Check size={12} className="text-ok" /> : action.icon}
                {action.label}
              </button>
            ))}
          </div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body
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
      label: 'Quote',
      onClick: onQuote,
    })
  }

  // "Ask" opens the isolated /side conversation seeded with the selection so
  // the user can ask a scoped follow-up WITHOUT polluting the main chat
  // context (unlike Quote, which injects into the main composer).
  if (onAsk) {
    actions.push({
      id: 'ask',
      icon: <MessageCircleQuestion size={12} />,
      label: 'Ask in Side',
      onClick: onAsk,
    })
  }

  actions.push({
    id: 'copy',
    icon: <Copy size={12} />,
    label: 'Copy',
    onClick: (text) => { copyToClipboard(text) },
  })

  return actions
}
