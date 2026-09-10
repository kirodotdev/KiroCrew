import { useId, useLayoutEffect, useMemo, useRef, type CSSProperties, type ReactNode } from 'react'
import type { FileContents } from '@pierre/diffs'
import type { PierreDiffOptions } from './config'
import { changedLineSpan } from '../utils/diffLineCounts'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/** Plain-text stand-in used while the Pierre chunk loads and for patch text
 *  that does not (yet) parse — e.g. the partial frames of a streaming diff.
 *
 *  Pierre's own geometry, MEASURED from a rendered block (7 blocks, heights
 *  exactly 50 + 20×lines): 2px border, 32px header, 16px body padding, 20px per
 *  line. The stand-in has to match all of it, or the swap moves the reader.
 *
 *  `leading-5` and `py-2` say 20px and 8px — and inside a transcript they were
 *  BOTH LOSING: `.msg-content pre` (specificity 0,1,1) beats a single-class
 *  utility and sets `line-height:1.5` (19.5px at 13px) with `padding:10px 12px`.
 *  The stand-in therefore rendered 4px taller per surface than the thing it
 *  stands in for, and the reader was displaced by 12-36px per transcript row the
 *  moment the chunk resolved (measured in a browser: -4px on every
 *  `.pierre-surface`, three code blocks in one row = -36px). `pierre-plain` is
 *  the hook that wins that fight; the metrics live in index.css next to the rule
 *  they have to beat. Keep the two geometries equal or the reflow returns.
 *
 *  Also the FINAL render for patch surfaces when the plain-diff preference is
 *  on (see `usePlainDiff`), which is why it accepts the caller's `className`:
 *  in that mode it stands in for the Pierre element the class was written for.
 *  The geometry above still has to hold there — the preference can flip while a
 *  transcript is on screen, so this element replaces a Pierre surface in place.
 */
export function PlainCodeFallback({ text, className }: { text: string; className?: string }) {
  return (
    <pre className={`pierre-plain m-0 px-3 py-2 overflow-x-auto text-[13px] font-mono leading-5 whitespace-pre${className ? ` ${className}` : ''}`}>
      {text}
    </pre>
  )
}

const KEYBOARD_SCROLL_REGION_PROPS = { role: 'region' as const, tabIndex: 0 }

interface PlainFilePairFallbackProps {
  oldFile: FileContents | null
  newFile: FileContents | null
  options?: PierreDiffOptions
  className?: string
  contentStyle?: CSSProperties
  renderHeaderMetadata?: () => ReactNode
  renderHeaderPrefix?: () => ReactNode
  renderHeaderFilenameSuffix?: () => ReactNode
}

/**
 * Complete, low-cost representation for a file pair that is unsafe to diff on
 * the renderer thread. It intentionally does not synthesize a unified patch:
 * doing so would repeat the synchronous operation this fallback avoids.
 */
