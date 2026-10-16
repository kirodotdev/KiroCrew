/**
 * AWS Control drive - the dialogs the Files pane opens for one object: the
 * in-place preview (`PreviewDialog`), the Move picker (`MoveDialog`) and the
 * share-link dialog (`ShareDialog`). Each is a presentational scrim around a
 * dialog panel, with modal keyboard behaviour from `useDialogFocusTrap`. The
 * preview fetches its own presigned URL or text, and the share dialog mints the
 * link.
 */
import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
// By path, not from the app-sdk barrel: that barrel is published to third-party
// apps through the vendor stub, and a host-namespaced cache client is not part
// of that contract.
import { useAppQuery, useAppQueryKey } from '../../app-sdk/appQuery'
import { Download, FolderClosed, FileText, X } from 'lucide-react'
import { Btn, Input, ContentSkeleton, IconButton } from '../../components/ui'
import { detectFileType } from '../../components/FileRenderers'
import { ContentRenderer, MD_EXTS, extOf, langFor, wrapCode } from '../../components/ContentRenderer'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { i18nT } from '../../i18n/t'
import { fmtBytes } from '../../i18n/format'
import { awsControlApi } from './api'
import type { DriveSection } from './types'
import { CopyBtn, AwsErrorNotice } from './shared'
import type { Failure } from './useDrivePageFiles'

const EXPIRY_LABEL_KEY: Record<string, string> = {
  '1h': 'apps.awsControl.console.expiry_1h',
  '1d': 'apps.awsControl.console.expiry_1d',
  '7d': 'apps.awsControl.console.expiry_7d',
}

/* Preview routes by EXTENSION, not by fetching first: the two transports
   differ. Media (img/video/audio/iframe tags) load the presigned URL directly
   — those tags are exempt from CORS, which a browser fetch of the same URL is
   not (the bucket carries no CORS config). Text goes through the gateway's
   preview endpoint for the same reason. Anything else gets an honest
   "download to view" instead of a broken pane.

   WHICH renderer a text file gets is not decided here: `detectFileType` owns
   that for the whole dashboard, and this pane reads its answer like the file
   side panel does. What IS decided here is whether the bytes may be read as
   text at all -- an unknown extension is a download, not 256 KB of mojibake --
   and which of the two transports fetches them. */
const PREVIEW_TEXT = new Set([
  '.txt', '.md', '.markdown', '.mdx', '.csv', '.tsv', '.json', '.jsonl', '.log',
  '.yaml', '.yml', '.xml', '.html', '.htm', '.css', '.js', '.mjs', '.cjs', '.ts',
  '.tsx', '.jsx', '.py', '.sh', '.toml', '.ini', '.cfg', '.sql', '.go', '.rs',
  '.java', '.kt', '.rb', '.excalidraw',
])
/* A read that stopped at the preview cap is a PREFIX. Line-oriented content
   (markdown, code, csv, jsonl, html) reads fine as one; a single-document type
   does not -- half a JSON object is not a broken file, it is an unfinished
   read, and its viewer would accuse the file of being invalid. Those two show
   their source under the truncation notice, which says what actually happened. */
const WHOLE_DOC_TYPES = new Set(['json', 'excalidraw'])
/* The renderer's `onChange` is for its editing surface, which this pane never
   mounts (`editing` is always false). Hoisted so it is one stable identity
   rather than a new closure per render. */
const NOOP = () => {}

type PreviewKind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'none'

function previewKind(key: string): PreviewKind {
  const type = detectFileType(key)
  // Bytes: the tag loads the presigned URL itself. `.svg` lands here too --
  // detectFileType calls a path-backed SVG an image, and an <img>-loaded SVG
  // cannot run script, which is the right answer for a bucket object.
  if (type === 'image' || type === 'video' || type === 'audio' || type === 'pdf') return type
  // Spreadsheets and Office documents render through a GATEWAY-SIDE parse
  // (openpyxl / doc_parser) of a file on disk. A drive object is in S3, so
  // there is nothing for that parse to open: they stay a download.
  if (type === 'sheet' || type === 'office') return 'none'
  return PREVIEW_TEXT.has(extOf(key)) ? 'text' : 'none'
}

