import { memo, useMemo } from 'react'
import { MessageSquare, X, FileDiff, TriangleAlert, CaseSensitive, ChevronUp, ChevronDown } from 'lucide-react'
import { CommentList, type InlineComment } from './CommentOverlay'
import { PierreFilePair, PierrePatch } from '../pierre'
import { i18nT } from '../i18n/t'
import { Badge } from './ui'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import type { useImeGuard } from '../hooks/useImeGuard'
import type { useMarkdownPanelFind } from './useMarkdownPanelFind'

/**
 * The blocks `MarkdownPanel`'s body stacks around the document: the one-time
 * comment hint, the missing-file banner, the empty and unavailable diff
 * notices, the Pierre diff view, the pending-comment overlay and the preview
 * find bar. Presentational: the panel owns every state they draw, and lays
 * them out in both the side-panel and the full-screen layouts.
 */

/** Comment hint banner for markdown files, shown until the reader dismisses it.
 *  `image` names the image gesture, and `isNew` badges the tip when it is back
 *  for a reader who closed the text-only one (see `useCommentHint`). */
export function CommentHint({ image, isNew, onDismiss }: { image: boolean; isNew: boolean; onDismiss: () => void }) {
  return (
    <div
      className="flex items-center gap-2.5 px-3 py-2.5 bg-bg-elevated border rounded-lg text-[12px] animate-scale-in mx-1 mb-2"
      style={{ borderColor: 'color-mix(in srgb, var(--accent) 40%, transparent)' }}
    >
      <span className="text-muted shrink-0"><MessageSquare className="lucide-inline" /></span>
      <span className="flex-1 text-text">
        {isNew && <Badge variant="aim" className="me-1.5 align-middle text-[11px] font-sans">{i18nT('components.markdownPanel.new_badge')}</Badge>}
        <strong className="text-text-strong font-semibold">{i18nT('components.markdownPanel.tip')}</strong> {image
          ? i18nT('components.markdownPanel.select_any_text_or_drag_across_an_image_to_add_i')
          : i18nT('components.markdownPanel.select_any_text_to_add_inline_comments_then_subm')}
      </span>
      <button className="text-accent hover:text-accent-hover cursor-pointer bg-transparent border-none text-[11px] font-medium shrink-0" onClick={onDismiss}>{i18nT('components.markdownPanel.got_it')}</button>
    </div>
  )
}

/** Working-tree diff view (current buffer vs HEAD), Pierre-rendered.
 *
 * View-only: entering edit mode routes to the source editor in
 * `ContentRenderer` (the render sites gate on `editing`), so the diff surface
 * itself never hosts an editable buffer. Text selection inside the rendered
 * diff is ordinary DOM selection, which `SelectionToolbar`'s container
 * listener already picks up — no editor-specific selection bridge needed. */
/** Diff mode with nothing to show: the file matches its baseline, so the diff
 *  canvas would render empty. Say so and offer the way out. */
/** What a tab shows while its file is not on disk: a banner OVER the document,
 *  never in place of it. The buffer underneath may be the only copy left of a
 *  file deleted outside the dashboard, so the last copy stays on screen and the
 *  banner says what it is -- and carries the two ways to keep it, because a
 *  sentence that says "copy it or save it elsewhere" without them leaves the
 *  reader guessing which toolbar icon it means. Both act on the BUFFER: the ⋯
 *  menu's Download fetches the path, which a gone file answers 404, so this
 *  Download hands the browser the text on screen under the file's name, and
 *  Copy content puts it on the clipboard. A PARTIAL buffer -- a read the
 *  gateway cut at 512 000 characters or redacted -- says so instead, and never
 *  calls itself the only remaining version. Same band and the same action
 *  styles as the unsaved-changes banner, so the two conditions the tab can be
 *  in read alike. */
export function MissingFileBanner({ onDownload, onCopy, copied, partial }: { onDownload: () => void; onCopy: () => void; copied: boolean; partial: boolean }) {
  return (
    <div data-testid="markdown-panel-missing-file" role="status" className="flex flex-wrap items-center gap-2 min-h-[40px] px-3 py-2 bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border-b border-[color-mix(in_srgb,var(--warn)_30%,transparent)] shrink-0">
      <TriangleAlert size={13} className="text-warn shrink-0" />
      <span className="flex-1 basis-[12rem] min-w-0 text-[12px] text-text">{i18nT(partial ? 'components.markdownPanel.file_not_found_partial_copy' : 'components.markdownPanel.file_not_found_last_copy')}</span>
      <button className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium text-muted hover:text-text border border-border bg-transparent cursor-pointer transition-colors shrink-0" onClick={onCopy}>{copied ? i18nT('components.markdownPanel.copied') : i18nT('components.markdownPanel.copy_content')}</button>
      <button className="px-3 h-[26px] rounded-md text-[11.5px] font-semibold border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover transition-all shrink-0" onClick={onDownload}>{i18nT('components.markdownPanel.download')}</button>
    </div>
  )
}

export function ZeroDiffNotice({ onExitDiff, message }: { onExitDiff?: () => void; message?: string }) {
  return (
    <div className="h-full flex flex-col items-center justify-center gap-2.5 text-muted px-6 text-center">
      <FileDiff size={20} className="opacity-50" />
      <span className="text-[12.5px]">{message ?? i18nT('components.markdownPanel.no_changes_in_file')}</span>
      {/* The escape hatch leaves diff mode for the full file. A read-only diff
          tab (#9695) is locked to diff mode (toggleDiffMode returns early), so
          the host passes no onExitDiff there and the dead button is hidden —
          the plain file: tab is the editable view of the path instead. */}
      {onExitDiff && (
        <button
          className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium text-muted hover:text-text border border-border bg-transparent cursor-pointer transition-colors"
          onClick={onExitDiff}
        >{i18nT('components.markdownPanel.show_full_file')}</button>
      )}
    </div>
  )
}

