/**
 * Public entry for Pierre-rendered surfaces. Import ONLY from here (or
 * `./config` for option types) — never from `@pierre/diffs` directly — so the
 * heavy Shiki/Pierre runtime stays behind one lazy chunk and every surface
 * shares the config in `./config`.
 *
 * Each component suspends into a plain monospace `<pre>` of the raw text while
 * the chunk loads, so content is readable immediately (and test environments
 * that never resolve the chunk still render the text).
 *
 * `PierrePatch` additionally makes that plain render the FINAL one when the
 * user has turned off highlighted diffs (see `usePlainDiff`), which is why the
 * fallback component is a real surface here rather than a loading state.
 */
import { Suspense, forwardRef, lazy, memo, type CSSProperties, useContext, useEffect, useRef, useState } from 'react'
import type { BaseCodeOptions, FileContents } from '@pierre/diffs'
import type { PierreDiffOptions } from './config'
import type { EditorMarker, PierreEditorHandle } from './PierreEditorImpl'
import { PlainCodeFallback, PlainFilePairFallback } from './PlainCodeFallback'
import { isPierreFilePairWithinBudget } from './renderBudget'
import { computePairPatch } from './diffOffThread'
import { PierreFarmHoldContext } from '../components/pierreStaging'
import { usePlainDiff } from '../hooks/usePlainDiff'

const CodeImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierreCodeImpl })))
const PatchImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierrePatchImpl })))
const FilePairImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierreFilePairImpl })))
// The opted-in oversized pair renders its worker-computed patch through the
// SAME hunk renderer as chat patches (PierrePatchImpl): the patch arrives
// ready, so main-thread cost is parse + hunk render, proportional to changed
// lines, never file size. Only the header slots differ, passed as props.
const PairPatchImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierrePatchImpl })))
const EditorImpl = lazy(() => import('./PierreEditorImpl').then(m => ({ default: m.PierreEditorImpl })))

/** Identity of the pair a line-by-line request was made for — referential, so
 *  a prop change (new file objects) invalidates the request. */
interface PairIdentity {
  oldFile: FileContents | null
  newFile: FileContents | null
}

/**
 * Paint-hold for the opted-in oversized pair. Same contract as `WarmSwap` —
 * keep the readable fallback on screen until the impl has real painted rows —
 * but with a measurement that cannot be satisfied by anything except the
 * impl's own content: the children mount inside an IN-FLOW `height:0;
 * overflow:hidden` box, whose `clientHeight` is 0, so `scrollHeight` reads the
 * content's height exactly. (`WarmSwap`'s box is `absolute inset-0`, which
 * floors `scrollHeight` at the wrapper height the fallback itself provides —
 * fine for the staged, off-viewport mounts it was built for, wrong for a swap
 * happening under the user's cursor.) The children stay in the same DOM node
 * across the flip, so nothing remounts or re-highlights on reveal.
 */
function PatchPaintHold({ fallback, children, onVisible }: { fallback: React.ReactNode; children: React.ReactNode; onVisible?: () => void }) {
  const contentRef = useRef<HTMLDivElement | null>(null)
  const [painted, setPainted] = useState(false)
  const farm = useContext(PierreFarmHoldContext)
  useEffect(() => {
    if (painted) onVisible?.()
  }, [onVisible, painted])
  useEffect(() => {
    if (farm || painted) return
    const el = contentRef.current
    if (!el || typeof ResizeObserver === 'undefined') {
      setPainted(true)
      return
    }
    if (el.scrollHeight > WARM_PAINT_MIN_PX) {
      setPainted(true)
      return
    }
    const ro = new ResizeObserver(() => {
      if (el.scrollHeight > WARM_PAINT_MIN_PX) {
        setPainted(true)
        ro.disconnect()
      }
    })
    ro.observe(el)
    const deadline = setTimeout(() => setPainted(true), WARM_SWAP_DEADLINE_MS)
    return () => {
      ro.disconnect()
      clearTimeout(deadline)
    }
  }, [painted, farm])
  if (farm) return <>{fallback}</>
  return (
    <>
      <div
        style={painted ? undefined : { height: 0, overflow: 'hidden', visibility: 'hidden' }}
        aria-hidden={painted ? undefined : true}
      >
        <div ref={contentRef}>{children}</div>
      </div>
      {!painted && fallback}
    </>
  )
}