/**
 * The body of a text preview, rendered by the dashboard's own file renderers
 * rather than by anything written for this pane.
 *
 * `ContentRenderer` is the same dispatcher the file side panel and the artifact
 * detail page render through, so markdown, a csv table, a JSON tree, a jsonl
 * stream, a sandboxed HTML page, an Excalidraw scene and syntax-highlighted
 * code all look here exactly as they look there -- and a renderer added to the
 * SDK later arrives here for free. What this pane still owns is the TRANSPORT:
 * the bytes came from the gateway's preview endpoint, capped and redacted,
 * which is what the two notices above this body are about.
 */
function PreviewBody({ fileKey, content, truncated }: { fileKey: string; content: string; truncated: boolean }) {
  const bodyRef = useRef<HTMLDivElement>(null)
  const type = detectFileType(fileKey)
  const ext = extOf(fileKey)
  const isMarkdown = MD_EXTS.has(ext)
  if (truncated && WHOLE_DOC_TYPES.has(type)) {
    return (
      <pre className="whitespace-pre-wrap break-words text-[12px] leading-relaxed text-text" data-testid="drive-preview-text">
        {content}
      </pre>
    )
  }
  const body = (
    <ContentRenderer
      // Everything left after markdown and code has its own viewer, so the flag
      // is derived rather than restated as a third list of extensions.
      isRichType={!isMarkdown && type !== 'code'}
      fileType={type}
      content={content}
      editing={false}
      lang={langFor(ext)}
      lineNums
      wordWrap
      onChange={NOOP}
      previewRef={bodyRef}
      // csv ONLY, and the narrowness is the point: `CsvViewer` reads the
      // extension to choose its delimiter, so without a key a `.tsv` splits on
      // commas and every row collapses into one cell. The other types must NOT
      // get it -- the renderer would take a drive key for a path on disk, which
      // for markdown means relative image links resolving into a gateway
      // filesystem read of a path derived from an S3 key.
      filePath={type === 'csv' ? fileKey : undefined}
      displayContent={isMarkdown ? content : wrapCode(content, ext)}
      isMarkdown={isMarkdown}
      markdownClassName="msg-content text-sm leading-relaxed"
    />
  )
  // Prose grows and lets the dialog scroll it. Every other viewer owns its own
  // scroller and measures against its box, so an unbounded parent collapses it
  // to nothing -- they get the same 70vh the media branches use.
  return isMarkdown
    ? <div data-testid="drive-preview-text">{body}</div>
    : <div className="h-[70vh]" data-testid="drive-preview-text">{body}</div>
}

/** In-place file preview. Same scrim/panel/focus-trap shape as
 *  AddFromArtifactsDialog — no third dialog grammar. */