export function DiffViewBlock({ diffMode, fileName, originalContent, content, gitPatch, lineNums, wordWrap, collapseUnchanged, flush, sideBySide = true }: {
  diffMode: boolean; fileName: string; originalContent: string; content: string
  /** Git's own unified diff, drawn INSTEAD of comparing the two bodies. Set
   *  when the buffer is only part of the file (see `gitPatch` in the panel). */
  gitPatch?: string | null
  lineNums: boolean; wordWrap: boolean
  /** Fold the unchanged stretches between hunks (panel preference, default off). */
  collapseUnchanged?: boolean
  /** Split vs unified layout — shares the app-wide `mc-diff-split` preference. */
  sideBySide?: boolean
  /** Drop the rounded border box — host surface frames the content. */
  flush?: boolean
}) {
  const oldFile = useMemo(
    () => (originalContent ? { name: fileName, contents: originalContent } : null),
    [fileName, originalContent],
  )
  const newFile = useMemo(
    () => (content ? { name: fileName, contents: content } : null),
    [fileName, content],
  )
  const options = useMemo(
    () => ({
      diffStyle: (sideBySide ? 'split' : 'unified') as 'split' | 'unified',
      disableLineNumbers: !lineNums,
      overflow: (wordWrap ? 'wrap' : 'scroll') as 'wrap' | 'scroll',
      expandUnchanged: !collapseUnchanged,
    }),
    [sideBySide, lineNums, wordWrap, collapseUnchanged],
  )
  if (!diffMode) return null
  return (
    <div className={`w-full h-full overflow-auto pierre-surface ${flush ? '' : 'border border-border rounded-md'}`}>
      {gitPatch != null
        ? <PierrePatch patch={gitPatch} options={options} />
        : <PierreFilePair oldFile={oldFile} newFile={newFile} options={options} />}
    </div>
  )
}

/** Shared comment overlay — the pending-comment list. (The input itself lives
 *  in `SelectionToolbar`'s composer, which opens on selection.) */
export const CommentOverlayBlock = memo(function CommentOverlayBlock({ onSubmitComments, comments, editComment, removeComment, submitAllComments, connected = true, onEditingChange, draftKey }: {
  onSubmitComments?: (message: string) => void; comments: InlineComment[]; editComment: (id: string, text: string) => void; removeComment: (id: string) => void; submitAllComments: (extraPrompt?: string) => void; connected?: boolean
  onEditingChange?: (id: string, editing: boolean) => void
  /** The document the pending instruction belongs to (kept across an unmount). */
  draftKey?: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  if (!onSubmitComments) return null
  return (
    <CommentList comments={comments} onEdit={editComment} onRemove={removeComment} onSubmitAll={submitAllComments} enableExtraPrompt connected={connected} onEditingChange={onEditingChange} draftKey={draftKey} />
  )
})

/** The preview find bar (Cmd+F): the term, case toggle, match count and
 *  steps. State and the highlight painting live in `useMarkdownPanelFind`. */
export function MarkdownPanelFindBar({ find, ime }: { find: ReturnType<typeof useMarkdownPanelFind>; ime: ReturnType<typeof useImeGuard> }) {
  const { findInputRef, findTerm, setFindTerm, findCase, setFindCase, findIdx, findCount, stepFind, closeFind } = find
  return (
    <div data-mc-mdpanel className="absolute top-2 right-3 z-30 flex items-center gap-1.5 bg-bg-elevated border border-border focus-within:border-accent rounded-lg shadow-md px-2.5 py-1.5 text-[13px]">
      <input
        ref={findInputRef}
        type="text"
        value={findTerm}
        onChange={(e) => setFindTerm(e.target.value)}
        {...ime.bindComposition()}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { if (!ime.claimEnter(e)) return; stepFind(e.shiftKey ? -1 : 1) }
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeFind() }
        }}
        placeholder={i18nT('components.markdownPanel.find_in_document')}
        className="bg-transparent border-none outline-hidden text-text placeholder:text-muted w-[170px] text-[13px]"
        aria-label={i18nT('components.markdownPanel.find_in_document_2')}
      />
      <button onClick={() => setFindCase((c) => !c)} className={`p-0.5 rounded cursor-pointer border-none transition-colors ${findCase ? 'bg-accent/20 text-accent' : 'bg-transparent text-muted hover:text-text'}`} title={i18nT('components.markdownPanel.case_sensitive')} aria-label={i18nT('components.markdownPanel.case_sensitive')}><CaseSensitive size={15} /></button>
      {findTerm && <span className="text-muted text-[12px] whitespace-nowrap tabular-nums">{findCount > 0 ? `${findIdx + 1} of ${findCount}` : i18nT('components.markdownPanel.no_results')}</span>}
      <button onClick={() => stepFind(-1)} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.markdownPanel.previous_shift_enter')} aria-label={i18nT('components.markdownPanel.previous_match')}><ChevronUp size={15} /></button>
      <button onClick={() => stepFind(1)} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.markdownPanel.next_enter')} aria-label={i18nT('components.markdownPanel.next_match')}><ChevronDown size={15} /></button>
      <button onClick={closeFind} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.markdownPanel.close_esc')} aria-label={i18nT('components.markdownPanel.close_find')}><X size={15} /></button>
    </div>
  )
}