export type { EditorMarker, PierreEditorHandle }

/** A one-shot line-reveal request: `nonce` distinguishes repeat clicks on the
 *  same `file.py:447` chip, which would otherwise be `===` and re-fire nothing. */
export interface RevealTarget {
  line: number
  endLine?: number
  nonce: number
}

/* All public wrappers are memoized: a heavy file renders thousands of shadow
   DOM rows, so an unrelated ancestor re-render (sidebar toggle, chat
   keystroke) must stop here. Call sites keep `file`/`options` referentially
   stable via useMemo, so the bailout actually holds. */
export const PierreEditor = memo(forwardRef<PierreEditorHandle, {
  file: FileContents
  options?: BaseCodeOptions
  onChange: (contents: string) => void
  onSave?: () => void
  markers?: EditorMarker[]
  onCursorChange?: (line: number, column: number) => void
  /** Live-diff editing: baseline contents to diff against while editing
   *  (`null` = new file). `undefined` renders the plain editor. */
  diffBase?: string | null
  /** Split vs unified layout for the live-diff surface. */
  diffSplit?: boolean
  /** Show unchanged regions in the live-diff surface instead of folding them. */
  diffExpandUnchanged?: boolean
  className?: string
}>(function PierreEditor(props, ref) {
  return (
    <Suspense fallback={<PlainCodeFallback text={props.file.contents} />}>
      <EditorImpl ref={ref} {...props} />
    </Suspense>
  )
}))


/** A surface under this height is still Pierre's pre-highlight empty shell,
 *  not painted content: one rendered line inside body padding exceeds it. */
const WARM_PAINT_MIN_PX = 24
/** Fail-safe: swap even if the surface never crosses the threshold (a
 *  legitimately tiny surface, or a broken worker) so nothing wedges on the
 *  fallback forever. */
const WARM_SWAP_DEADLINE_MS = 2500

/** Painted height of surfaces that completed a swap this page load, keyed
 *  on content identity. A virtualized row unmounts whenever it leaves the
 *  mounted window, and per-instance state dies with it -- so every remount
 *  re-ran the fallback-then-swap cycle, pulsing the row's height (plain
 *  fallback vs painted impl differ by hundreds of px on a big diff).
 *  Rendering the impl DIRECTLY on remount is NOT the answer: the impl is
 *  ~zero height until its highlight worker answers, so the row collapses
 *  to nothing and springs back -- a worse pulse, visible as the surface
 *  "reloading" in place. Instead a remount of a known surface keeps the
 *  fallback visible inside a box floored at the LAST PAINTED HEIGHT, so
 *  the re-highlight happens behind a layout that does not move, and the
 *  swap lands at (approximately) the height already reserved.
 *  Bounded: cleared wholesale past a cap rather than LRU-tracked -- keys
 *  are content hashes of surfaces the reader actually saw this session. */
const warmSwapHeights = new Map<string, number>()
const WARM_SWAP_DONE_CAP = 2000

/** Cheap stable id for swap memory: length-anchored FNV-1a over a prefix.
 *  Collisions only re-skip a fallback for an already-warm worker: benign. */