export function PlainFilePairFallback({
  oldFile,
  newFile,
  options,
  className,
  contentStyle,
  renderHeaderMetadata,
  renderHeaderPrefix,
  renderHeaderFilenameSuffix,
}: PlainFilePairFallbackProps) {
  useLanguageGeneration()
  const titleId = useId()
  const simplifiedLabel = i18nT('components.fileChangeChips.large_file_simplified_view')
  const changeRegionLabel = i18nT('components.fileChangeChips.large_file_change_region_view')

  // The fallback is handed two whole files and no hunks, so it finds WHERE the
  // change is with a bounded prefix/suffix scan (see `changedLineSpan`) rather
  // than a diff — the synchronous diff is exactly what the render budget
  // rejected. Split each side into lines ONCE and remember the outer change
  // region. The region stays neutral because its interior can include unchanged
  // rows; only the boundary rows the scan proves changed use diff colors.
  const oldLines = useMemo(() => (oldFile?.contents ? oldFile.contents.split('\n') : []), [oldFile?.contents])
  const newLines = useMemo(() => (newFile?.contents ? newFile.contents.split('\n') : []), [newFile?.contents])
  const span = useMemo(() => changedLineSpan(oldLines, newLines), [oldLines, newLines])
  const anchorKey = span == null
    ? null
    : span.oldEnd > span.oldStart ? 'old' : span.newEnd > span.newStart ? 'new' : null
  // The bounded content scroller and the first changed row, so the mount effect
  // can anchor the card without moving the surrounding transcript.
  const contentRef = useRef<HTMLDivElement | null>(null)
  const firstChangedRef = useRef<HTMLSpanElement | null>(null)
  useLayoutEffect(() => {
    const content = contentRef.current
    const changed = firstChangedRef.current
    if (!content || !changed) return
    // Scroll THIS card's own bounded region directly. `scrollIntoView()` can
    // choose the transcript/page as another scroll ancestor and strand the card
    // at line 1 after the surrounding virtualizer settles. `offsetTop` is the
    // changed row's position inside the card; center it when there is room.
    content.scrollTop = Math.max(0, changed.offsetTop - content.clientHeight / 2)
  }, [span, oldFile?.contents, newFile?.contents, options?.diffStyle])

  const filename = newFile?.name ?? oldFile?.name
  if (filename == null) return null

  // Pierre's shared diff default hides the file header. Omission must
  // resolve the same way here or oversized side-panel diffs gain extra chrome.
  const showHeader = options?.disableFileHeader === false
  const collapsed = options?.collapsed === true
  const filePairLabel = span != null && !collapsed ? changeRegionLabel : simplifiedLabel
  const wraps = options?.overflow === 'wrap'
  const sides = [
    oldFile == null ? null : { file: oldFile, lines: oldLines, marker: '−', key: 'old' as const, start: span?.oldStart, end: span?.oldEnd },
    newFile == null ? null : { file: newFile, lines: newLines, marker: '+', key: 'new' as const, start: span?.newStart, end: span?.newEnd },
  ].filter((side): side is { file: FileContents; lines: string[]; marker: string; key: 'old' | 'new'; start: number | undefined; end: number | undefined } => side != null)
  const split = options?.diffStyle === 'split' && sides.length === 2

  return (
    <div
      data-pierre-plain-file-pair
      className={`pierre-surface min-w-0 overflow-hidden bg-bg text-text ${className ?? ''}`}
    >
      {showHeader && (
        <div
          data-diffs-header
          className="flex min-h-9 items-center gap-2 border-b border-border bg-bg px-3 text-[12px]"
        >
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            {renderHeaderPrefix?.()}
            <span id={titleId} data-title className="truncate font-mono font-medium">
              {filename}
            </span>
            {renderHeaderFilenameSuffix?.()}
            <span className="text-[11px] font-normal text-muted">{filePairLabel}</span>
          </div>
          {renderHeaderMetadata && <div className="shrink-0">{renderHeaderMetadata()}</div>}
        </div>
      )}
      {!collapsed && (
        <div
          ref={contentRef}
          data-pierre-plain-content
          style={contentStyle}
          className={`${split ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1'} relative grid min-w-0 overflow-auto`}
        >
          {!showHeader && (
            <div className={`${split ? 'md:col-span-2' : ''} border-b border-border bg-bg px-3 py-1 text-[11px] text-muted`}>
              {filePairLabel}
            </div>
          )}
          {sides.map(({ file, lines, marker, key, start, end }, index) => {
            const headingId = `${titleId}-${key}`
            const regionLines = start != null && end != null && end > start
              ? lines.slice(start, end)
              : []
            const lastRegionLine = regionLines.length - 1
            return (
              <section
                key={key}
                aria-labelledby={headingId}
                className={`min-w-0 ${
                  index > 0
                    ? split ? 'border-t border-border md:border-l md:border-t-0' : 'border-t border-border'
                    : ''
                }`}
              >
                <h3
                  id={headingId}
                  className="m-0 border-b border-border bg-bg-hover px-3 py-1 text-[12px] font-mono font-medium text-muted"
                >
                  {marker} {file.name}
                </h3>
                {/* A native pre is the actual horizontal scroller. The named
                    props give it a tab stop so keyboard users can reach clipped
                    long lines without weakening lint for other elements.

                    Keep the DOM constant-size even for a 40k-line file: plain
                    prefix text, ONE neutral region span, plain suffix text. The
                    region has at most two diff-colored child spans for the first
                    and last rows the bounded scan proves changed. Its interior
                    remains neutral because the scan cannot classify those rows.
                    Box-decoration cloning paints each wrapped fragment without
                    adding one DOM node per source line. */}
                <pre
                  data-pierre-plain-side={key}
                  {...KEYBOARD_SCROLL_REGION_PROPS}
                  aria-labelledby={headingId}
                  className={`m-0 overflow-x-auto px-3 py-2 text-[13px] font-mono leading-relaxed ${
                    wraps ? 'whitespace-pre-wrap break-words' : 'whitespace-pre'
                  }`}
                >
                  {regionLines.length > 0 ? (
                    <>
                      {start! > 0 ? `${lines.slice(0, start).join('\n')}\n` : ''}
                      <span
                        data-change-region=""
                        className="bg-bg-hover [box-decoration-break:clone] [-webkit-box-decoration-break:clone]"
                      >
                        <span
                          ref={key === anchorKey ? firstChangedRef : undefined}
                          data-changed-line=""
                          className={key === 'new'
                            ? 'bg-[var(--diff-add)] text-[var(--diff-add-text)] [box-shadow:inset_2px_0_0_0_var(--diff-add-text)] pl-[0.5ch] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]'
                            : 'bg-[var(--diff-del)] text-[var(--diff-del-text)] [box-shadow:inset_2px_0_0_0_var(--diff-del-text)] pl-[0.5ch] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]'
                          }
                        >
                          <span aria-hidden className="select-none">{marker} </span>
                          {regionLines[0]}
                        </span>
                        {lastRegionLine > 0 && (
                          <>
                            {lastRegionLine > 1
                              ? `\n${regionLines.slice(1, lastRegionLine).join('\n')}\n`
                              : '\n'}
                            <span
                              data-changed-line=""
                              className={key === 'new'
                            ? 'bg-[var(--diff-add)] text-[var(--diff-add-text)] [box-shadow:inset_2px_0_0_0_var(--diff-add-text)] pl-[0.5ch] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]'
                            : 'bg-[var(--diff-del)] text-[var(--diff-del-text)] [box-shadow:inset_2px_0_0_0_var(--diff-del-text)] pl-[0.5ch] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]'
                          }
                            >
                              <span aria-hidden className="select-none">{marker} </span>
                              {regionLines[lastRegionLine]}
                            </span>
                          </>
                        )}
                      </span>
                      {end! < lines.length ? `\n${lines.slice(end).join('\n')}` : ''}
                    </>
                  ) : lines.join('\n')}
                </pre>
              </section>
            )
          })}
        </div>
      )}
    </div>
  )
}
