import { createContext, Fragment, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, type CSSProperties, type ReactNode } from 'react'
import type { FileContents } from '@pierre/diffs'
import {
  DIFF_HEADER_BG_CSS,
  DIFF_HEADER_COUNT_MIN_WIDTH_CH,
  DIFF_HEADER_META_W_PX,
  DIFF_HEADER_PADDING_INLINE_PX,
  type PierreDiffOptions,
} from './config'
import { changedLineSpan, plainPatchHunks } from '../utils/diffLineCounts'
import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/**
 * What a surface that draws its OWN header row over a patch body tells the
 * plain bodies rendered beneath that row.
 *
 * The chat diff block owns its row (`PlainFilePairHeader`, one per file) and
 * hands Pierre the body only. Under a row that already says which file, which
 * rename, which kind of change and how many lines, a plain body's raw `diff
 * --git` / `---` / `+++` / `@@` lines read as plumbing leaking through, so
 * EVERY plain body under this context prints the hunks' content only (see
 * `PatchSection.body`): the stand-in shown while frames still arrive, the
 * fallback `WarmSwap` holds while Pierre mounts and the text Pierre prints
 * INSTEAD of the diff (its pool down, a section it cannot parse) are then one
 * shape, and the hold — which measures the impl's own height against the held
 * fallback — releases where it should. Only the last, rendered with
 * `degraded`, REPORTS itself, so the row can say why its body is plain and
 * withhold the layout controls only a highlighted body needs; the hold before
 * a paint does not, because the toggle it would gate still shapes the diff
 * that is about to land.
 *
 * Provided by the surface around `PierrePatch`; read by `PlainCodeFallback`.
 * Absent (the default), a plain body prints its text verbatim, which is what
 * every surface without a row of its own needs — the `---`/`+++` lines are
 * then the only thing naming the file — and what plain-diff mode promises.
 */
export interface PlainPatchBodyOwner {
  /** A degraded plain body mounted (`true`) or left (`false`) beneath the row. */
  onPlainBody: (mounted: boolean) => void
}
export const PlainPatchBodyContext = createContext<PlainPatchBodyOwner | null>(null)

/** Plain-text stand-in used while the Pierre chunk loads and for patch text
 *  that does not (yet) parse — e.g. the partial frames of a streaming diff.
 *
 *  Pierre's own geometry is 2px border, 32px header, 16px body padding, and
 *  20px per line. `pierre-plain` wins the transcript's more-specific `<pre>`
 *  rule so a fallback-to-Pierre swap does not move the reader.
 *
 *  This is also the final patch render when plain-diff mode is enabled, so it
 *  preserves a caller class. Recovery and file-pair loading additionally use
 *  the optional header; a collapsed pair renders the header row alone.
 *
 *  Under a `PlainPatchBodyContext` owner the body prints the hunks' content
 *  only, whatever it stands in for. `degraded` marks the body a patch surface
 *  shows INSTEAD of the highlighted diff — no worker generation, an
 *  unparseable patch — as opposed to a hold before the diff paints: only that
 *  body reports itself to the owner. Without an owner the text prints
 *  verbatim and nothing is reported.
 */
export function PlainCodeFallback({ text, className, contentStyle, header, degraded = false }: {
  text: string
  className?: string
  contentStyle?: CSSProperties
  header?: ReactNode
  degraded?: boolean
}) {
  const owner = useContext(PlainPatchBodyContext)
  const reports = degraded && owner != null
  useEffect(() => {
    if (!reports) return
    owner.onPlainBody(true)
    return () => owner.onPlainBody(false)
  }, [reports, owner])
  const hunks = useMemo(() => (owner != null ? plainPatchHunks(text) : null), [owner, text])
  // An entry with nothing left to print — a 100%-similarity rename — is the
  // row alone, as it is when Pierre draws it: a padded empty strip under the
  // row reads as something missing.
  if (hunks != null && hunks.length === 0) return header != null ? <PlainFallbackHeader>{header}</PlainFallbackHeader> : null
  return (
    <>
      {header != null ? <PlainFallbackHeader>{header}</PlainFallbackHeader> : null}
      <pre style={contentStyle} className={`pierre-plain m-0 px-3 py-2 overflow-x-auto text-[13px] font-mono leading-5 whitespace-pre${className ? ` ${className}` : ''}`}>
        {hunks == null
          ? text
          : hunks.map((hunk, i) => (
            <Fragment key={i}>
              {/* Where the `@@` line was: the hairline Pierre's `simple`
                  separator draws between hunks. */}
              {i > 0 && <span aria-hidden className="my-1 block border-t border-border" />}
              {hunk}
            </Fragment>
          ))}
      </pre>
    </>
  )
}

