import { memo, useRef, useState } from 'react'
import { Download, Maximize2 } from 'lucide-react'

import ErrorNotice from './ErrorNotice'
import { dispatchLightbox } from './markdown/Lightbox'
import { i18nT } from '../i18n/t'
import { fmtBytes } from '../i18n/format'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { fileTypeIcon, fileTypeLabel } from '../lib/fileTypeIcon'
export interface FileData {
  filename: string
  description?: string
  size?: number
  content_type?: string
}

const ACTION = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border text-[12px] leading-[18px] no-underline text-text hover:border-border-strong hover:bg-bg-hover transition-colors shrink-0'

/**
 * The card's header row: [type glyph] [name / type · size / description]
 * [save]. The glyph is chosen per file family, while a long description keeps
 * its own wrapping line instead of disappearing behind the metadata.
 */
function FileHeader({ file, url, saveLink = true }: { file: FileData; url: string; saveLink?: boolean }) {
  const { Icon, tone, family } = fileTypeIcon(file.filename, file.content_type)
  const meta = [
    fileTypeLabel(file.filename),
    file.size != null && file.size > 0 ? fmtBytes(file.size) : '',
  ].filter(Boolean)
  const save = <><Download className="lucide-inline" aria-hidden /> {i18nT('components.fileCard.save')}</>
  const saveUrl = saveDownloadUrl(url)

  return (
    <div className="grid grid-cols-[28px_1fr_auto] items-center gap-x-3">
      <span
        data-testid="file-card-glyph"
        data-family={family}
        className={`grid place-items-center size-7 rounded-md bg-bg-elevated ring-1 ring-inset ring-border ${tone}`}
      >
        <Icon className="lucide-inline" aria-hidden />
      </span>
      <span className="flex flex-col min-w-0">
        <span className="font-medium truncate">{file.filename}</span>
        {meta.length > 0 && (
          <span className="text-muted text-[12px] leading-5 truncate">{meta.join(' · ')}</span>
        )}
        {file.description && (
          <span className="text-muted text-[12px] leading-5 break-words">{file.description}</span>
        )}
      </span>
      {saveLink ? (
        <a href={saveUrl} download={file.filename} className={ACTION}>{save}</a>
      ) : (
        <span className={ACTION}>{save}</span>
      )}
    </div>
  )
}

/**
 * The URL Save points at. `?download=1` makes the server answer
 * `Content-Disposition: attachment`, so a browser that ignores the link's
 * `download` attribute still saves the file instead of opening a viewer for a
 * type it can render inline (a PDF).
 */
function saveDownloadUrl(url: string): string {
  return `${url}?download=1`
}

/**
 * The image under the header, as a button that opens the full-screen image
 * viewer at this image. Enter and Space come from the native button.
 *
 * The `<img>` carries `data-lightbox-image` + `data-lightbox-turn`, so the
 * viewer pages through it together with the reply's markdown images in the
 * same turn (see `dispatchLightbox`), and `data-lightbox-filename`, so the
 * viewer's Download saves under the file's real name rather than one guessed
 * from the alt text.
 *
 * A preview that fails to load is not a control: there is nothing to enlarge,
 * so the button gives way to an inline error and Save stays the way to the file.
 */
function ImagePreview({ file, url }: { file: FileData; url: string }) {
  const imgRef = useRef<HTMLImageElement>(null)
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  if (failedUrl === url) {
    return (
      <>
        {/* No hand-off: FileCard is store-free and renders in every chat host,
            ChatEmbed included, whose unsaved composer draft lives only in the
            view the hand-off would unmount. Save in the header stays the way
            to the file. */}
        <ErrorNotice variant="inline" message={i18nT('components.markdownRenderer.image_failed_to_load')} testId="file-card-image-error" />
      </>
    )
  }
  return (
    <button
      type="button"
      data-testid="file-card-image-open"
      aria-label={i18nT('components.chatInput.open_preview_of', { name: file.filename })}
      title={i18nT('components.chatInput.open_preview_of', { name: file.filename })}
      className="group/preview relative self-center max-w-full rounded ring-1 ring-transparent hover:ring-border-strong transition-shadow cursor-zoom-in focus-ring-accent"
      onClick={(e) => {
        // A transcript ancestor can bind a fold toggle on click; opening the
        // viewer must not also fold the row.
        e.stopPropagation()
        if (imgRef.current) dispatchLightbox(imgRef.current)
      }}
    >
      {/* onError is a load outcome, not an interaction; the button is the control. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <img
        ref={imgRef}
        src={url}
        alt={file.description || file.filename}
        data-lightbox-image=""
        data-lightbox-turn=""
        data-lightbox-filename={file.filename}
        onError={() => setFailedUrl(url)}
        className="block max-w-full max-h-[400px] rounded object-contain transition-opacity group-hover/preview:opacity-90"
      />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-2 end-2 grid place-items-center size-7 rounded-md bg-black/60 text-white opacity-0 transition-opacity group-hover/preview:opacity-100 group-focus-visible/preview:opacity-100 [@media(hover:none)]:opacity-100"
      >
        <Maximize2 className="lucide-inline" />
      </span>
    </button>
  )
}

const CARD = 'flex flex-col gap-2 bg-card ring-1 ring-inset forced-colors:border ring-border rounded-lg px-4 py-3 text-sm leading-5 animate-scale-in'

/** Renders a file embed card — inline audio/video/image below the header, or a download card. */
export const FileCard = memo(function FileCard({ file }: { file: FileData }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const url = `/api/outbox/${encodeURIComponent(file.filename)}`
  const mime = (file.content_type || '') as string

  if (mime.startsWith('audio/')) {
    return (
      <div className={CARD}>
        <FileHeader file={file} url={url} />
        {/* User-uploaded media: no caption track exists to associate. */}
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <audio controls preload="metadata" className="w-full h-8" src={url} aria-label={i18nT('components.fileCard.audio', { name: file.filename })} />
      </div>
    )
  }

  if (mime.startsWith('video/')) {
    return (
      <div className={CARD}>
        <FileHeader file={file} url={url} />
        {/* User-uploaded media: no caption track exists to associate. */}
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video controls preload="metadata" className="w-full max-h-[300px] rounded" src={url} aria-label={i18nT('components.fileCard.video', { name: file.filename })} />
      </div>
    )
  }

  if (mime.startsWith('image/') && mime !== 'image/svg+xml') {
    return (
      <div className={CARD}>
        <FileHeader file={file} url={url} />
        <ImagePreview file={file} url={url} />
      </div>
    )
  }

  return (
    <a href={saveDownloadUrl(url)} download={file.filename} className={`${CARD} no-underline text-text hover:ring-accent transition-colors cursor-pointer`}>
      <FileHeader file={file} url={url} saveLink={false} />
    </a>
  )
})