export function PreviewDialog({
  account, entry, onDownload, downloadError, onClose,
}: {
  account: string
  entry: { key: string; size: number }
  onDownload: (key: string) => void
  /** The pane's download failure, when the click came from THIS dialog's
   *  header: the pane's own notice renders behind the dialog's scrim, so a
   *  failed download from here would show only a tab flashing closed. */
  downloadError: Failure | null
  onClose: () => void
}) {
  const kind = previewKind(entry.key)
  const panelRef = useRef<HTMLDivElement>(null)
  const backdropDown = useRef(false)
  useDialogFocusTrap(panelRef, onClose)
  const [mediaError, setMediaError] = useState(false)
  const isMedia = kind === 'image' || kind === 'video' || kind === 'audio' || kind === 'pdf'
  const urlQ = useAppQuery(['drive-preview-url', account, entry.key], {
    queryFn: () => awsControlApi.driveDownload(account, 'drive', entry.key),
    enabled: isMedia,
    // The presign is minted per open on purpose: it is short-lived, and a
    // cached URL that outlives its signature renders as a broken image.
    gcTime: 0,
    staleTime: 0,
    retry: false,
  })
  /* The download presign is a 60-second grant sized for one click, and a
     <video>/<audio> keeps issuing ranged GETs for as long as it plays -- so a
     clip longer than the grant hits S3 403 mid-file. The element reports that
     as a media error; the response is to re-mint the URL (another short grant,
     not a longer one) and resume from where playback stopped. Each successful
     resume re-arms the re-mint, so a clip spanning many grants keeps playing;
     what stops it is two errors in a row with no media loaded between them --
     that is a URL that never worked, not a grant that ran out. */
  const remintPendingRef = useRef(false)
  const resumeAtRef = useRef(0)
  const onMediaError = (el?: HTMLMediaElement) => {
    if (remintPendingRef.current) { setMediaError(true); return }
    remintPendingRef.current = true
    resumeAtRef.current = el?.currentTime ?? 0
    void urlQ.refetch()
  }
  const onMediaReady = (e: { currentTarget: HTMLMediaElement }) => {
    remintPendingRef.current = false
    if (resumeAtRef.current > 0) {
      e.currentTarget.currentTime = resumeAtRef.current
      resumeAtRef.current = 0
    }
  }
  const textQ = useAppQuery(['drive-preview-text', account, entry.key], {
    queryFn: () => awsControlApi.drivePreview(account, 'drive', entry.key),
    enabled: kind === 'text',
    retry: false,
  })
  const name = entry.key.split('/').pop() ?? entry.key
  const dir = entry.key.includes('/') ? entry.key.slice(0, entry.key.lastIndexOf('/')) : ''
  const loading = kind === 'text' ? textQ.isLoading : isMedia ? urlQ.isLoading : false
  const failed = mediaError || (kind === 'text' ? textQ.isError : isMedia ? urlQ.isError : false)
  /* A `.pdf` key is only renderable when S3 serves it as a PDF. Objects
     uploaded before content types were set come back as octet-stream, which
     the sandboxed iframe (no allow-downloads) can neither render nor hand to
     the browser -- it shows an empty frame and fires no error. The HEAD behind
     the presign carries the stored type, so those route to the same "cannot
     be previewed, download it" fallback an unknown extension gets. A missing
     type (null) is left to the frame: S3 always records one, so null means
     the field was not reported, not that the object is wrong. */
  const pdfNotRenderable =
    kind === 'pdf' && typeof urlQ.data?.contentType === 'string' &&
    !urlQ.data.contentType.toLowerCase().startsWith('application/pdf')
  const unsupported = kind === 'none' || pdfNotRenderable
  const url = urlQ.data?.url

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 sm:p-8"
      data-testid="drive-preview-dialog"
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) onClose(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-label={name}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <FileText size={14} className="shrink-0 text-muted" aria-hidden="true" />
            <h3 className="flex min-w-0 items-baseline gap-1.5 text-sm font-semibold text-text-strong">
              <span className="truncate">{name}</span>
              {/* Opened from search, two same-named hits from different folders
                  would be indistinguishable once the dialog is up; the folder
                  rides along muted whenever the key has one. */}
              {dir && (
                <span className="truncate text-[11px] font-normal text-muted" data-testid="drive-preview-dir">{dir}/</span>
              )}
            </h3>
            <span className="shrink-0 text-[11px] text-muted">{fmtBytes(entry.size)}</span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Btn onClick={() => onDownload(entry.key)} data-testid="drive-preview-download">
              <Download size={13} />{i18nT('apps.awsControl.console.download')}
            </Btn>
            <IconButton
              onClick={onClose}
              aria-label={i18nT('apps.awsControl.console.close')}
              data-testid="drive-preview-close"
            >
              <X className="h-4 w-4" />
            </IconButton>
          </div>
        </div>
        <div className="min-h-[160px] overflow-auto p-4">
          <AwsErrorNotice
            error={downloadError?.error}
            message={downloadError?.message}
            askAgent
            className="mb-3"
            testId="drive-preview-download-error"
          />
          {loading && <ContentSkeleton rows={4} />}
          {/* Two different "nothing to show". An unsupported TYPE is a status,
              not a failure — nothing was tried — so it stays plain text. A
              failed load is an error and goes through the shared notice like
              every other failure in this app: Try again re-issues the read
              (the presign for media, the gateway read for text), and the
              hand-off carries the thrown value when there is one. The dialog
              holds no draft, so the hand-off is always offered. */}
          {!loading && !failed && unsupported && (
            <p className="text-[13px] text-muted" data-testid="drive-preview-fallback">
              {i18nT('apps.awsControl.console.preview_unsupported')}
            </p>
          )}
          {!loading && failed && (
            <AwsErrorNotice
              error={kind === 'text' ? textQ.error : urlQ.error}
              message={i18nT('apps.awsControl.console.preview_failed')}
              askAgent
              onRetry={() => {
                setMediaError(false)
                remintPendingRef.current = false
                void (kind === 'text' ? textQ.refetch() : urlQ.refetch())
              }}
              testId="drive-preview-error"
            />
          )}
          {!loading && !failed && kind === 'image' && url && (
            // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is a load-failure hook, not an interaction
            <img
              src={url}
              alt={name}
              className="mx-auto max-h-[70vh] max-w-full object-contain"
              onError={() => onMediaError()}
              data-testid="drive-preview-image"
            />
          )}
          {!loading && !failed && kind === 'video' && url && (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- user files carry no caption track
            <video src={url} controls aria-label={name} className="mx-auto max-h-[70vh] max-w-full" onError={(e) => onMediaError(e.currentTarget)} onLoadedMetadata={onMediaReady} data-testid="drive-preview-video" />
          )}
          {!loading && !failed && kind === 'audio' && url && (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- user files carry no caption track
            <audio src={url} controls aria-label={name} className="w-full" onError={(e) => onMediaError(e.currentTarget)} onLoadedMetadata={onMediaReady} data-testid="drive-preview-audio" />
          )}
          {!loading && !failed && kind === 'pdf' && !pdfNotRenderable && url && (
            // Only reached when the stored Content-Type says PDF (or was not
            // reported): an octet-stream `.pdf` is routed to the unsupported
            // fallback above instead of an empty frame. The empty sandbox is
            // load-bearing: the extension picks this branch, so a `.pdf` key
            // holding HTML would otherwise run script and could navigate the
            // top window. Rendering a PDF needs no sandbox permission.
            <iframe src={url} title={name} sandbox="" className="h-[70vh] w-full rounded border border-border" data-testid="drive-preview-pdf" />
          )}
          {!loading && !failed && kind === 'text' && textQ.data && (
            <>
              {textQ.data.truncated && (
                <p className="mb-2 text-[11px] text-muted" data-testid="drive-preview-truncated">
                  {i18nT('apps.awsControl.console.preview_truncated')}
                </p>
              )}
              {/* The redactor rewrites the text silently; a reader checking a
                  config file would otherwise take the masked value for the
                  file's own bytes. NOT the truncation note's muted weight: that
                  one says "there is more below", this one changes the meaning
                  of what is shown, so it reads at body weight. */}
              {textQ.data.redacted && (
                <p className="mb-2 text-[12px] font-medium text-text" data-testid="drive-preview-redacted">
                  {i18nT('apps.awsControl.console.preview_redacted')}
                </p>
              )}
              <PreviewBody fileKey={entry.key} content={textQ.data.content} truncated={textQ.data.truncated} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * A folder picker for one file. Small on purpose: the destinations a reader
 * can see from the current folder, one button each, no tree walk. Modal
 * keyboard behaviour (focus in, Tab ring, Escape, focus restore) comes from the
 * shared hook, exactly as `AddFromArtifactsDialog`, the Library picker.
 */
export function MoveDialog({ fileKey, currentPath, folders, pending, error, askAgent, onMove, onClose }: {
  fileKey: string
  /** The folder the file lives in ('' = top level). */
  currentPath: string
  /** Full paths of the sub-folders in the current listing. */
  folders: string[]
  pending: boolean
  /** The refused move, reported inside the picker so the reader can pick again. */
  error: Failure | null
  /** The picker holds no draft of its own, so the hand-off is the page's call. */
  askAgent: boolean
  onMove: (folder: string) => void
  onClose: () => void
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  // `restoreFocus: false`: at mount the previously focused element is a menu
  // item that unmounted in the same commit, so the hook would capture `body`.
  // The caller knows who opened the menu and focuses them on close.
  useDialogFocusTrap(panelRef, onClose, { restoreFocus: false })
  const backdropDown = useRef(false)
  const name = fileKey.split('/').pop() ?? fileKey
  const crumbs = currentPath.split('/').filter(Boolean)
  const parent = crumbs.length > 1 ? crumbs.slice(0, -1).join('/') : null
  // Up-links first (the way a file manager orders them), then the sub-folders
  // in listing order. Top level appears only when the file is not already
  // there; the parent only when it is not the top level (which the first
  // entry already covers).
  const options: Array<{ folder: string; label: string; testId: string }> = []
  if (currentPath) options.push({ folder: '', label: i18nT('apps.awsControl.console.move_root'), testId: 'move-root' })
  if (parent) options.push({ folder: parent, label: i18nT('apps.awsControl.console.move_up', { name: crumbs[crumbs.length - 2] }), testId: 'move-up' })
  for (const f of folders) options.push({ folder: f, label: f.split('/').pop() ?? f, testId: 'move-folder' })

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) onClose(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="w-full max-w-sm rounded-xl border border-border bg-card p-4 shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-labelledby="aws-move-title"
        aria-busy={pending || undefined}
        data-testid="move-dialog"
      >
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 id="aws-move-title" className="min-w-0 truncate text-sm font-semibold text-text-strong">
            {i18nT('apps.awsControl.console.move_title', { name })}
          </h3>
          <IconButton
            onClick={onClose}
            aria-label={i18nT('apps.awsControl.console.close')}
            data-testid="move-close"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </div>
        {options.length === 0 ? (
          <p className="text-[13px] text-muted" data-testid="move-no-folders">
            {i18nT('apps.awsControl.console.move_no_folders')}
          </p>
        ) : (
          <div className="flex max-h-72 flex-col gap-1 overflow-y-auto" data-testid="move-options">
            {options.map((o) => (
              <button
                key={o.testId + o.folder}
                onClick={() => onMove(o.folder)}
                disabled={pending}
                className="flex w-full items-center gap-2 rounded-md border-none bg-transparent px-2.5 py-2 text-left text-[13px] text-text cursor-pointer hover:bg-bg-hover focus-ring disabled:opacity-50"
                data-testid={o.testId}
                data-folder={o.folder}
              >
                <FolderClosed size={14} className="shrink-0 text-muted" aria-hidden="true" />
                <span className="min-w-0 truncate">{o.label}</span>
              </button>
            ))}
          </div>
        )}
        {pending && (
          <p className="mt-2 text-[12px] text-muted" data-testid="move-pending">
            {i18nT('apps.awsControl.console.move_moving')}
          </p>
        )}
        <AwsErrorNotice
          error={error?.error}
          message={error?.message}
          askAgent={askAgent}
          className="mt-2"
          testId="move-error"
        />
      </div>
    </div>
  )
}

const EXPIRY_OPTIONS: Array<{ key: string; secs: number }> = [
  { key: '1h', secs: 3600 },
  { key: '1d', secs: 86400 },
  { key: '7d', secs: 604800 },
]

export function ShareDialog({ account, section, fileKey, onClose }: { account: string; section: DriveSection; fileKey: string; onClose: () => void }) {
  const qc = useQueryClient()
  const appKey = useAppQueryKey()
  const [secs, setSecs] = useState(3600)
  const [note, setNote] = useState('')
  const shareMut = useMutation({
    mutationFn: () => awsControlApi.driveShare(account, section, fileKey, secs, note),
    onSuccess: () => qc.invalidateQueries({ queryKey: appKey(['shares', account]) }),
  })
  const url = shareMut.data?.url

  /* Modal keyboard behaviour from the shared hook, as every dialog in this
     file: focus moves in on open, Tab cycles inside, Escape closes. Escape is
     refused while the link is being minted -- closing then would discard a
     share that is about to exist with no way to see its URL. Focus RETURN is
     the opener's job (`restoreFocus: false`): this dialog is opened from a
     Radix menu item that unmounts in the same commit, so the element focused
     at mount is `body`, and the drive section knows the real opener. */
  const panelRef = useRef<HTMLDivElement>(null)
  const backdropDown = useRef(false)
  const close = () => { if (!shareMut.isPending) onClose() }
  useDialogFocusTrap(panelRef, close, { restoreFocus: false })

  return (
    // Scrim and panel are two elements: the scrim owns click-to-dismiss (a
    // press that starts AND ends on it), the panel is the dialog. Same shape
    // as `AddFromArtifactsDialog`, the Library picker.
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) close(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="w-full max-w-md rounded-xl border border-border bg-card p-4 shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-labelledby="aws-share-title"
        data-testid="share-dialog"
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 id="aws-share-title" className="min-w-0 truncate text-sm font-semibold text-text-strong">
            {/* Names the file: a link publishes it externally, so the reader
                must see WHICH file before creating one — the Move picker
                beside this dialog already does the same. */}
            {i18nT('apps.awsControl.console.share_title', { name: fileKey.split('/').pop() ?? fileKey })}
          </h3>
          <IconButton onClick={close} disabled={shareMut.isPending} aria-label={i18nT('apps.awsControl.console.close')} data-testid="share-close"><X className="h-4 w-4" /></IconButton>
        </div>

        {!url ? (
          <>
            <span className="mb-1 block text-[12px] text-muted">{i18nT('apps.awsControl.console.share_expiry')}</span>
            <div className="mb-3 flex gap-1.5" data-testid="share-expiry" role="group" aria-label={i18nT('apps.awsControl.console.share_expiry')}>
              {EXPIRY_OPTIONS.map((o) => (
                <button
                  key={o.key}
                  onClick={() => setSecs(o.secs)}
                  aria-pressed={secs === o.secs}
                  className={`rounded-md border px-2.5 py-1 text-[13px] cursor-pointer transition-colors ${secs === o.secs ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-transparent text-muted hover:text-text'}`}
                  data-testid={`share-expiry-${o.key}`}
                >
                  {i18nT(EXPIRY_LABEL_KEY[o.key])}
                </button>
              ))}
            </div>
            <label htmlFor="aws-share-note" className="mb-1 block text-[12px] text-muted">{i18nT('apps.awsControl.console.share_note')}</label>
            <Input id="aws-share-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={i18nT('apps.awsControl.console.share_note_placeholder')} className="mb-3 w-full" data-testid="share-note" />
            {/* A refused share (publishing denied by policy, a dead key) used to
                leave the button simply re-enabled, as if nothing had been asked.
                No hand-off here: the note field above still holds whatever was
                typed, and the hand-off navigates away from it. The refusal is
                journaled regardless, so the agent can still be asked afterwards. */}
            <AwsErrorNotice
              error={shareMut.error}
              message={shareMut.isError ? i18nT('apps.awsControl.console.share_failed') : null}
              askAgent={false}
              className="mb-3"
              testId="share-error"
            />
            <Btn primary onClick={() => shareMut.mutate()} disabled={shareMut.isPending} data-testid="share-create">
              {shareMut.isPending ? i18nT('apps.awsControl.console.share_creating') : i18nT('apps.awsControl.console.share_create')}
            </Btn>
          </>
        ) : (
          <div data-testid="share-result">
            <div className="mb-2 flex items-center gap-2">
              <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">{url}</code>
              <CopyBtn text={url} testId="share-copy" />
            </div>
            <p className="text-[12px] text-muted">{i18nT('apps.awsControl.console.share_expires_note')}</p>
            <p className="mt-1 text-[12px] text-muted">{i18nT('apps.awsControl.console.share_credentials_caveat')}</p>
          </div>
        )}
      </div>
    </div>
  )
}
