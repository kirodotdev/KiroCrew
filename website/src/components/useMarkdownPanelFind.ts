import { useState, useEffect, useRef, useCallback, type RefObject } from 'react'
import { hasCommandModifier } from '../utils/commandModifier'
import { useLocalCrewHidden } from './overlayRoot'
import { matchRanges, searchRuns, watchText } from '../utils/pierreShadow'

/**
 * `MarkdownPanel`'s CSS Custom Highlight registry, and the preview find (Cmd+F)
 * that paints through it: term, case, match ranges, stepping, and the
 * capture-phase routing of the chord between the preview, the editor and chat
 * find. The panel's comment-anchor highlight reads the same registry handles.
 * `MarkdownPanelFindBar` draws the bar from the hook's result.
 */

// ── CSS Custom Highlight API accessors ───────────────────────────────────────
// Preview find AND the annotation selection paint via the browser-native CSS
// Custom Highlight API (CSS.highlights + Range) instead of injecting <mark>
// nodes. The preview is React-reconciled (react-markdown), so mutating its DOM
// would crash React on the next re-render; ranges live outside the DOM and
// never touch it.
// These types aren't in this TS lib yet, so we reach them through narrow casts
// and feature-detect at runtime (graceful no-highlight fallback when absent).
type FindHighlight = object
export const FindHighlightCtor: (new (...ranges: Range[]) => FindHighlight) | undefined =
  typeof window !== 'undefined'
    ? (window as unknown as { Highlight?: new (...r: Range[]) => FindHighlight }).Highlight
    : undefined
export const cssHighlights: { set(n: string, h: FindHighlight): void; delete(n: string): boolean } | undefined =
  typeof CSS !== 'undefined'
    ? (CSS as unknown as { highlights?: { set(n: string, h: FindHighlight): void; delete(n: string): boolean } }).highlights
    : undefined
export const FIND_HL_SUPPORTED = !!FindHighlightCtor && !!cssHighlights
// Global registry names, shared by every mounted panel. Only one panel ever has
// find open, because the `active` gate refuses the chord in every tab but the
// visible one — so the names cannot be contended. Were two previews ever to
// search at once they would overlap visually, never crash.
const FIND_HL_ALL = 'mc-find'
const FIND_HL_CURRENT = 'mc-find-current'
// The annotation (open-composer) highlight lives in utils/annotationHighlight:
// the artifact hosts paint through the same registry name, and ranges are
// aggregated per owner so one host's open/close cannot erase another's paint.

