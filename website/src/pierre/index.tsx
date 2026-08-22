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
import type { BaseCodeOptions, FileContents } from '@pierre/diffs'
import type { PierreCodeViewOptions, PierreDiffOptions } from './config'
import type { EditorMarker, PierreEditorHandle } from './PierreEditorImpl'
import type { PierrePatchFile, PierreCodeViewScrollHandle } from './PierreCodeViewImpl'
import { PlainCodeFallback } from './PlainCodeFallback'

const CodeImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierreCodeImpl })))
const PatchImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierrePatchImpl })))
const FilePairImpl = lazy(() => import('./PierreImpl').then(m => ({ default: m.PierreFilePairImpl })))
const EditorImpl = lazy(() => import('./PierreEditorImpl').then(m => ({ default: m.PierreEditorImpl })))
const CodeViewImpl = lazy(() => import('./PierreCodeViewImpl').then(m => ({ default: m.PierreCodeViewImpl })))

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
  return (
    <Suspense fallback={
      /* The fallback carries the same scroll classes, so the pre-chunk text
         scrolls in the same box and the layout does not shift when the chunk
         resolves. */
      <div className={scrollClassName}><PlainCodeFallback text={file.contents} /></div>
    }>
      <CodeImpl file={file} options={options} className={className} langHint={langHint} scrollClassName={scrollClassName} />
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

/**
 * Every file of a change set in ONE Pierre CodeView.
 *
 * The library owns the scroll container, so `className` styles the element
 * CodeView itself scrolls and the caller's box must not scroll too. Unlike the
 * other surfaces here the FILE HEADER is Pierre's: it draws the change icon,
 * path and +/- counts, pins itself while its file scrolls, and exposes the three
 * slots below for a caller's own affordances (a collapse toggle belongs in
 * `renderHeaderPrefix`).
 *
 * The pre-chunk fallback lists the paths rather than the patch text: a whole
 * change set as one plain blob is unreadable, whereas the file list is exactly
 * what the resolved view opens on, so the panel does not flash empty.
 */
export const PierreCodeView = memo(forwardRef<PierreCodeViewScrollHandle, {
  files: readonly PierrePatchFile[]
  options?: PierreCodeViewOptions
  className?: string
  /** Scroll offset of CodeView's own scroller — the only way to react to the
   *  reader moving through the change set, since the library owns that element. */
  onScroll?: (scrollTop: number) => void
  /** The file at the top of the viewport, reported when it changes. */
  onViewportFileChange?: (path: string) => void
  renderHeaderPrefix?: (path: string) => React.ReactNode
  renderHeaderFilenameSuffix?: (path: string) => React.ReactNode
  renderHeaderMetadata?: (path: string) => React.ReactNode
}>(function PierreCodeView({ files, options, className, onScroll, onViewportFileChange, renderHeaderPrefix, renderHeaderFilenameSuffix, renderHeaderMetadata }, ref) {
  return (
    <Suspense fallback={
      <div className={className}><PlainCodeFallback text={files.map(f => f.path).join('\n')} /></div>
    }>
      <CodeViewImpl
        ref={ref}
        files={files}
        options={options}
        className={className}
        onScroll={onScroll}
        onViewportFileChange={onViewportFileChange}
        renderHeaderPrefix={renderHeaderPrefix}
        renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
        renderHeaderMetadata={renderHeaderMetadata}
      />
    </Suspense>
  )
}))

export type { PierreCodeViewOptions, PierrePatchFile, PierreCodeViewScrollHandle }
