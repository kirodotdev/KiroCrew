/**
 * MIME-aware file-icon lookup for the attachment card.
 *
 * The shared extension, icon and tone map lives in utils/fileIcons.ts. MIME
 * wins here when the sender supplied one; unrecognised input stays generic.
 */
import type { LucideIcon } from 'lucide-react'

import {
  fileExtension,
  fileFamilyForPath,
  fileIcon,
  fileIconForFamily,
  fileToneForFamily,
  type FileFamily,
} from '../utils/fileIcons'

export type { FileFamily }

export interface FileTypeIcon {
  family: FileFamily
  Icon: LucideIcon
  /** Theme text-colour utility for the glyph. */
  tone: string
}

/** Exact MIME types that a `startsWith` prefix rule would misfile. */
const MIME_EXACT: Record<string, FileFamily> = {
  'application/pdf': 'document',
  'application/msword': 'document',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'document',
  'application/vnd.oasis.opendocument.text': 'document',
  'application/rtf': 'document',
  'text/plain': 'document',
  'text/markdown': 'document',
  'application/vnd.ms-excel': 'spreadsheet',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'spreadsheet',
  'application/vnd.oasis.opendocument.spreadsheet': 'spreadsheet',
  'text/csv': 'spreadsheet',
  'text/tab-separated-values': 'spreadsheet',
  'application/vnd.ms-powerpoint': 'slides',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'slides',
  'application/vnd.oasis.opendocument.presentation': 'slides',
  'application/vnd.apple.keynote': 'slides',
  'application/json': 'data',
  'application/xml': 'data',
  'text/xml': 'data',
  'application/yaml': 'data',
  'application/toml': 'data',
  'text/html': 'code',
  'text/css': 'code',
  'text/javascript': 'code',
  'application/javascript': 'code',
  'application/typescript': 'code',
  'application/zip': 'archive',
  'application/gzip': 'archive',
  'application/x-tar': 'archive',
  'application/x-7z-compressed': 'archive',
  'application/vnd.rar': 'archive',
  'application/x-bzip2': 'archive',
  'application/x-xz': 'archive',
  'application/x-pem-file': 'key',
  'application/x-x509-ca-cert': 'key',
  'application/pkcs12': 'key',
  'application/x-pkcs12': 'key',
  'application/vnd.apple.diskimage': 'installer',
  'application/x-apple-diskimage': 'installer',
  'application/x-msdownload': 'installer',
  'application/x-msi': 'installer',
  'application/vnd.debian.binary-package': 'installer',
  'application/x-rpm': 'installer',
  'application/vnd.android.package-archive': 'installer',
  'application/vnd.sqlite3': 'database',
  'application/x-sqlite3': 'database',
  'application/sql': 'database',
  'application/vnd.apache.parquet': 'database',
  'text/x-diff': 'patch',
  'text/x-patch': 'patch',
  'application/epub+zip': 'ebook',
  'application/x-mobipocket-ebook': 'ebook',
  'text/calendar': 'calendar',
  'text/vcard': 'calendar',
  'application/x-ipynb+json': 'notebook',
  'model/stl': 'model3d',
  'model/obj': 'model3d',
  'model/gltf+json': 'model3d',
  'model/gltf-binary': 'model3d',
}

export function fileFamilyOf(filename: string, contentType?: string | null): FileFamily {
  const mime = (contentType || '').split(';')[0].trim().toLowerCase()
  if (mime) {
    const exact = MIME_EXACT[mime]
    if (exact) return exact
    if (mime.startsWith('image/')) return 'image'
    if (mime.startsWith('video/')) return 'video'
    if (mime.startsWith('audio/')) return 'audio'
    if (mime.startsWith('font/')) return 'font'
    if (mime.startsWith('model/')) return 'model3d'
  }
  return fileFamilyForPath(filename)
}

export function fileTypeIcon(filename: string, contentType?: string | null): FileTypeIcon {
  const family = fileFamilyOf(filename, contentType)
  const extensionFamily = fileFamilyForPath(filename)
  return {
    family,
    Icon: extensionFamily === family ? fileIcon(filename) : fileIconForFamily(family),
    tone: fileToneForFamily(family),
  }
}

/** Show an extension only when the shared map recognises it as a file type. */
export function fileTypeLabel(filename: string): string {
  if (fileFamilyForPath(filename) === 'unknown') return ''
  return fileExtension(filename).toUpperCase()
}