function warmKeyOf(text: string): string {
  let h = 0x811c9dc5
  const n = Math.min(text.length, 512)
  for (let i = 0; i < n; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return `${text.length}:${(h >>> 0).toString(36)}`
}

/**
 * Keeps a readable fallback ON SCREEN while the Pierre impl mounts and its
 * async highlight runs, swapping only once the impl has real painted height.
 *
 * Every impl behind this module renders ~zero height until the highlight
 * worker answers. That window used to hide off-screen; viewport-gated staging
 * moved Pierre mounts to where the reader is looking, so the window became a
 * visible collapse: a code block shrank to its border line, an expanded file
 * diff showed only its title row — and the row was measured at that collapsed
 * height, which is a scroll jump when the real height lands. The impl mounts
 * invisibly (absolute, zero footprint) so its chunk load, worker round-trip,
 * and paint all happen while the fallback holds the layout.
 */
function WarmSwap({ fallback, children, warmKey, onVisible }: {
  fallback: React.ReactNode
  children: React.ReactNode
  warmKey?: string
  onVisible?: () => void
}) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  const [painted, setPainted] = useState(false)
  // Measure-farm render: the fallback IS the measured geometry -- mounting the
  // impl invisibly would burn main thread for a surface that is never shown.
  const farm = useContext(PierreFarmHoldContext)
  // Height floor for a KNOWN surface's warm-up (see warmSwapHeights).
  const knownH = warmKey !== undefined ? warmSwapHeights.get(warmKey) : undefined
  useEffect(() => {
    if (farm) return
    if (painted) {
      // Record the surface's real painted height for future remounts. The
      // box is the impl's own wrapper, so scrollHeight is the impl height.
      if (warmKey !== undefined) {
        const el = boxRef.current
        const h = el ? el.scrollHeight : 0
        if (h > WARM_PAINT_MIN_PX) {
          if (warmSwapHeights.size >= WARM_SWAP_DONE_CAP && !warmSwapHeights.has(warmKey)) warmSwapHeights.clear()
          warmSwapHeights.set(warmKey, h)
        }
      }
      return
    }
    const el = boxRef.current
    if (!el || typeof ResizeObserver === 'undefined') {
      setPainted(true)
      return
    }
    if (el.scrollHeight > WARM_PAINT_MIN_PX) {
      setPainted(true)
      return
    }
    const ro = new ResizeObserver(() => {
      if (el.scrollHeight > WARM_PAINT_MIN_PX) {
        setPainted(true)
        ro.disconnect()
      }
    })
    ro.observe(el)
    const deadline = setTimeout(() => setPainted(true), WARM_SWAP_DEADLINE_MS)
    return () => {
      ro.disconnect()
      clearTimeout(deadline)
    }
  }, [painted, farm, warmKey])
  useEffect(() => {
    if (painted) onVisible?.()
  }, [onVisible, painted])
  if (farm) return <>{fallback}</>
  return (
    <div
      className={painted ? undefined : 'relative'}
      // While warming, FREEZE the box at the height this surface painted at
      // last time (exact, overflow clipped): neither the fallback's own
      // height nor the impl's pre-paint collapse can move the layout.
      // Released once painted, so a genuine height change (width flip,
      // side-by-side toggle) still settles to the impl's own height.
      style={!painted && knownH !== undefined ? { height: knownH, overflow: 'hidden' } : undefined}
    >
      <div
        ref={boxRef}
        className={painted ? undefined : 'absolute inset-0 overflow-hidden invisible'}
        aria-hidden={painted ? undefined : true}
      >
        {children}
      </div>
      {!painted && fallback}
    </div>
  )
}

export const PierreCode = memo(function PierreCode({ file, options, className, langHint, scrollClassName }: {
  file: FileContents
  options?: BaseCodeOptions
  className?: string
  /** Markdown fence tag, resolved to a safe highlight language in the impl. */
  langHint?: string
  /** Set for whole-file surfaces: Pierre takes over the scroll container and
   *  renders a window of rows instead of one per line. The caller's own box
   *  must then NOT scroll. */
  scrollClassName?: string
}) {
  const impl = (
    <Suspense fallback={
      /* The fallback carries the same scroll classes, so the pre-chunk text
         scrolls in the same box and the layout does not shift when the chunk
         resolves. */
      <div className={scrollClassName}><PlainCodeFallback text={file.contents} /></div>
    }>
      <CodeImpl file={file} options={options} className={className} langHint={langHint} scrollClassName={scrollClassName} />
    </Suspense>
  )
  // Whole-file surfaces (scrollClassName) own their scroll container and are
  // windowed by Pierre itself; wrapping them in an invisible box would break
  // that measurement, so only snippet surfaces warm-swap.
  if (scrollClassName) return impl
  return <WarmSwap warmKey={warmKeyOf(file.contents)} fallback={<PlainCodeFallback text={file.contents} />}>{impl}</WarmSwap>
})

