/**
 * Pierre's CodeView: ONE virtualized viewer holding every file of a change set,
 * each with its own diff surface and a header that pins while its file scrolls.
 *
 * Reached only through the `React.lazy` boundary in `./index`, like the other
 * Pierre surfaces, so the library stays out of the eager bundle.
 *
 * Deliberately thin. CodeView owns the scroll container, cross-file
 * virtualization, layout measurement, sticky positioning and the file header, so
 * this module does only the two things the library cannot do for us:
 *
 *  1. turn our per-file patch payloads into `CodeViewItem`s, and
 *  2. keep each item's `version` moving, because a CONTROLLED CodeView re-reads a
 *     reused record only when its version changes.
 *
 * Everything else is stock: options flow through `./config`, the caller's own
 * affordances go in Pierre's header slots, and the scroll element is the `div`
 * CodeView mounts itself into.
 */
import { forwardRef, useImperativeHandle, useMemo, useRef } from 'react'
import type { CodeViewItem, FileDiffMetadata } from '@pierre/diffs'
import { CodeView, type CodeViewHandle } from '@pierre/diffs/react'
import { patchChangeForStatus, withUnifiedPatchHeaders } from '../components/unifiedPatchHeaders'
import { useIsDark } from '../hooks/useIsDark'
import { PierreShell, parsePatchFileDiffs, patchLostItsHunks } from './PierreImpl'
import { pierreCodeViewOptions, pierreThemeType, type PierreCodeViewOptions } from './config'

/** One changed file, as a caller that talks to a git provider already has it. */
export interface PierrePatchFile {
  /** Repo-relative path. Doubles as the CodeView item id, so it has to be unique
   *  within one view — which a change set's file list already guarantees. */
  path: string
  /** The file's unified patch. A bare hunk body is accepted: providers return
   *  hunks only, so the `diff --git` / `---` / `+++` headers Pierre identifies a
   *  file from are synthesized when the payload does not carry them. Missing or
   *  unparseable text still renders — as a header-only row. */
  patch?: string
  /** Provider status token (`added`, `removed`, `modified`, …). Spelled into the
   *  synthesized headers, because that is where Pierre reads the change type
   *  its header icon shows. */
  status?: string
  /** Header-only. CodeView then reserves just the header band for the item and
   *  never tokenizes its body, which is what makes a large change set cheap. */
  collapsed?: boolean
}

/** Item state that must move the `version` when it changes: the raw payload
 *  fields and the collapse flag, compared individually. EXACT inputs, never a
 *  content hash -- `fileDiff.cacheKey` is a 32-bit djb2 digest, and two
 *  different patches colliding onto one digest would freeze the version and
 *  leave CodeView showing the stale diff. Field equality cannot collide, and
 *  the patch text is already held in props, so this costs nothing. */
interface ItemState {
  collapsed: boolean
  status: string
  patch: string
  version: number
}

function sameItemState(previous: ItemState | undefined, collapsed: boolean, file: PierrePatchFile): boolean {
  return previous !== undefined
    && previous.collapsed === collapsed
    && previous.status === (file.status ?? '')
    && previous.patch === (file.patch ?? '')
}

/** The whole-file patch text Pierre parses, headers included. */
function headedPatch(file: PierrePatchFile): string {
  const patch = file.patch ?? ''
  // A payload that already carries its own `diff --git` section is passed
  // through: re-wrapping it would nest two file headers in one string.
  if (patch.startsWith('diff --git')) return patch
  return withUnifiedPatchHeaders(file.path, patch, patchChangeForStatus(file.status ?? ''))
}

/** The diff metadata for one file, or undefined when nothing renderable came
 *  back.
 *
 *  A file whose patch the provider withheld (binary, or over its size ceiling)
 *  deliberately still produces an item: parsing headers with NO hunk body yields
 *  the header-with-no-rows shape, which keeps the file present and countable in
 *  the list instead of silently missing. Its header can only report `+0 −0`,
 *  which is why a caller should put the true counts in the metadata slot. */
function fileDiffFor(file: PierrePatchFile): FileDiffMetadata | undefined {
  const patch = headedPatch(file)
  const parsed = parsePatchFileDiffs(patch)
  if (!patchLostItsHunks(parsed, patch)) return parsed[0]
  return parsePatchFileDiffs(headedPatch({ ...file, patch: '' }))[0]
}

/** The caller-facing imperative surface. Path-keyed like the render slots, so
 *  callers never hold a library handle or item type. */
export interface PierreCodeViewScrollHandle {
  /** Scroll the change set so `path`'s file starts at the top of the viewport.
   *  Animated; CodeView cancels the animation on any user input, so a reader
   *  who grabs the scrollbar mid-travel wins immediately. */
  scrollToFile(path: string): void
}