/** The header row shared by the fallbacks, sized to Pierre's own header so a
 *  swap does not move the layout. A collapsed pair is this row alone. */
function PlainFallbackHeader({ children }: { children: ReactNode }) {
  return (
    <div data-diffs-header="" className="flex min-h-9 items-center justify-end gap-1 border-b border-border px-2 py-1">
      {children}
    </div>
  )
}

/**
 * The header row of the simplified file-pair fallback: the caller's prefix
 * slot (the card's expand/collapse control), the filename, the caller's
 * filename suffix, an optional state label, the caller's metadata, and the
 * caller's actions.
 *
 * Exported because two surfaces keep THIS row as their header for the whole
 * life of the card and let Pierre draw the body only: the opted-in oversized
 * pair once the line-by-line diff replaces the two-side body, and the chat diff
 * block in every state. Pierre's own header exists only while its renderer has
 * a highlight result to draw — none while the worker pool is still
 * initialising, recovering, or unavailable, none for a patch that will not
 * parse, none at all in plain-diff mode — so a control slotted into it would
 * vanish in every one of those states. One row, the same component in every
 * state; only the body beneath it changes.
 */
export function PlainFilePairHeader({ filename, titleId, label, titleClickable, stats, renderHeaderPrefix, renderHeaderFilenameSuffix, renderHeaderMetadata, renderHeaderActions }: {
  filename: string
  titleId?: string
  label?: string
  /** The caller opens the file when the filename is clicked (a light-DOM
   *  listener resolving `headerClickAction`), so the filename shows the pointer
   *  and hover accent. Pierre's own header gets the same cue through the
   *  caller's `unsafeCSS`, which is scoped to Pierre's shadow root and cannot
   *  reach this row. */
  titleClickable?: boolean
  /** Exact added/removed line counts, rightmost like Pierre's own. The
   *  opted-in pair reads them off its computed patch and the chat diff block
   *  off the patch it renders; the fallback's bounded scan cannot count lines
   *  and passes none. */
  stats?: { added: number; removed: number }
  renderHeaderPrefix?: () => ReactNode
  renderHeaderFilenameSuffix?: () => ReactNode
  /** The file row's diffstat indicator: drawn INSIDE the metadata group, before
   *  the counts, in the fixed-width box that keeps it at one x across rows. */
  renderHeaderMetadata?: () => ReactNode
  /** The caller's action cluster, drawn BEFORE the counts — where Pierre's own
   *  header shows its metadata slot, which is where the chat diff block's
   *  Open / layout / Copy controls sit when Pierre draws that header — so the
   *  counts stay rightmost. Outside the metadata group: a cluster is wider than
   *  the indicator's box, and it aligns to the counts rather than to a column. */
  renderHeaderActions?: () => ReactNode
}) {
  const metadata = renderHeaderMetadata?.()
  const removed = stats?.removed ?? 0
  const added = stats?.added ?? 0
  return (
    <div
      data-diffs-header
      className="flex min-h-9 items-center gap-2 border-b border-border text-[12px]"
      style={HEADER_ROW_STYLE}
    >
      {/* Wraps: at 320px the filename, the caller's suffix and the label do
          not all share one line — the suffix and the label drop under the name
          before any of them gives way — and an item wider than the line still
          truncates (`min-w-0`) rather than push the row past the card. The
          name keeps the first line with the caller's prefix (its expand
          control), capped at the line so a long name truncates there rather
          than wrap under the control. The actions and the counts keep their
          width; the row grows a line instead. */}
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span className="flex min-w-0 max-w-full items-center gap-1.5">
          {renderHeaderPrefix?.()}
          <span id={titleId} data-title className={`min-w-0 truncate font-mono font-medium${titleClickable ? ' cursor-pointer hover:text-accent' : ''}`}>
            {filename}
          </span>
        </span>
        {renderHeaderFilenameSuffix?.()}
        {/* Shrinks and truncates like the title, with the whole wording
            reachable as the tooltip. */}
        {label != null && <span data-label className="min-w-0 truncate text-[11px] font-normal text-muted" title={label}>{label}</span>}
      </div>
      {renderHeaderActions?.()}
      {/* The same group Pierre's header draws — the caller's metadata (its
          diffstat indicator) pinned left, the ±counts pinned right, in a box of
          fixed width — so the indicator starts at the same x on this row as on
          the within-budget rows around it, whether or not a count is present.
          The box is pinned only when there is an indicator to align: counts on
          their own sit together at the row's edge, as in Pierre's header. */}
      {(metadata != null || removed > 0 || added > 0) && (
        <div data-metadata className="flex shrink-0 items-center gap-2" style={metadata != null ? HEADER_META_GROUP_STYLE : undefined}>
          {metadata}
          {removed > 0 && <span data-deletions-count="" className="font-mono text-danger" style={HEADER_COUNT_STYLE}>-{removed}</span>}
          {added > 0 && <span data-additions-count="" className="font-mono text-ok" style={HEADER_COUNT_STYLE}>+{added}</span>}
        </div>
      )}
    </div>
  )
}

