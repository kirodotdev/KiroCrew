import { useId, type CSSProperties, type ReactNode } from 'react'
import type { FileContents } from '@pierre/diffs'
import type { PierreDiffOptions } from './config'
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
  const filename = newFile?.name ?? oldFile?.name
  if (filename == null) return null

  // Pierre's shared diff default hides the file header. Omission must
  // resolve the same way here or oversized side-panel diffs gain extra chrome.
  const showHeader = options?.disableFileHeader === false
  const collapsed = options?.collapsed === true
  const wraps = options?.overflow === 'wrap'
  const sides = [
    oldFile == null ? null : { file: oldFile, marker: '−', key: 'old' },
    newFile == null ? null : { file: newFile, marker: '+', key: 'new' },
  ].filter((side): side is { file: FileContents; marker: string; key: string } => side != null)
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
            <span className="text-[11px] font-normal text-muted">{simplifiedLabel}</span>
          </div>
          {renderHeaderMetadata && <div className="shrink-0">{renderHeaderMetadata()}</div>}
        </div>
      )}
      {!collapsed && (
        <div
          data-pierre-plain-content
          style={contentStyle}
          className={`${split ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1'} grid min-w-0 overflow-auto`}
        >
          {!showHeader && (
            <div className={`${split ? 'md:col-span-2' : ''} border-b border-border bg-bg px-3 py-1 text-[11px] text-muted`}>
              {simplifiedLabel}
            </div>
          )}
          {sides.map(({ file, marker, key }, index) => {
            const headingId = `${titleId}-${key}`
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
                    long lines without weakening lint for other elements. */}
                <pre
                  data-pierre-plain-side={key}
                  {...KEYBOARD_SCROLL_REGION_PROPS}
                  aria-labelledby={headingId}
                  className={`m-0 overflow-x-auto px-3 py-2 text-[13px] font-mono leading-relaxed ${
                    wraps ? 'whitespace-pre-wrap break-words' : 'whitespace-pre'
                  }`}
                >
                  {file.contents}
                </pre>
              </section>
            )
          })}
        </div>
      )}
    </div>
  )
}
