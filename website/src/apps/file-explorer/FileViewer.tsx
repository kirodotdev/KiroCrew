import { FileText, AlertTriangle, FileQuestion, RefreshCw, Download, Copy, ExternalLink, MoreHorizontal, ShieldAlert } from 'lucide-react'
import { useIsMobile } from '../../hooks/useIsMobile'
import MarkdownRenderer, { BasePathCtx } from '../../components/MarkdownRenderer'
import { EmptyState, Skeleton } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { IMAGE_EXTS, LANG_BY_EXT } from './constants'
import { extOf, basename, formatBytes, formatTime, isSensitivePath } from './utils'
import { copyToClipboard } from '../../utils/clipboard'
import { api } from '../../api/client'
import { useGatewayPlatform } from '../../hooks/useGatewayPlatform'
import type { FileMeta } from './types'

import { i18nT } from '../../i18n/t'
interface FileViewerProps {
  filePath: string | null
  fileMeta: FileMeta | null
  content: string
  loading: boolean
  error: string | null
  onReload: () => void
  onDownload: () => void
}

/**
 * Select the open file in the host's file manager (Finder, Explorer, or the
 * Linux equivalent), which is what makes the enclosing folder reachable from a
 * path the dashboard only shows as text. The label stays platform-neutral
 * because the endpoint serves all three.
 *
 * A headless host has no file manager, and the backend says so by answering
 * with `copy` rather than an error — `api.revealPath` puts the path on the
 * clipboard in that case, so the alert tells the user why nothing appeared on
 * screen instead of leaving the click looking broken. A refusal (the SEL guard
 * treats the path as sensitive) surfaces the server's own message.
 */
async function revealFile(filePath: string) {
  try {
    const res = await api.revealPath(filePath)
    if (res?.copy) alert(i18nT('apps.fileExplorer.fileViewer.path_copied_to_clipboard_no_desktop_available'))
  } catch (err) {
    // eslint-disable-next-line no-console -- surface reveal failures for diagnostics
    console.error('revealPath failed', err)
    alert((err as Error).message)
  }
}

function renderViewerBody({ ext, fileMeta, content, openFile }: { ext: string; fileMeta: FileMeta; content: string; openFile: string }) {
  if (fileMeta.binary && fileMeta.encoding !== 'base64') {
    return <EmptyState icon={<FileQuestion size={22} />} title={i18nT('apps.fileExplorer.fileViewer.binary_file')} subtitle={`${formatBytes(fileMeta.size)} · ${fileMeta.mime || 'unknown'}`} />
  }
  if (IMAGE_EXTS.has(ext) && fileMeta.encoding === 'base64') {
    const src = `data:${fileMeta.mime || 'image/png'};base64,${content}`
    return <div className="mc-fe-img-wrap"><img src={src} alt={openFile} style={{ maxWidth: '100%', maxHeight: '100%' }} /></div>
  }
  if (ext === '.md' || ext === '.markdown') {
    return <BasePathCtx.Provider value={openFile}><MarkdownRenderer content={content || ''} /></BasePathCtx.Provider>
  }
  const lang = LANG_BY_EXT[ext] || 'plaintext'
  const maxRun = (content || '').match(/`{3,}/g)?.reduce((max, s) => Math.max(max, s.length), 0) ?? 0
  const fence = '`'.repeat(Math.max(3, maxRun + 1))
  const wrapped = fence + lang + '\n' + (content || '') + '\n' + fence
  return <MarkdownRenderer content={wrapped} />
}

export default function FileViewer({ filePath, fileMeta, content, loading, error, onReload, onDownload }: FileViewerProps) {
  const isMobile = useIsMobile()
  // Before the early returns: a hook cannot sit behind a conditional.
  const gatewayPlatform = useGatewayPlatform()
  if (!filePath) {
    return <EmptyState icon={<FileText size={28} />} title={i18nT('apps.fileExplorer.fileViewer.select_a_file_to_view')} subtitle={isMobile ? undefined : i18nT('apps.fileExplorer.fileViewer.tip_ctrl_cmd_f_to_search')} />
  }
  if (loading) return <Skeleton className="h-full w-full" />
  if (error) {
    return <EmptyState icon={<AlertTriangle size={22} style={{ color: 'var(--danger)' }} />} title={error} />
  }
  if (!fileMeta) return null

  const ext = extOf(filePath)
  const fileName = basename(filePath)
  const copyPath = () => { copyToClipboard(filePath) }
  // Name the real application where the platform HAS one — Finder, File
  // Explorer — and fall back to the generic term for Linux and for a gateway
  // whose platform we could not read. The platform is the GATEWAY's, not the
  // browser's: the reveal shells out on the host.
  const revealLabel = gatewayPlatform === 'darwin'
    ? i18nT('apps.fileExplorer.fileViewer.open_in_finder')
    : gatewayPlatform === 'windows'
      ? i18nT('apps.fileExplorer.fileViewer.open_in_file_explorer')
      : i18nT('apps.fileExplorer.fileViewer.show_in_file_manager')

  return (
    <>
      <div className="mc-fe-viewer-bar">
        <div className="mc-fe-viewer-title">
          <FileText size={14} style={{ marginRight: 6, opacity: 0.6 }} />
          <span className="mc-fe-viewer-filename">{fileName}</span>
          <button className="mc-fe-iconbtn" title={i18nT('apps.fileExplorer.fileViewer.copy_path_2', { path: filePath })} onClick={copyPath} aria-label={i18nT('apps.fileExplorer.fileViewer.copy_path')}>
            <Copy size={11} />
          </button>
        </div>
        <div className="mc-fe-viewer-actions">
          <span className="mc-fe-viewer-meta">{formatBytes(fileMeta.size)}</span>
          {fileMeta.mtime && <span className="mc-fe-viewer-meta"> · {formatTime(fileMeta.mtime)}</span>}
          {fileMeta.truncated && <span style={{ color: 'var(--warn)', fontSize: 11 }}> {i18nT('apps.fileExplorer.fileViewer.truncated')}</span>}
          <button className="mc-fe-iconbtn" title={i18nT('apps.fileExplorer.fileViewer.reload')} onClick={onReload} aria-label={i18nT('apps.fileExplorer.fileViewer.reload')}><RefreshCw size={12} /></button>
          {/* Overflow, not a third peer button: the row caps at two controls, and
              the file-location actions are the ones a user reaches for least
              often. Mirrors the markdown panel's file viewer, whose own ⋯ menu
              holds the same reveal/download pair. */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="mc-fe-iconbtn" title={i18nT('apps.fileExplorer.fileViewer.more_options')} aria-label={i18nT('apps.fileExplorer.fileViewer.more_options')}><MoreHorizontal size={12} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[190px]">
              <DropdownMenuItem onSelect={() => { void revealFile(filePath) }}>
                <ExternalLink size={13} className="shrink-0 text-muted" />
                <span>{revealLabel}</span>
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onDownload}>
                <Download size={13} className="shrink-0 text-muted" />
                <span>{i18nT('apps.fileExplorer.fileViewer.download')}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {isSensitivePath(filePath) && (
        <div style={{ padding: '6px 12px', background: 'color-mix(in srgb, var(--warn) 12%, transparent)', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--warn)' }}>
          <ShieldAlert size={13} /> {i18nT('apps.fileExplorer.fileViewer.sensitive_file_avoid_sharing_your_screen_while_v')}
        </div>
      )}
      <div className="mc-fe-viewer-body">
        {renderViewerBody({ ext, fileMeta, content, openFile: filePath })}
      </div>
    </>
  )
}