/** The file whose content occupies the top of the viewport: the LAST item whose
 *  logical top is at or above the scroll offset. Item tops come from the
 *  virtualizer's estimates for unrendered items, which is exactly what makes
 *  this answerable without rendering the whole change set. Exported for tests.
 */
export function fileAtScrollTop(
  scrollTop: number,
  orderedPaths: readonly string[],
  topFor: (path: string) => number | undefined,
): string | undefined {
  let current: string | undefined
  for (const path of orderedPaths) {
    const top = topFor(path)
    // An unmeasured item cannot be placed; skipping it means the answer can
    // only come from items the virtualizer has an offset for.
    if (top === undefined) continue
    if (top > scrollTop) break
    current = path
  }
  return current
}

export const PierreCodeViewImpl = forwardRef<PierreCodeViewScrollHandle, {
  files: readonly PierrePatchFile[]
  options?: PierreCodeViewOptions
  /** Classes for the element CodeView scrolls. It must be height-bounded and
   *  scrollable — CodeView listens for scroll on it and virtualizes against its
   *  box — so the caller's own container must NOT also scroll. */
  className?: string
  /** Scroll position of CodeView's own scroller. CodeView owns that element, so
   *  this is the only way a caller can react to the reader moving through the
   *  change set (condensing page chrome, and so on). */
  onScroll?: (scrollTop: number) => void
  /** The file whose content is at the top of the viewport, reported when it
   *  CHANGES during scrolling — a tree or outline follows the reader with it. */
  onViewportFileChange?: (path: string) => void
  /** Pierre's header slots, keyed by path so callers never touch item types. */
  renderHeaderPrefix?: (path: string) => React.ReactNode
  renderHeaderFilenameSuffix?: (path: string) => React.ReactNode
  renderHeaderMetadata?: (path: string) => React.ReactNode
}>(function PierreCodeViewImpl({
  files,
  options,
  className,
  onScroll,
  onViewportFileChange,
  renderHeaderPrefix,
  renderHeaderFilenameSuffix,
  renderHeaderMetadata,
}, ref) {
  const dark = useIsDark()
  const resolved = useMemo(
    () => pierreCodeViewOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  /* Per-path version counters. A counter rather than a hash of the payload:
     ANY change bumps it, and two different payloads can never collide onto the
     same number — which a hash can, and the failure mode there is a file that
     silently keeps rendering its previous diff. */
  const versions = useRef(new Map<string, ItemState>())
  const items = useMemo<CodeViewItem[]>(() => {
    const next = new Map<string, ItemState>()
    const built: CodeViewItem[] = []
    for (const file of files) {
      const fileDiff = fileDiffFor(file)
      if (!fileDiff) continue
      const collapsed = file.collapsed === true
      const previous = versions.current.get(file.path)
      const version = sameItemState(previous, collapsed, file)
        ? (previous as ItemState).version
        : (previous?.version ?? 0) + 1
      next.set(file.path, { collapsed, status: file.status ?? '', patch: file.patch ?? '', version })
      built.push({ id: file.path, type: 'diff', fileDiff, version, collapsed })
    }
    // Replaced rather than merged, so a path that left the change set drops its
    // counter instead of accumulating for the lifetime of the tab.
    versions.current = next
    return built
  }, [files])

  /* Pierre hands its slot renderers the ITEM; the public surface takes a path.
     Wrapped in memos so an unchanged caller callback does not churn CodeView's
     managed options (it compares them by identity and re-renders on a change). */
  const headerPrefix = useMemo(
    () => (renderHeaderPrefix ? (item: CodeViewItem) => renderHeaderPrefix(item.id) : undefined),
    [renderHeaderPrefix],
  )
  const headerFilenameSuffix = useMemo(
    () => (renderHeaderFilenameSuffix ? (item: CodeViewItem) => renderHeaderFilenameSuffix(item.id) : undefined),
    [renderHeaderFilenameSuffix],
  )
  const headerMetadata = useMemo(
    () => (renderHeaderMetadata ? (item: CodeViewItem) => renderHeaderMetadata(item.id) : undefined),
    [renderHeaderMetadata],
  )

  /* Pierre hands its scroll listener the viewer as a second argument; the public
     surface takes the offset alone, so callers never hold a library instance.
     The viewer IS consulted here, though: its per-item offsets answer which file
     the reader is on, reported only on change so a scroll does not spray
     identical updates. */
  const codeViewRef = useRef<CodeViewHandle<undefined>>(null)
  const orderedPaths = useMemo(() => items.map(item => item.id), [items])
  const viewportFile = useRef<string | undefined>(undefined)
  const callbacks = useRef({ onScroll, onViewportFileChange })
  callbacks.current = { onScroll, onViewportFileChange }
  /* One-shot landing correction. A smooth scroll DOWN crosses items the
     virtualizer has only estimated; the animation settles the moment the
     offset matches the then-current resolved target, and a measurement landing
     on the NEXT frame can shift the target by a pixel or two with no pending
     target left to chase it. (Upward travel crosses already-measured items, so
     it lands exact — which is why the gap is direction-dependent.) So after
     the scroll stream goes quiet, one INSTANT re-scroll to the same item
     closes the residue. Bounded to a small delta: a large one means the
     reader grabbed the scrollbar mid-travel — their scroll won, leave it.

     While a correction is pending, viewport-file reporting is PINNED to the
     clicked file (reported once, at click time): the travel crosses every file
     in between, and echoing each of them would sweep the tree's selection
     through the whole change set. The reader taking over mid-travel is the
     exception — a position outside the start→target corridor means their
     scroll won, so the pin is dropped and following resumes. */
  const correction = useRef<{ path: string; startTop: number | null; timer: ReturnType<typeof setTimeout> | null } | null>(null)
  /** Corridor slack around the programmatic travel: rubber-banding and the
   *  settle overshoot stay inside it; a deliberate grab does not. */
  const HIJACK_SLACK = 48
  const settleCorrection = (path: string, scrollTop: number, viewer: { getTopForItem(id: string): number | undefined }) => {
    const spacing = resolved.itemMetrics?.spacing ?? 8
    const top = viewer.getTopForItem(path)
    if (top === undefined) return
    const delta = Math.abs((top - spacing) - scrollTop)
    if (delta > 0.5 && delta <= 40) {
      codeViewRef.current?.scrollTo({ type: 'item', id: path, align: 'start', behavior: 'instant', offset: -spacing })
    }
  }
  const handleScroll = useMemo(
    () => (scrollTop: number, viewer: { getTopForItem(id: string): number | undefined }) => {
      callbacks.current.onScroll?.(scrollTop)
      const pending = correction.current
      if (pending) {
        if (pending.startTop === null) pending.startTop = scrollTop
        const top = viewer.getTopForItem(pending.path)
        const target = top === undefined ? undefined : top - (resolved.itemMetrics?.spacing ?? 8)
        const hijacked = target !== undefined
          && (scrollTop < Math.min(pending.startTop, target) - HIJACK_SLACK
            || scrollTop > Math.max(pending.startTop, target) + HIJACK_SLACK)
        if (pending.timer) clearTimeout(pending.timer)
        if (hijacked) {
          correction.current = null
        } else {
          pending.timer = setTimeout(() => {
            correction.current = null
            settleCorrection(pending.path, scrollTop, viewer)
          }, 160)
        }
      }
      if (!callbacks.current.onViewportFileChange) return
      if (correction.current) return
      const path = fileAtScrollTop(scrollTop, orderedPaths, id => viewer.getTopForItem(id))
      if (!path || path === viewportFile.current) return
      viewportFile.current = path
      callbacks.current.onViewportFileChange(path)
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [orderedPaths, resolved],
  )

  useImperativeHandle(ref, () => ({
    scrollToFile(path: string) {
      // Smooth, not instant: a tree click is a navigation, and the travel is
      // what tells the reader how far they moved. CodeView cancels the
      // animation on any user input, which is the right interruption model.
      //
      // The negative offset compensates a library asymmetry: an item's sticky
      // header actually sits `itemMetrics.spacing` below `item.top` (the
      // leading hunk-separator gap is part of the item box), and
      // `resolveAlignedScrollPosition` compensates sticky offsets for `line`
      // and `range` targets but hardwires 0 for `item` targets — measured as a
      // constant 8px band of the previous file left above the header.
      if (correction.current?.timer) clearTimeout(correction.current.timer)
      /* Pin the viewport report to the destination for the whole travel (see
         the correction comment above), and say it once, NOW: the tree's
         selection should land on the clicked file immediately, not after the
         scroll arrives. The timer covers a click whose target is already at
         position — no scroll event ever fires, so nothing else would clear
         the pin. */
      viewportFile.current = path
      callbacks.current.onViewportFileChange?.(path)
      correction.current = {
        path,
        startTop: null,
        timer: setTimeout(() => { correction.current = null }, 400),
      }
      codeViewRef.current?.scrollTo({
        type: 'item',
        id: path,
        align: 'start',
        behavior: 'smooth',
        offset: -(resolved.itemMetrics?.spacing ?? 8),
      })
    },
  }), [resolved])

  return (
    <PierreShell>
      <CodeView
        ref={codeViewRef}
        className={className}
        items={items}
        options={resolved}
        onScroll={handleScroll}
        renderHeaderPrefix={headerPrefix}
        renderHeaderFilenameSuffix={headerFilenameSuffix}
        renderHeaderMetadata={headerMetadata}
      />
    </PierreShell>
  )
})