/* Inline because a `unsafeCSS` stylesheet is scoped to Pierre's shadow root and
 * cannot reach a light-DOM row; the values are the ones `ROW_CSS_BASE` injects
 * there (see pierre/config.ts), so the two headers cannot drift apart. */
const HEADER_ROW_STYLE: CSSProperties = {
  backgroundColor: DIFF_HEADER_BG_CSS,
  paddingInline: DIFF_HEADER_PADDING_INLINE_PX,
}
const HEADER_META_GROUP_STYLE: CSSProperties = {
  flex: `0 0 ${DIFF_HEADER_META_W_PX}px`,
  justifyContent: 'space-between',
}
const HEADER_COUNT_STYLE: CSSProperties = {
  display: 'inline-block',
  minWidth: `${DIFF_HEADER_COUNT_MIN_WIDTH_CH}ch`,
  textAlign: 'right',
}

const KEYBOARD_SCROLL_REGION_PROPS = { role: 'region' as const, tabIndex: 0 }

interface PlainFilePairFallbackProps {
  oldFile: FileContents | null
  newFile: FileContents | null
  options?: PierreDiffOptions
  geometry?: 'simplified' | 'pierre-swap'
  text?: string
  className?: string
  contentStyle?: CSSProperties
  renderHeaderMetadata?: () => ReactNode
  renderHeaderPrefix?: () => ReactNode
  renderHeaderFilenameSuffix?: () => ReactNode
  /** See `PlainFilePairHeader`. */
  titleClickable?: boolean
  onShowLineByLineDiff?: () => void
  /** Off-thread compute status for the opt-in. `computing` swaps the button
   *  for a progress label + Cancel; `error` re-offers the button with a short
   *  failure notice. */
  lineByLineState?: 'idle' | 'computing' | 'error'
  onCancelLineByLineDiff?: () => void
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
  geometry = 'simplified',
  text,
  className,
  contentStyle,
  renderHeaderMetadata,
  renderHeaderPrefix,
  renderHeaderFilenameSuffix,
  titleClickable,
  onShowLineByLineDiff,
  lineByLineState = 'idle',
  onCancelLineByLineDiff,
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

