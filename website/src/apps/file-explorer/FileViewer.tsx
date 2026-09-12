import { useCallback, useEffect, useRef } from 'react'
import { FileText, Pencil, RefreshCw, Download, Copy, Eye, ExternalLink, FolderOpen, MoreHorizontal, Search, ShieldAlert } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useIsMobile } from '../../hooks/useIsMobile'
import MarkdownRenderer, { BasePathCtx } from '../../components/MarkdownRenderer'
import { EmptyState, Skeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { AUDIO_EXTS, IMAGE_EXTS, LANG_BY_EXT, MARKDOWN_EXTS, OFFICE_EXTS, VIDEO_EXTS } from './constants'
import { extOf, basename, formatBytes, formatTime, isSensitivePath } from './utils'
import { copyToClipboard } from '../../utils/clipboard'
import { revealOrOpen, useRevealFailure, useRevealLabel, useCanOpenFile } from '../../components/FilePathMenu'
import { useBranding } from '../../hooks/useBranding'
import { FindBar, useFindInDocument } from './findInDocument'
import { MarkdownEditor, useMarkdownEditor } from './MarkdownEditor'
import { useFileScrollMemory } from './useFileScrollMemory'
import {
  BinaryFallback, DelimitedViewer, HtmlViewer, ImageViewer, MediaViewer, OfficeViewer, PdfViewer,
} from './viewers'
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

/** Route the open file to its viewer by extension, most specific first. */
function renderViewerBody({ ext, fileMeta, content, openFile }: { ext: string; fileMeta: FileMeta; content: string; openFile: string }) {
  if (IMAGE_EXTS.has(ext)) return <ImageViewer path={openFile} />
  if (ext === '.pdf') return <PdfViewer path={openFile} />
  if (VIDEO_EXTS.has(ext)) return <MediaViewer path={openFile} kind="video" />
  if (AUDIO_EXTS.has(ext)) return <MediaViewer path={openFile} kind="audio" />
  if (OFFICE_EXTS.has(ext)) return <OfficeViewer path={openFile} />
  if (ext === '.html' || ext === '.htm') return <HtmlViewer content={content} />
  if (ext === '.csv') return <DelimitedViewer content={content} delim="," />
  if (ext === '.tsv') return <DelimitedViewer content={content} delim={'\t'} />
  if (fileMeta.binary && fileMeta.encoding !== 'base64') {
    return <BinaryFallback path={openFile} fileMeta={fileMeta} />
  }
  if (MARKDOWN_EXTS.has(ext)) {
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
  // directLocal gates the Open/Reveal pair — they shell out on the gateway, so a
  // remote/tunneled browser sees Download only (matching the shared FilePathMenu
  // and MarkdownPanel's overflow). revealLabel is the shared platform-aware wording.
  const { directLocal } = useBranding()
  // Open uses the shared gate (directLocal + non-Windows); the viewer only shows
  // files, so no kind is passed. Reveal keeps the laxer directLocal-only gate.
  const canOpen = useCanOpenFile()
  const revealLabel = useRevealLabel()
  // A failed open/reveal from the ⋯ menu renders under the viewer bar;
  // askAgent on — the viewer is read-only.
  const reveal = useRevealFailure(filePath ?? undefined)
  const queryClient = useQueryClient()
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const ext = filePath ? extOf(filePath) : ''
  const isMarkdown = MARKDOWN_EXTS.has(ext)
  const editor = useMarkdownEditor(filePath)
  const find = useFindInDocument(bodyRef, `${filePath}:${content?.length ?? 0}`, !editor.editing)
  const onBodyScroll = useFileScrollMemory(bodyRef, filePath, !loading && fileMeta != null)
  const editingRef = useRef(false)
  editingRef.current = editor.editing
  const findOpenRef = useRef(false)
  findOpenRef.current = find.open

  const openFind = useCallback(() => {
    find.setOpen(true)
    setTimeout(() => find.inputRef.current?.select?.(), 30)
  }, [find])

  // ⌘F / Ctrl+F finds within the OPEN document; with no file open the page's
  // own folder-search binding still applies. Capture phase so the page-level
  // handler cannot swallow it first; edit mode leaves the key alone.
  useEffect(() => {
    if (!filePath) return
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === 'f' || e.key === 'F')) {
        if (editingRef.current) return
        if (findOpenRef.current) {
          // Second ⌘F escalates: close find-in-document and let the page's
          // folder-search binding receive the event, so folder search stays
          // reachable while a file is open.
          find.setOpen(false)
          return
        }
        e.preventDefault()
        e.stopPropagation()
        openFind()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [filePath, openFind, find])

  const toggleEdit = useCallback(async () => {
    if (!filePath || !fileMeta) return
    if (!editor.editing) {
      find.setOpen(false)
      editor.start(content || '', fileMeta.mtime || 0, fileMeta.mtime_ns)
      return
    }
    const saved = await editor.finish()
    if (saved) {
      queryClient.invalidateQueries({ queryKey: ['file-explorer', 'read', filePath] })
    }
  }, [filePath, fileMeta, content, editor, find, queryClient])


  if (!filePath) {
    return <EmptyState icon={<FileText size={28} />} title={i18nT('apps.fileExplorer.fileViewer.select_a_file_to_view')} subtitle={isMobile ? undefined : i18nT('apps.fileExplorer.fileViewer.tip_ctrl_cmd_f_to_search')} />
  }
  if (loading) return <Skeleton className="h-full w-full" />
  if (error) {
    // A read failure, not an empty state: the viewer holds nothing to lose.
    return (
      <div className="p-4">
        <ErrorNotice message={error} askAgent />
      </div>
    )
  }
  if (!fileMeta) return null

  const fileName = basename(filePath)
  // Editing requires markdown whose bytes round-trip losslessly. `truncated`
  // alone is not enough: /read serves a BINARY or undecodable .md as empty or
  // replacement-char content, so saving that buffer back would overwrite the
  // original bytes with a lossy rendering of themselves — silent corruption a
  // user hits deterministically just by opening such a file and typing.
  //
  // The encoding label is NORMALISED rather than compared literally: the wire
  // value is spelled both "utf-8" and "utf8" depending on the producer, and
  // demanding one spelling would silently make a perfectly editable file
  // read-only. Anything else (e.g. "none" for a binary blob) stays uneditable.
  const encoding = (fileMeta.encoding ?? 'utf-8').toLowerCase().replace(/[^a-z0-9]/g, '')
  const canEdit = isMarkdown
    && !fileMeta.truncated
    && !fileMeta.binary
    && encoding === 'utf8'
  const copyPath = () => { copyToClipboard(filePath) }

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
          {editor.status && !editor.statusIsWarning && (
            <span data-testid="fe-editor-status" style={{ fontSize: 11, color: 'var(--muted)' }}>{editor.status}</span>
          )}
          <span className="mc-fe-viewer-meta">{formatBytes(fileMeta.size)}</span>
          {fileMeta.mtime && <span className="mc-fe-viewer-meta"> · {formatTime(fileMeta.mtime)}</span>}
          {fileMeta.truncated && <span style={{ color: 'var(--warn)', fontSize: 11 }}> {i18nT('apps.fileExplorer.fileViewer.truncated')}</span>}
          {/* One direct action + overflow keeps the row inside the two-control
              cap: markdown gets its edit toggle (find and reload live in the
              ⋯ menu), everything else gets find-in-document. */}
          {canEdit && (
            <button
              className="mc-fe-iconbtn"
              title={editor.editing ? i18nT('apps.fileExplorer.editor.done_editing') : i18nT('apps.fileExplorer.editor.edit_file_action')}
              onClick={() => { void toggleEdit() }}
              aria-label={editor.editing ? i18nT('apps.fileExplorer.editor.done_editing') : i18nT('apps.fileExplorer.editor.edit_file_action')}
              style={editor.editing ? { color: 'var(--accent)' } : undefined}
            >
              {editor.editing ? <Eye size={12} /> : <Pencil size={12} />}
            </button>
          )}
          {!canEdit && !editor.editing && (
            <button className="mc-fe-iconbtn" title={i18nT('apps.fileExplorer.find.find_in_document')} onClick={openFind} aria-label={i18nT('apps.fileExplorer.find.find_in_document')}><Search size={12} /></button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="mc-fe-iconbtn" title={i18nT('apps.fileExplorer.fileViewer.more_options')} aria-label={i18nT('apps.fileExplorer.fileViewer.more_options')}><MoreHorizontal size={12} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[190px]">
              {canEdit && !editor.editing && (
                <DropdownMenuItem onSelect={openFind}>
                  <Search size={13} className="shrink-0 text-muted" />
                  <span>{i18nT('apps.fileExplorer.find.find_in_document')}</span>
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onSelect={onReload}>
                <RefreshCw size={13} className="shrink-0 text-muted" />
                <span>{i18nT('apps.fileExplorer.fileViewer.reload')}</span>
              </DropdownMenuItem>
              {/* Open uses the shared canOpen gate (directLocal + non-Windows);
                  Reveal uses directLocal alone. A remote session gets Download
                  only, so it never sees a "reveal" that would open Finder on a
                  host it is not looking at. Failures funnel through the shared
                  i18n path — this replaced this PR's own revealFile(), which
                  reported failures with alert(). */}
              {canOpen && (
                <DropdownMenuItem onSelect={() => { void revealOrOpen(filePath, 'open', reveal) }}>
                  <ExternalLink size={13} className="shrink-0 text-muted" />
                  <span>{i18nT('components.markdownPanel.open_with_default_app')}</span>
                </DropdownMenuItem>
              )}
              {directLocal && (
                <DropdownMenuItem onSelect={() => { void revealOrOpen(filePath, 'reveal', reveal) }}>
                  <FolderOpen size={13} className="shrink-0 text-muted" />
                  <span>{revealLabel}</span>
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onSelect={onDownload}>
                <Download size={13} className="shrink-0 text-muted" />
                <span>{i18nT('apps.fileExplorer.fileViewer.download')}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {/* A SAVE failure is an error, not a status line: it renders through the
          shared ErrorNotice so it carries the same structure as every other
          failure in the dashboard (AUTOSDE `errors-use-error-notice`).
          askAgent is deliberately OFF here — the unsaved buffer is the user's
          own in-progress text, and handing a draft to an agent is not a
          recovery step; the editor already parks it for recovery instead. */}
      {editor.status && editor.statusIsWarning && (
        <div style={{ padding: '6px 12px', borderBottom: '1px solid var(--border)' }}>
          <ErrorNotice variant="inline" className="whitespace-normal" message={editor.status} testId="fe-editor-error" />
        </div>
      )}
      {reveal.error && (
        <div style={{ padding: '6px 12px', borderBottom: '1px solid var(--border)' }}>
          <ErrorNotice variant="inline" className="whitespace-normal" message={reveal.error} askAgent onDismiss={reveal.clear} testId="file-viewer-reveal-error" />
        </div>
      )}
      {!editor.editing && <FindBar find={find} fileName={fileName} />}
      {isSensitivePath(filePath) && (
        <div style={{ padding: '6px 12px', background: 'color-mix(in srgb, var(--warn) 12%, transparent)', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--warn)' }}>
          <ShieldAlert size={13} /> {i18nT('apps.fileExplorer.fileViewer.sensitive_file_avoid_sharing_your_screen_while_v')}
        </div>
      )}
      <div className="mc-fe-viewer-body" ref={bodyRef} onScroll={onBodyScroll}>
        {editor.editing
          ? <MarkdownEditor editor={editor} fileName={fileName} />
          : renderViewerBody({ ext, fileMeta, content, openFile: filePath })}
      </div>
    </>
  )
}