export const PierrePatch = memo(function PierrePatch({ patch, options, className, renderHeaderMetadata }: {
  patch: string
  options?: PierreDiffOptions
  className?: string
  /** Injected into the FIRST file header's metadata slot (patch-level
   *  controls). Only rendered when the file header is enabled. */
  renderHeaderMetadata?: () => React.ReactNode
}) {
  // Plain-diff preference (Settings → Display): render the raw patch text and
  // never request the Pierre chunk at all. This is the seam for EVERY unified-
  // patch surface — chat fences, tool-call cards, tool input, PR file diffs —
  // so one preference covers all of them instead of each call site opting in.
  //
  // Not requesting the chunk is the point rather than a side effect: loading it
  // is what pulls in Pierre + Shiki, and the first surface inside it that wants
  // colour is what builds the highlight worker pool (`highlightWorkerPool` in
  // ./PierreImpl). So plain mode on a patch surface costs a `<pre>` and nothing
  // else — no chunk, no pool, no workers.
  //
  // `PierreFilePair` below honours the same preference but cannot take this
  // route: it is handed two file bodies and no patch, so there is nothing to
  // print raw and the diff still has to be computed inside the chunk. It drops
  // the colour and the workers instead — see `PierreFilePairImpl`.
  const [plain] = usePlainDiff()
  if (plain) return <PlainCodeFallback text={patch} className={className} />
  return (
    <WarmSwap warmKey={warmKeyOf(patch)} fallback={<PlainCodeFallback text={patch} />}>
      <Suspense fallback={<PlainCodeFallback text={patch} />}>
        <PatchImpl patch={patch} options={options} className={className} renderHeaderMetadata={renderHeaderMetadata} />
      </Suspense>
    </WarmSwap>
  )
})

