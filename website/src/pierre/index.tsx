/**
 * Public entry for Pierre-rendered surfaces. Import ONLY from here (or
 * `./config` for option types) — never from `@pierre/diffs` directly — so the
 * heavy Shiki/Pierre runtime stays behind one lazy chunk and every surface
 * shares the config in `./config`.
 *
 * Each component suspends into a plain monospace `<pre>` of the raw text while
 * the chunk loads, so content is readable immediately (and test environments
 * that never resolve the chunk still render the text).
 */
import { Suspense, forwardRef, lazy, memo, useContext, useEffect, useRef, useState } from 'react'
import type { BaseCodeOptions, FileContents } from '@pierre/diffs'
import type { PierreDiffOptions } from './config'
import type { EditorMarker, PierreEditorHandle } from './PierreEditorImpl'
import { PlainCodeFallback } from './PlainCodeFallback'
import { PierreFarmHoldContext } from '../components/pierreStaging'

const CodeImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierreCodeImpl })))
const PatchImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierrePatchImpl })))
const FilePairImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierreFilePairImpl })))
const EditorImpl = lazy(() => import('./PierreEditorImpl').then(m => ({ default: m.PierreEditorImpl })))

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
function WarmSwap({ fallback, children, warmKey }: { fallback: React.ReactNode; children: React.ReactNode; warmKey?: string }) {
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
  return (
    <WarmSwap warmKey={warmKeyOf(patch)} fallback={<PlainCodeFallback text={patch} />}>
      <Suspense fallback={<PlainCodeFallback text={patch} />}>
        <PatchImpl patch={patch} options={options} className={className} renderHeaderMetadata={renderHeaderMetadata} />
      </Suspense>
    </WarmSwap>
  )
})

export const PierreFilePair = memo(function PierreFilePair({ oldFile, newFile, options, className, renderHeaderMetadata, renderHeaderPrefix, renderHeaderFilenameSuffix }: {
  oldFile: FileContents | null
  newFile: FileContents | null
  options?: PierreDiffOptions
  className?: string
  /** Injected into the file header's metadata slot. Also rendered while
   *  `options.collapsed` is set, where the header IS the whole surface. */
  renderHeaderMetadata?: () => React.ReactNode
  /** Injected before the change icon and filename in the header. */
  renderHeaderPrefix?: () => React.ReactNode
  /** Injected directly after the filename in the header. */
  renderHeaderFilenameSuffix?: () => React.ReactNode
}) {
  const impl = (
    <Suspense fallback={<PlainCodeFallback text={(newFile ?? oldFile)?.contents ?? ''} />}>
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
    <WarmSwap warmKey={warmKeyOf((newFile ?? oldFile)?.contents ?? '')} fallback={<PlainCodeFallback text={(newFile ?? oldFile)?.contents ?? ''} />}>
      {impl}
    </WarmSwap>
  )
})

export type { BaseCodeOptions, PierreDiffOptions, FileContents }
