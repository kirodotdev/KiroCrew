/**
 * Scroll a Monaco editor to a cited source line and flash it.
 *
 * Exists because agents cite code the way compilers do — `…/_dispatch.py:447` —
 * and a chip carrying that location should land the reader ON the line, not just
 * somewhere in the file. Monaco owns both halves rather than a hand-rolled
 * overlay: `revealLineInCenter` already accounts for wrapped and folded lines,
 * which arithmetic on a line height cannot, and a decoration survives resize,
 * re-render and scrolling for free.
 *
 * A hook in its own module, not inline in `MarkdownPanel`, so the clamp /
 * one-shot / cleanup behaviour is unit-testable against a fake editor instead of
 * requiring the whole panel and a real Monaco in jsdom.
 */
import { useCallback, useEffect, useRef } from 'react'
import type { Monaco } from '@monaco-editor/react'
import type { editor } from 'monaco-editor'
import type { IDisposable } from 'monaco-editor'

/**
 * A line to reveal, plus the request identity that makes a REPEAT reveal act.
 *
 * The nonce is load-bearing: clicking the same `file.py:447` chip twice produces
 * the same `line`, and a bare number would be `===` to the previous value, so
 * nothing would re-trigger and the second click would look broken after the
 * reader had scrolled away.
 */
export interface RevealTarget {
  /** 1-based, as every editor and compiler counts. */
  line: number
  /**
   * Last line of an inclusive range (`file.md:10-16`), or absent for a single
   * line. Always greater than `line` — `splitLineRef` collapses a reversed or
   * degenerate range to its start rather than passing one through.
   */
  endLine?: number
  nonce: number
}

/** Decoration classes — defined in index.css next to the find-highlight block. */
const LINE_CLASS = 'mc-line-reveal'
const GUTTER_CLASS = 'mc-line-reveal-gutter'

/**
 * How long the highlight stays fully lit before it starts fading, in ms.
 *
 * Transient on purpose: a permanent band would be misread as a selection or a
 * diagnostic once the reader moves on.
 */
export const REVEAL_HOLD_MS = 1800

/**
 * How long the highlight takes to fade to transparent, in ms.
 *
 * The fade is a CSS animation on the decoration class, NOT a `transition`. A
 * transition cannot run here: clearing the decoration removes Monaco's DOM
 * element outright, and a removed element does not animate — which is why the
 * highlight used to vanish in one frame despite the class carrying
 * `transition: background-color`. So the class animates itself to transparent on
 * a delay, and the JS below only clears the (already invisible) decoration after
 * the animation has finished.
 *
 * Framer Motion is the house animation system, but it cannot reach a Monaco
 * decoration: those are editor-owned DOM nodes, not React elements, so there is
 * no component whose presence `AnimatePresence` could keep alive. A keyframe in
 * `index.css` is the only mechanism available, and the `use-framer-motion` rule
 * scopes itself to TSX sources accordingly.
 */
export const REVEAL_FADE_MS = 1000

/**
 * Total lifetime of the highlight, in ms. Held at `flashCommentRow`'s 2800 so
 * the two "here is what you clicked" signals in this panel read as one idea.
 *
 * Both halves are published to CSS as custom properties on the editor container
 * (see `reveal`), so these constants are the single source of truth and the
 * stylesheet cannot drift from them.
 */
export const REVEAL_FLASH_MS = REVEAL_HOLD_MS + REVEAL_FADE_MS

/**
 * Wire an editor up to `target`.
 *
 * Returns the `onMount` handler to hand to `CodeEditor`. Monaco is lazy-loaded,
 * so mount is the earliest moment a model exists — which is why the reveal has
 * two entry points: the mount (first open, where the effect below has already
 * run and found no editor) and the nonce effect (a tab that is already open).
 * Both clear the previous decoration first, so a first open that happens to fire
 * both still leaves exactly one highlight.
 *
 * `onConsumed` fires after a reveal lands, so the owner can drop the target and
 * make it a true one-shot. Without it the target lingers on the tab record and
 * re-fires every time the panel remounts — switching chats and back would jump
 * the reader again, at a line they asked about once.
 */