export const PierreFilePair = memo(function PierreFilePair({ oldFile, newFile, options, className, fallbackText, fallbackClassName, fallbackContentStyle, onVisible, renderHeaderMetadata, renderHeaderPrefix, renderHeaderFilenameSuffix }: {
  oldFile: FileContents | null
  newFile: FileContents | null
  options?: PierreDiffOptions
  className?: string
  /** Optional caller-specific warm/Suspense fallback text and bounds. */
  fallbackText?: string
  fallbackClassName?: string
  /** Light-DOM sizing for the plain oversized fallback. Pierre's `unsafeCSS`
   *  styles its shadow root and cannot reach that fallback. */
  fallbackContentStyle?: CSSProperties
  /** Called after WarmSwap reveals the real implementation. */
  onVisible?: () => void
  /** Injected into the file header's metadata slot. Also rendered while
   *  `options.collapsed` is set, where the header IS the whole surface. */
  renderHeaderMetadata?: () => React.ReactNode
  /** Injected before the change icon and filename in the header. */
  renderHeaderPrefix?: () => React.ReactNode
  /** Injected directly after the filename in the header. */
  renderHeaderFilenameSuffix?: () => React.ReactNode
}) {
  const withinBudget = isPierreFilePairWithinBudget(oldFile, newFile)
  const [plain] = usePlainDiff()
  // Opt-in state machine for the oversized path: idle → computing → ready
  // (or error → back to idle with a notice). The diff itself runs in a Web
  // Worker (`diffOffThread.ts`), so the click never freezes the renderer —
  // the plain fallback stays interactive with a cancellable "computing" strip
  // until the patch arrives, then the hunk-based patch path renders it.
  const [request, setRequest] = useState<
    | { pair: PairIdentity; status: 'computing' }
    | { pair: PairIdentity; status: 'ready'; patch: string }
    | { pair: PairIdentity; status: 'error' }
    | null
  >(null)
  const abortRef = useRef<AbortController | null>(null)
  const pairMatches = request != null && request.pair.oldFile === oldFile && request.pair.newFile === newFile
  const active = pairMatches ? request : null
  useEffect(() => () => abortRef.current?.abort(), [])
  const startLineByLineDiff = () => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const pair: PairIdentity = { oldFile, newFile }
    setRequest({ pair, status: 'computing' })
    computePairPatch(oldFile, newFile, controller.signal).then(
      patch => setRequest(prev => (prev?.pair === pair ? { pair, status: 'ready', patch } : prev)),
      (err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setRequest(prev => (prev?.pair === pair ? { pair, status: 'error' } : prev))
      },
    )
  }
  const cancelLineByLineDiff = () => {
    abortRef.current?.abort()
    setRequest(null)
  }
  const plainFilePairFallback = (
    <PlainFilePairFallback
      oldFile={oldFile}
      newFile={newFile}
      options={options}
      className={className}
      contentStyle={fallbackContentStyle}
      renderHeaderMetadata={renderHeaderMetadata}
      renderHeaderPrefix={renderHeaderPrefix}
      renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
      onShowLineByLineDiff={startLineByLineDiff}
      lineByLineState={active?.status === 'computing' ? 'computing' : active?.status === 'error' ? 'error' : 'idle'}
      onCancelLineByLineDiff={cancelLineByLineDiff}
    />
  )
  if (!withinBudget && active?.status !== 'ready') return plainFilePairFallback

  if (!withinBudget && active?.status === 'ready') {
    // Plain-diff mode drops colour everywhere; the computed patch printed raw
    // keeps the ± markers, matching what the preference means on every other
    // patch surface (and preserving PatchImpl's colour-is-on invariant).
    if (plain) return <PlainCodeFallback text={active.patch} className={className} />
    // The impl renders ~zero height until the highlight worker answers, so the
    // plain view must hold the layout until real paint (same WarmSwap contract
    // as every other Pierre surface) — otherwise the card visibly collapses
    // and re-expands on swap. The held fallback keeps the "computing" strip:
    // the work is not done until the diff is on screen.
    const holdFallback = (
      <PlainFilePairFallback
        oldFile={oldFile}
        newFile={newFile}
        options={options}
        className={className}
        contentStyle={fallbackContentStyle}
        renderHeaderMetadata={renderHeaderMetadata}
        renderHeaderPrefix={renderHeaderPrefix}
        renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
        onShowLineByLineDiff={startLineByLineDiff}
        lineByLineState="computing"
        onCancelLineByLineDiff={cancelLineByLineDiff}
      />
    )
    // A collapsed pair is header-only (~32px) — under the paint threshold by
    // design — so it must not warm-swap or it would sit on the fallback until
    // the deadline (same rule as the within-budget branch below).
    if (options?.collapsed) {
      return (
        <Suspense fallback={holdFallback}>
          <PairPatchImpl
            patch={active.patch}
            options={options}
            className={className}
            renderHeaderMetadata={renderHeaderMetadata}
            renderHeaderPrefix={renderHeaderPrefix}
            renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
          />
        </Suspense>
      )
    }
    // The Suspense fallback INSIDE the hold must be null: the hold measures
    // its content box, and a visible fallback there would defeat the
    // measurement exactly the way it defeated WarmSwap's. With null the box
    // stays at zero height through the chunk load and the pre-highlight
    // mount, so the held plain view owns the layout until the diff truly
    // paints.
    return (
      <PatchPaintHold fallback={holdFallback} onVisible={onVisible}>
        <Suspense fallback={null}>
          <PairPatchImpl
            patch={active.patch}
            options={options}
            className={className}
            renderHeaderMetadata={renderHeaderMetadata}
            renderHeaderPrefix={renderHeaderPrefix}
            renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
          />
        </Suspense>
      </PatchPaintHold>
    )
  }

  const fallbackNode = (
    <PlainCodeFallback
      text={fallbackText ?? (newFile ?? oldFile)?.contents ?? ''}
      className={fallbackClassName}
    />
  )
  const impl = (
    <Suspense fallback={fallbackNode}>
      <FilePairImpl
        oldFile={oldFile}
        newFile={newFile}
        options={options}
        className={className}
        renderHeaderMetadata={renderHeaderMetadata}
        renderHeaderPrefix={renderHeaderPrefix}
        renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
      />
    </Suspense>
  )

  // A collapsed pair renders ONLY its header (~32px) — under the paint
  // threshold by design — so it must not warm-swap or it would sit on the
  // fallback until the deadline. Expanded pairs get the same treatment as
  // Patch: readable text holds the layout until the diff paints.
  if (options?.collapsed) return impl
  return (
    <WarmSwap
      warmKey={warmKeyOf((newFile ?? oldFile)?.contents ?? '')}
      fallback={fallbackNode}
      onVisible={onVisible}
    >
      {impl}
    </WarmSwap>
  )
})

export type { BaseCodeOptions, PierreDiffOptions, FileContents }
