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
import { Suspense, forwardRef, lazy, memo } from 'react'
import type { BaseCodeOptions, FileContents, LineAnnotation } from '@pierre/diffs'
import type { FileOptions } from '@pierre/diffs/react'
import type { PierreDiffOptions } from './config'
import type { EditorMarker, PierreEditorHandle } from './PierreEditorImpl'
import { PlainCodeFallback } from './PlainCodeFallback'

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

export const PierreCode = memo(function PierreCode({ file, options, className, langHint, scrollClassName, lineAnnotations, renderAnnotation, renderGutterUtility }: {
  file: FileContents
  /** Accepts Pierre's full `FileOptions` surface (interaction callbacks like
   *  `enableLineSelection` / `onLineSelected` / `enableGutterUtility`
   *  included) — the impl merges it over the shared code-view defaults. */
  options?: FileOptions<unknown>
  className?: string
  /** Markdown fence tag, resolved to a safe highlight language in the impl. */
  langHint?: string
  /** Set for whole-file surfaces: Pierre takes over the scroll container and
   *  renders a window of rows instead of one per line. The caller's own box
   *  must then NOT scroll. */
  scrollClassName?: string
  /** Line-pinned annotation rows, rendered by Pierre inside its own surface
   *  (virtualization-correct). Payload type is the caller's; `renderAnnotation`
   *  narrows it back. Dashboard stylesheets do NOT reach Pierre's shadow DOM,
   *  so render-prop content must style itself inline (theme custom properties
   *  DO inherit through the boundary). */
  lineAnnotations?: LineAnnotation<unknown>[]
  renderAnnotation?: (annotation: LineAnnotation<unknown>) => React.ReactNode
  /** Hover gutter slot (e.g. a comment button). Same inline-style rule as
   *  `renderAnnotation`. */
  renderGutterUtility?: (getHoveredLine: () => { lineNumber: number } | undefined) => React.ReactNode
}) {
  return (
    <Suspense fallback={
      /* The fallback carries the same scroll classes, so the pre-chunk text
         scrolls in the same box and the layout does not shift when the chunk
         resolves. */
      <div className={scrollClassName}><PlainCodeFallback text={file.contents} /></div>
    }>
      <CodeImpl file={file} options={options} className={className} langHint={langHint} scrollClassName={scrollClassName} lineAnnotations={lineAnnotations} renderAnnotation={renderAnnotation} renderGutterUtility={renderGutterUtility} />
    </Suspense>
  )
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
    <Suspense fallback={<PlainCodeFallback text={patch} />}>
      <PatchImpl patch={patch} options={options} className={className} renderHeaderMetadata={renderHeaderMetadata} />
    </Suspense>
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
  return (
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
})

export type { BaseCodeOptions, PierreDiffOptions, FileContents }