  if (geometry === 'pierre-swap') {
    const oldMarker = '-'.repeat(3)
    const newMarker = '+'.repeat(3)
    const fallbackText = text ?? (oldFile && newFile
      ? `${oldMarker} ${oldFile.name}\n${oldFile.contents}\n${newMarker} ${newFile.name}\n${newFile.contents}`
      : (newFile ?? oldFile)?.contents ?? '')
    // Prefix, filename and suffix keep the header's geometry and click
    // selectors; the action cluster (`renderHeaderMetadata`) is left to
    // Pierre's real header — it can be three buttons, and a diff-added row of
    // three is barred by `max-two-buttons-per-row`.
    const header = options?.disableFileHeader === false ? (
      <>
        {renderHeaderPrefix?.()}
        <span data-title="" className="min-w-0 flex-1 truncate">{filename}</span>
        {renderHeaderFilenameSuffix?.()}
      </>
    ) : undefined
    if (options?.collapsed === true) return header != null ? <PlainFallbackHeader>{header}</PlainFallbackHeader> : null
    return <PlainCodeFallback text={fallbackText} className={className} contentStyle={contentStyle} header={header} />
  }
  const sides = [
    oldFile == null ? null : { file: oldFile, lines: oldLines, marker: '−', key: 'old' as const, start: span?.oldStart, end: span?.oldEnd },
    newFile == null ? null : { file: newFile, lines: newLines, marker: '+', key: 'new' as const, start: span?.newStart, end: span?.newEnd },
  ].filter((side): side is { file: FileContents; lines: string[]; marker: string; key: 'old' | 'new'; start: number | undefined; end: number | undefined } => side != null)
  const split = options?.diffStyle === 'split' && sides.length === 2
  const showLineByLineDiffControl = !collapsed && onShowLineByLineDiff ? (
    lineByLineState === 'computing' ? (
      <span className="flex shrink-0 items-center gap-2">
        <span aria-live="polite">{i18nT('components.fileChangeChips.computing_line_by_line_diff')}</span>
        {onCancelLineByLineDiff && (
          <Btn type="button" className="shrink-0" onClick={onCancelLineByLineDiff}>{i18nT('components.fileChangeChips.cancel_line_by_line_diff')}</Btn>
        )}
      </span>
    ) : (
      <span className="flex shrink-0 items-center gap-2">
        {lineByLineState === 'error' && (
          /* No hand-off: this shared fallback also renders dirty working-tree
             diffs (MarkdownPanel routes the current, possibly unsaved buffer
             through PierreFilePair), and the Ask Agent navigation has no dirty
             guard — it would silently discard that buffer. The retry button
             next to this notice is the recovery path. */
          <ErrorNotice
            message={i18nT('components.fileChangeChips.line_by_line_diff_failed')}
            variant="inline"
          />
        )}
        <Btn type="button" className="shrink-0" onClick={onShowLineByLineDiff}>{i18nT('components.fileChangeChips.show_line_by_line_diff')}</Btn>
      </span>
    )
  ) : null

  return (
    <div
      data-pierre-plain-file-pair
      className={`pierre-surface min-w-0 overflow-hidden bg-bg text-text ${className ?? ''}`}
    >
      {showHeader && (
        <PlainFilePairHeader
          filename={filename}
          titleId={titleId}
          label={filePairLabel}
          titleClickable={titleClickable}
          renderHeaderPrefix={renderHeaderPrefix}
          renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
          renderHeaderMetadata={renderHeaderMetadata}
        />
      )}
      {/* Opt-in control lives in its own strip BETWEEN the header and the
          scroller, never inside either: the header already carries the
          caller's prefix and metadata actions (a third one crowds the
          truncating filename at narrow widths), and inside the bounded
          scroller the strip would scroll out of view — or start hidden
          entirely once the fallback opens scrolled to the first change. The
          no-header branch also shows the simplified label here, since it has
          no header to carry it. */}
      {!collapsed && (!showHeader || showLineByLineDiffControl) && (
        <div className="flex items-center justify-between gap-2 border-b border-border bg-bg px-3 py-1 text-[11px] text-muted">
          {!showHeader ? <span>{filePairLabel}</span> : <span />}
          {showLineByLineDiffControl}
        </div>
      )}
      {!collapsed && (
        <div
          ref={contentRef}
          data-pierre-plain-content
          style={contentStyle}
          className={`${split ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1'} relative grid min-w-0 overflow-auto`}
        >
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