export function useLineReveal(
  target: RevealTarget | undefined,
  onConsumed?: () => void,
): { onEditorMount: (ed: editor.IStandaloneCodeEditor, monaco: Monaco) => void } {
  const edRef = useRef<editor.IStandaloneCodeEditor | null>(null)
  const monacoRef = useRef<Monaco | null>(null)
  const decoRef = useRef<editor.IEditorDecorationsCollection | null>(null)
  const timerRef = useRef<number | undefined>(undefined)
  /** Line still being settled — re-centred on every layout change until the
   *  flash clears. See `onEditorMount`. */
  const pendingRef = useRef<{ start: number; end: number } | undefined>(undefined)
  const layoutSubRef = useRef<IDisposable | undefined>(undefined)
  /** Flips every reveal so consecutive reveals never request the same animation
   *  name — see the comment beside its use in `reveal`. */
  const flipRef = useRef(false)
  // Both read by onEditorMount, which is registered once and would otherwise
  // close over the values from the render that mounted the editor.
  const targetRef = useRef(target)
  targetRef.current = target
  const consumedRef = useRef(onConsumed)
  consumedRef.current = onConsumed

  const reveal = useCallback((line: number, endLine?: number): boolean => {
    const ed = edRef.current, monaco = monacoRef.current
    if (!ed || !monaco) return false
    const model = ed.getModel()
    if (!model) return false
    // Clamp rather than bail: a cited line can sit past the end of the file the
    // panel actually loaded (the file changed since the message, or it was read
    // truncated). Landing on the last line beats doing nothing silently. A range
    // clamps at both ends, and collapses to one line when the whole span is EOF.
    const lastLine = model.getLineCount()
    const start = Math.max(1, Math.min(line, lastLine))
    const end = endLine != null ? Math.max(start, Math.min(endLine, lastLine)) : start
    if (end > start) {
      // Fit the whole span on screen where it can, instead of centring the first
      // line and leaving the rest of the citation below the fold.
      ed.revealLinesInCenter(start, end)
    } else {
      ed.revealLineInCenter(start)
    }
    ed.setPosition({ lineNumber: start, column: 1 })
    // Publish the fade timings so index.css animates on the same numbers this
    // module schedules its clear against: the stylesheet cannot drift from the
    // constants, and the decoration is only removed once already invisible.
    const container = ed.getContainerDomNode?.()
    if (container) {
      container.style.setProperty('--mc-line-reveal-hold', `${REVEAL_HOLD_MS}ms`)
      container.style.setProperty('--mc-line-reveal-fade', `${REVEAL_FADE_MS}ms`)
      // Alternate the animation NAME so a repeat reveal actually restarts the fade.
      // Monaco builds whole-line decorations from an HTML string and
      // `ViewOverlayLine.renderLine` short-circuits on `_renderedContent === result`,
      // so clearing and re-adding an identical decoration in one synchronous block
      // leaves the very same DOM node in place — keeping its original animation
      // start time, which `forwards` has already pinned to transparent. Re-clicking
      // the same chip inside the flash window would then scroll but paint nothing,
      // which is precisely the case the nonce exists for. Changing the computed
      // `animation-name` cancels and restarts the animation even on a reused node.
      flipRef.current = !flipRef.current
      const suffix = flipRef.current ? '-b' : ''
      container.style.setProperty('--mc-line-reveal-anim', `mc-line-reveal-out${suffix}`)
      container.style.setProperty('--mc-line-reveal-gutter-anim', `mc-line-reveal-gutter-out${suffix}`)
    }
    // Mark the span as still-settling. On a first open the editor is mounted
    // before its container has been laid out, so "center" is computed against a
    // zero-height viewport and the line lands flush against the TOP edge —
    // losing exactly the preceding context a reader wants from a citation.
    // `automaticLayout` corrects the size later via a ResizeObserver, which is
    // why the fix hangs off onDidLayoutChange (below) rather than a frame count:
    // the observer fires after requestAnimationFrame, so guessing frames does
    // not close the gap. Cleared by the first layout with a real height — see
    // `onEditorMount` — with the flash timeout as the backstop for an editor
    // that never gets one.
    pendingRef.current = { start, end }
    // Deliberately NOT ed.focus(): the click came from the transcript, so
    // pulling focus into the editor would move the caret out of the chat input
    // mid-conversation.
    clearTimeout(timerRef.current)
    decoRef.current?.clear()
    decoRef.current = ed.createDecorationsCollection([{
      range: new monaco.Range(start, 1, end, 1),
      options: { isWholeLine: true, className: LINE_CLASS, linesDecorationsClassName: GUTTER_CLASS },
    }])
    timerRef.current = window.setTimeout(() => {
      decoRef.current?.clear()
      // The span has stopped being "the thing you just clicked", so stop
      // re-centring it — a later panel resize must not yank the reader back.
      pendingRef.current = undefined
    }, REVEAL_FLASH_MS)
    return true
  }, [])

  const onEditorMount = useCallback((ed: editor.IStandaloneCodeEditor, monaco: Monaco) => {
    edRef.current = ed
    monacoRef.current = monaco
    // Re-centre whenever the editor is re-laid-out while a reveal is still
    // fresh. This is what actually lands the line in the CENTRE: at mount the
    // container usually has no height yet, and `automaticLayout`'s
    // ResizeObserver supplies the real one a beat later.
    layoutSubRef.current?.dispose()
    layoutSubRef.current = ed.onDidLayoutChange(() => {
      const span = pendingRef.current
      if (span == null) return
      // This correction exists ONLY for the mount-time zero-height case, so it
      // retires the moment the editor has a real viewport: that layout change is
      // the one that centres correctly, and anything after it is an ordinary
      // resize. Without this, dragging the panel divider inside the flash window
      // would yank a reader who had already scrolled away to read around the line.
      if (ed.getLayoutInfo().height > 0) pendingRef.current = undefined
      if (span.end > span.start) ed.revealLinesInCenter(span.start, span.end)
      else ed.revealLineInCenter(span.start)
    })
    const pending = targetRef.current
    if (pending && reveal(pending.line, pending.endLine)) consumedRef.current?.()
  }, [reveal])

  useEffect(() => {
    if (target && reveal(target.line, target.endLine)) onConsumed?.()
    // `onConsumed` is deliberately not a dependency: it is read fresh through
    // consumedRef, and including it would re-run the reveal on every parent
    // re-render that produced a new callback identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, reveal])

  useEffect(() => () => {
    clearTimeout(timerRef.current)
    pendingRef.current = undefined
    layoutSubRef.current?.dispose()
    decoRef.current?.clear()
  }, [])

  return { onEditorMount }
}