export function useMarkdownPanelFind({ active, editing, diffMode, isMarkdown, fullscreen, content, previewRef, fullscreenPreviewRef }: {
  active: boolean; editing: boolean; diffMode: boolean; isMarkdown: boolean; fullscreen: boolean; content: string
  previewRef: RefObject<HTMLDivElement>; fullscreenPreviewRef: RefObject<HTMLDivElement>
}) {
  // ── Preview-mode find (Cmd+F) ─────────────────────────────────────────────
  // Three surfaces compete for Cmd+F: the editor owns it while editing (it stops
  // propagation before anything else sees the key), and ChatPage's chat-find
  // owns it via a document-level *bubble* listener. The rendered markdown
  // PREVIEW is the only surface with no editor to capture the key, so today it
  // falls through to chat-find — the reported bug. This adds a find scoped to
  // the preview that wins over chat-find using a *capture-phase* listener +
  // stopImmediatePropagation, but only when this panel is the active region
  // and we're in markdown preview (edit surface and non-markdown are untouched).
  // Highlights paint via the CSS Custom Highlight API (Range objects outside
  // the DOM) so the react-markdown subtree is never mutated.
  // True while a remote Crew covers the local dashboard this panel lives in.
  const crewHidden = useLocalCrewHidden()
  const [findOpen, setFindOpen] = useState(false)
  const [findTerm, setFindTerm] = useState('')
  const [findCase, setFindCase] = useState(false)
  const [findIdx, setFindIdx] = useState(0)
  const [findCount, setFindCount] = useState(0)
  const findInputRef = useRef<HTMLInputElement>(null)
  const findRangesRef = useRef<Range[]>([])
  // Is this panel the region the cursor is in? Defaults true so Cmd+F right
  // after opening the doc searches the doc. Flips based on where the last
  // pointer-down landed.
  //
  // The selector is deliberately unscoped. It answers "did the pointer land in
  // SOME markdown panel", which every mounted instance answers identically —
  // and that is sufficient because the `active` gate below already refuses the
  // chord in every panel but the visible one, and a pointer cannot land inside
  // a `display:none` sibling.
  const findActiveRef = useRef(true)

  useEffect(() => {
    const onPointer = (e: Event) => {
      const t = e.target as Element | null
      findActiveRef.current = !!t?.closest?.('[data-mc-mdpanel]')
    }
    document.addEventListener('pointerdown', onPointer, true)
    return () => document.removeEventListener('pointerdown', onPointer, true)
  }, [])

  // Becoming the visible tab is itself the signal that the user is looking at
  // this document — the same reason the ref defaults to true on mount. Without
  // this, a click in a sibling tab before the switch would leave the now-visible
  // panel out of its own find region.
  useEffect(() => { if (active) findActiveRef.current = true }, [active])

  // Highlights are external (CSS.highlights), so clearing never touches the
  // React-owned DOM — no reconciliation hazard.
  const clearFindMarks = useCallback(() => {
    findRangesRef.current = []
    if (cssHighlights) { cssHighlights.delete(FIND_HL_ALL); cssHighlights.delete(FIND_HL_CURRENT) }
  }, [])

  // Re-register the two highlights so `idx` paints as the current match and the
  // rest paint as plain matches. Cheap; called on every step.
  const paintFind = useCallback((ranges: Range[], idx: number) => {
    if (!FIND_HL_SUPPORTED || !FindHighlightCtor || !cssHighlights) return
    const others = ranges.filter((_, i) => i !== idx)
    cssHighlights.set(FIND_HL_ALL, new FindHighlightCtor(...others))
    const cur = ranges[idx]
    cssHighlights.set(FIND_HL_CURRENT, new FindHighlightCtor(...(cur ? [cur] : [])))
  }, [])

  const paintFindCurrent = useCallback((idx: number) => {
    const ranges = findRangesRef.current
    paintFind(ranges, idx)
    // Range has no scrollIntoView; scroll the match's nearest element instead.
    ranges[idx]?.startContainer.parentElement?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [paintFind])

  // `keep` is the match to stay on when the document changed under a find that is
  // already open; omitted, the term is new and the find starts over.
  const runFind = useCallback((term: string, caseSensitive: boolean, keep?: number) => {
    clearFindMarks()
    const root = fullscreen ? fullscreenPreviewRef.current : previewRef.current
    if (!root || !term) { setFindCount(0); setFindIdx(0); return }
    const re = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), caseSensitive ? 'g' : 'gi')
    // Code blocks are drawn into shadow roots a TreeWalker cannot enter; the
    // shared runs read them too, one source line at a time.
    const ranges = matchRanges(searchRuns(root), re)
    findRangesRef.current = ranges
    setFindCount(ranges.length)
    const idx = keep === undefined ? 0 : Math.min(keep, Math.max(ranges.length - 1, 0))
    setFindIdx(idx)
    if (!ranges.length) return
    paintFind(ranges, idx)
    if (keep === undefined) ranges[0].startContainer.parentElement?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [clearFindMarks, paintFind, fullscreen, previewRef, fullscreenPreviewRef])

  const stepFind = useCallback((dir: number) => {
    const n = findRangesRef.current.length
    if (!n) return
    setFindIdx((prev) => { const next = (prev + dir + n) % n; paintFindCurrent(next); return next })
  }, [paintFindCurrent])

  const closeFind = useCallback(() => {
    clearFindMarks()
    setFindOpen(false)
    setFindTerm('')
    setFindCount(0)
    setFindIdx(0)
  }, [clearFindMarks])

  // Recompute matches as the term/case/content/view changes while open. With
  // the CSS Highlight API there's no DOM to clean up if `content` re-renders
  // mid-find — stale ranges simply stop painting and are rebuilt here.
  useEffect(() => {
    if (!findOpen) return
    runFind(findTerm, findCase)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runFind is stable per `fullscreen`; listing it would re-run on every match repaint
  }, [findOpen, findTerm, findCase, content, fullscreen])

  // A code block is swapped for its highlighted copy after a find has run, which
  // drops the ranges taken on the first one; find again on the same match.
  useEffect(() => {
    const root = fullscreen ? fullscreenPreviewRef.current : previewRef.current
    if (!findOpen || !findTerm || !root) return
    return watchText(root, () => runFind(findTerm, findCase, findIdx))
  }, [findOpen, findTerm, findCase, findIdx, fullscreen, runFind, previewRef, fullscreenPreviewRef])

  // Find highlight names belong to the one active panel, so clear them if it
  // unmounts. Annotation ranges use per-owner cleanup in MarkdownPanel.tsx; deleting their
  // global registry entry here would erase ranges owned by other mounted tabs.
  useEffect(() => () => {
    if (cssHighlights) { cssHighlights.delete(FIND_HL_ALL); cssHighlights.delete(FIND_HL_CURRENT) }
  }, [])

  // Leaving preview (edit/diff) has no rendered DOM to search — close find so
  // the editor's own find takes over cleanly.
  useEffect(() => { if (editing || diffMode) closeFind() }, [editing, diffMode, closeFind])

  // A backgrounded tab must not keep a find bar the user cannot see, nor hold
  // the module-global highlight names away from the visible panel.
  useEffect(() => { if (!active) closeFind() }, [active, closeFind])

  // Capture-phase Cmd+F: fires before ChatPage's bubble-phase chat-find.
  //
  // Four side-panel surfaces compete for the chord, and this handler is the one
  // component that already knows which surface is on screen (`editing`,
  // `diffMode`, `isMarkdown`) AND whether the user is looking at it
  // (`findActiveRef`). So it is where the routing decision belongs:
  //
  //   • markdown preview — open our own in-document find (TreeWalker + CSS
  //     Highlight API over `previewRef`), stealing the key from chat-find.
  //   • edit mode — Pierre's editor binds the chord on its own content element
  //     and preventDefault()s it, and #6397's `defaultPrevented` guard in
  //     useMessageSearch already stops chat-find from stacking a second pane.
  //     Nothing to do here; bail so the editor keeps ownership.
  //   • code preview and read-only diff — there is no in-document find for
  //     these yet (code preview is windowed behind Pierre's <Virtualizer> so a
  //     DOM TreeWalker would silently under-report off-screen matches, and the
  //     diff renders outside `previewRef`; a real find for either is an open
  //     design question — see #6383). But chat-find answering the chord with
  //     the conversation is strictly worse than not answering, so we YIELD the
  //     key away from chat-find without opening anything. We only stop the
  //     event reaching chat-find's bubble-phase listener; we deliberately do
  //     NOT preventDefault(), so a native browser find bar (where one exists)
  //     still opens over the file, which is honest about what is on screen.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!hasCommandModifier(e) || e.key.toLowerCase() !== 'f') return
      if (!active || crewHidden) return                     // hidden tab or another Crew showing: never claim the key
      if (editing) return                                   // editor owns it (#6397)
      if (!findActiveRef.current) return                    // cursor is in chat → let chat-find handle
      if (diffMode || !isMarkdown) {
        // Code preview / read-only diff: no in-document find to open, but keep
        // chat-find from answering with the wrong content. Stopping the event
        // in the capture phase prevents chat-find's bubble-phase listener from
        // running at all; leaving the default action lets a native find bar
        // (browser) open over the file.
        e.stopImmediatePropagation()
        return
      }
      // Markdown preview: open our own in-document find.
      e.preventDefault()
      e.stopImmediatePropagation()                          // beat ChatPage's bubble-phase chat-find
      setFindOpen(true)
      requestAnimationFrame(() => { findInputRef.current?.focus(); findInputRef.current?.select() })
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [active, editing, diffMode, isMarkdown, crewHidden])

  return { findOpen, findTerm, setFindTerm, findCase, setFindCase, findIdx, findCount, findInputRef, stepFind, closeFind }
}
