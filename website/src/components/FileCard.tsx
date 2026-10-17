import { memo } from 'react'
import { Download } from 'lucide-react'

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
        <a href={url} download className={ACTION}>{save}</a>
      ) : (
        <span className={ACTION}>{save}</span>
      )}
    </div>
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
        <img src={url} alt={file.description || file.filename} className="max-w-full max-h-[400px] rounded object-contain" />
      </div>
    )
  }

  return (
    <a href={url} download className={`${CARD} no-underline text-text hover:ring-accent transition-colors cursor-pointer`}>
      <FileHeader file={file} url={url} saveLink={false} />
    </a>
  )
})
