import { memo } from 'react'
import { Music, Video, Image, Paperclip, ArrowDown } from 'lucide-react'

import { i18nT } from '../i18n/t'
export interface FileData {
  filename: string
  description?: string
  size?: number
  content_type?: string
}

/** Renders a file embed card — inline audio/video player or download link. */
export const FileCard = memo(function FileCard({ file }: { file: FileData }) {
  const url = `/api/outbox/${encodeURIComponent(file.filename)}`
  const mime = (file.content_type || '') as string

  if (mime.startsWith('audio/')) {
    return (
      <div className="flex flex-col gap-2 bg-card border border-border rounded-lg px-4 py-3 animate-scale-in">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-xl"><Music className="lucide-inline" /></span>
          <span className="font-medium truncate">{file.filename}</span>
          {file.description && <span className="text-muted text-[12px]">— {file.description}</span>}
        </div>
        {/* User-uploaded media: no caption track exists to associate. */}
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <audio controls preload="metadata" className="w-full h-8" src={url} aria-label={i18nT('components.fileCard.audio', { name: file.filename })} />
      </div>
    )
  }

  if (mime.startsWith('video/')) {
    return (
      <div className="flex flex-col gap-2 bg-card border border-border rounded-lg px-4 py-3 animate-scale-in">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-xl"><Video className="lucide-inline" /></span>
          <span className="font-medium truncate">{file.filename}</span>
          {file.description && <span className="text-muted text-[12px]">— {file.description}</span>}
        </div>
        {/* User-uploaded media: no caption track exists to associate. */}
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video controls preload="metadata" className="w-full max-h-[300px] rounded" src={url} aria-label={i18nT('components.fileCard.video', { name: file.filename })} />
      </div>
    )
  }

  if (mime.startsWith('image/') && mime !== 'image/svg+xml') {
    return (
      <div className="flex flex-col gap-2 bg-card border border-border rounded-lg px-4 py-3 animate-scale-in">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-xl"><Image className="lucide-inline" /></span>
          <span className="font-medium truncate">{file.filename}</span>
          {file.description && <span className="text-muted text-[12px]">— {file.description}</span>}
        </div>
        <img src={url} alt={file.description || file.filename} className="max-w-full max-h-[400px] rounded object-contain" />
      </div>
    )
  }

  return (
    <a href={url} download className="flex items-center gap-3 bg-card border border-border rounded-lg px-4 py-3 text-sm no-underline text-text hover:border-accent transition-colors animate-scale-in cursor-pointer">
      <span className="text-xl"><Paperclip className="lucide-inline" /></span>
      <span className="flex flex-col gap-0.5 min-w-0">
        <span className="font-medium truncate">{file.filename}</span>
        {file.description && <span className="text-muted text-[12px]">{file.description}</span>}
        {file.size != null && file.size > 0 && <span className="text-muted text-[12px]">{file.size < 1024 ? `${file.size} B` : file.size < 1024 * 1024 ? `${(file.size / 1024).toFixed(1)} KB` : `${(file.size / (1024 * 1024)).toFixed(1)} MB`}</span>}
      </span>
      <span className="ml-auto text-accent text-[13px] font-medium shrink-0"><ArrowDown className="lucide-inline" /> {i18nT('components.fileCard.save')}</span>
    </a>
  )
})
