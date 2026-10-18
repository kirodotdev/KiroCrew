/**
 * The artifact page's sticky header: the title and its inline rename, the type
 * select, the folder, star and tag chips, the version picker, the Save/Cancel,
 * Revert/Edit and companion-chat buttons, and the More menu. The menu's state
 * (copy, more-open, send to a session, download) is `useArtifactDetailPageMenu`,
 * which the facade calls in the page's hook order
 * (`ArtifactDetailPage.owners.test.tsx` pins it).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowLeft, Camera, Check, Copy, ExternalLink, Download, Eye, FoldHorizontal, MoreHorizontal, Pencil,
  UnfoldHorizontal, RefreshCw, X, RotateCcw, Plus, Sparkles, MessageSquare, Monitor, Undo2, Upload, Star,
  Folder as FolderIcon,
} from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { PageHeader, Badge, Btn, Input } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '../components/ui/dropdown-menu'
import { IS_MAC } from '../hooks/useKeyboardShortcuts'
import HoverTip from '../components/HoverTip'
import { ArtifactSendToSessionSubmenu, useArtifactSendToSession } from '../components/ArtifactSendToSession'
import { useArtifactFolders, useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import { folderBreadcrumb } from '../utils/artifactFolderTree'
// Artifact body renderers, extracted here so the chat side panel shares them.
import { artifactAssetUrl } from '../components/ArtifactBody'
import { useArtifactPopouts } from '../hooks/useArtifactPopouts'
import { USER_SELECTABLE_KINDS } from '../lib/artifactKinds'
import type { Artifact } from '../types'

import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import { errMessage } from '../utils/thunkError'
import ErrorNotice from '../components/ErrorNotice'
import type {
  ArtifactDetailPageStateRead, ArtifactDetailPageDocumentRead,
} from './useArtifactDetailPageDocument'
import type { ArtifactDetailPageEditorRead } from './useArtifactDetailPageEditor'
import type { ArtifactDetailPageComposerRead } from './useArtifactDetailPageComments'
import type { ArtifactDetailPageCompanionRead } from './useArtifactDetailPageCompanion'

/** Puts the header's dropdowns on the same 32px line as the buttons beside
 *  them; the select's padding alone stands it taller. A height, not a padding
 *  override: the trigger owns its spacing and typography. */
const TOOLBAR_SELECT_CLASS = 'h-8'

/** Size and shape shared by the header's folder / star / tag chips, so they
 *  sit on the same 32px line as Back and the kind select. Colour and border
 *  style stay per chip. */
const TOOLBAR_CHIP_CLASS = 'inline-flex items-center gap-1.5 h-8 px-2.5 text-[12px] rounded-md box-border'

/**
 * Header folder chip: shows where the artifact is filed and opens
 * a picker to move it (metadata-only — no version bump). Mirrors the tag-chip
 * row's inline-mutation pattern.
 */
// The More menu's comment count. Its own component so the item's registered
// label (artifacts.detail.comments) reads as the Show / Hide comments text alone.
function CommentCountBadge({ count }: { count: number }) {
  if (count <= 0) return null
  return <span className="ml-auto px-1 rounded bg-accent/20 text-[10px]">{count}</span>
}

function FolderChip({ artifact }: { artifact: Artifact }) {
  const { folders } = useArtifactFolders()
  const moveArtifact = useMoveArtifactToFolder()
  const chain = folderBreadcrumb(folders, artifact.folder_id || '')
  const current = chain.length ? chain[chain.length - 1] : null
  const path = chain.map(f => f.name).join(' › ')
  return (
    <DropdownMenu>
      <HoverTip label={current ? i18nT('pages.artifactDetailPage.filed_in_click_to_move', { path }) : i18nT('pages.artifactDetailPage.not_in_a_folder_click_to_file')}>
      <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={`${TOOLBAR_CHIP_CLASS} border cursor-pointer bg-bg-elevated transition-colors ${
              current ? 'border-border text-muted hover:text-text' : 'border-dashed border-border text-muted hover:text-text hover:border-border-strong'
            }`}
            aria-label={current ? i18nT('pages.artifactDetailPage.folder_move_to_folder', { path }) : i18nT('pages.artifactDetailPage.move_to_folder')}
            {...uiLocation('artifacts.detail.move-to-folder')}
          >
            <FolderIcon size={13} className={current ? 'text-accent' : undefined} />
            {current ? current.name : 'folder'}
          </button>
      </DropdownMenuTrigger>
      </HoverTip>
      {/* Intentional tighter cap composed via min() with the primitive's
          available-height var: 300px keeps the folder picker compact while
          preserving the viewport never-clip floor (a bare max-h would override
          the primitive, since cn()'s tailwind-merge dedupes max-h-*). overflow
          is left to the primitive. */}
      <DropdownMenuContent align="start" className="min-w-[190px] max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
        <FolderPickerItems
          folders={folders}
          currentFolderId={artifact.folder_id || null}
          onPick={(fid) => moveArtifact(artifact.slug, fid || '')}
          Item={DropdownMenuItem}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * The pop-out control in the artifact detail toolbar. Opens the artifact in its
 * own browser window and, once it's out, swaps to Focus + Bring-back (mirrors
 * the chat session popout menu). Kept as a child so the `useArtifactPopouts`
 * subscription only runs on the main dashboard — never inside the popout window
 * itself (where this control isn't rendered).
 */
/**
 * Pop-out entries of the toolbar's overflow menu: "Pop out to window" while the
 * artifact is here, or Focus + Bring back once it lives in its own window. Not
 * rendered inside the popout window itself (the frame's Return button closes it).
 */
function ArtifactPopoutMenuItems({ slug, name }: { slug: string; name: string }) {
  const { isPoppedOut, open, focus, bringBack } = useArtifactPopouts()
  if (isPoppedOut(slug)) {
    return (
      <>
        <DropdownMenuItem onSelect={() => focus(slug)}>
          <Monitor size={13} className="shrink-0 text-accent" aria-hidden="true" />
          <span>{i18nT('pages.artifactDetailPage.focus_popped_out_window')}</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => bringBack(slug)}>
          <Undo2 size={13} className="shrink-0 text-muted" aria-hidden="true" />
          <span>{i18nT('pages.artifactDetailPage.bring_artifact_back_to_this_window')}</span>
        </DropdownMenuItem>
      </>
    )
  }
  return (
    <DropdownMenuItem onSelect={() => open(slug, name)}>
      <ExternalLink size={13} className="shrink-0 text-muted" aria-hidden="true" />
      <span>{i18nT('pages.artifactDetailPage.pop_out_to_window')}</span>
    </DropdownMenuItem>
  )
}

/** The More menu's state: copy, open, send to a session and download. */
export function useArtifactDetailPageMenu({
  stateRead, documentRead, composerRead, companionRead,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  composerRead: ArtifactDetailPageComposerRead
  companionRead: ArtifactDetailPageCompanionRead
}) {
  const { slug, editing, setSendToSessionError } = stateRead
  const { effectiveVersion, artifact, exportSrcdoc } = documentRead
  const { guardSendToSession } = composerRead
  const { sendNav } = companionRead
  // ── Copy raw content ──────────────────────────────────────────────────────
  // Copies the stored source (markdown/HTML/JSON/text as-is) of the version
  // currently on screen — `artifact` already resolves to the selected
  // snapshot, so a historical view copies that snapshot's content. The menu
  // item swaps to a check and "Copied" for a moment as the success
  // confirmation (the same pattern chat messages and diff blocks use).
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  // A failure is held as a page notice until dismissed or the next copy.
  const [copyError, setCopyError] = useState<string | null>(null)
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const copyAttemptRef = useRef(0)
  // The route reuses this page across artifacts, so a failure banner from one
  // artifact must not stay up over the next.
  useEffect(() => {
    setCopyError(null)
    setSendToSessionError(null)
  }, [slug, setSendToSessionError])
  useEffect(() => () => {
    copyAttemptRef.current += 1
    if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current)
  }, [])
  const handleCopyContent = useCallback(() => {
    const attempt = ++copyAttemptRef.current
    if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current)
    setCopyStatus('idle')
    setCopyError(null)
    // `copyToClipboard` resolves a boolean and never rejects: `true` only once
    // the text actually reached the clipboard. Gate the confirmation on it so a
    // `false` shows the failure glyph instead of a tick over an unchanged
    // clipboard. (A `.catch` here would be unreachable dead code.)
    copyToClipboard(artifact?.content ?? '')
      .then((ok) => {
        if (attempt !== copyAttemptRef.current) return
        setCopyStatus(ok ? 'copied' : 'failed')
        if (!ok) setCopyError(i18nT('pages.artifactDetailPage.copy_failed'))
        copiedTimerRef.current = setTimeout(() => {
          if (attempt === copyAttemptRef.current) setCopyStatus('idle')
        }, 1500)
      })
  }, [artifact])
  // Success is confirmed on the menu item itself (Copied); a failure is reported
  // only through the page's ErrorNotice stack (`copyError`).
  const copyLabel = copyStatus === 'copied'
    ? i18nT('pages.artifactDetailPage.copied')
    : i18nT('pages.artifactDetailPage.copy_content')
  // The toolbar's More menu. A copy keeps it open so the item's Copied label is
  // the confirmation; a failed copy closes it, so the page's ErrorNotice — the
  // one place the failure is reported — is on screen.
  const [moreOpen, setMoreOpen] = useState(false)
  useEffect(() => { if (copyStatus === 'failed') setMoreOpen(false) }, [copyStatus])

  const sendToSession = useArtifactSendToSession({
    name: artifact?.name ?? '',
    slug,
    active: !editing,
    onSend: sendNav,
    beforeSend: guardSendToSession,
    onError: setSendToSessionError,
  })

  const downloadAsHtml = () => {
    if (!artifact) return
    // Image artifacts carry no text content — their bytes live behind the asset
    // endpoint. Blobbing `artifact.content` here would hand the user an empty
    // `.html` file from the toolbar's habituated download spot.
    if (artifact.kind === 'image') {
      const a = document.createElement('a')
      a.href = artifactAssetUrl(artifact.slug)
      a.download = artifact.image?.original_filename
        || `${artifact.slug}.${artifact.image?.ext || 'png'}`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      return
    }
    const isMarkdownLike = artifact.kind === 'markdown' || artifact.kind === 'text' || artifact.kind === 'json' || artifact.kind === 'svg'
    const blobBody = exportSrcdoc ?? artifact.content ?? ''
    const mime = isMarkdownLike
      ? (artifact.kind === 'json' ? 'application/json' : artifact.kind === 'svg' ? 'image/svg+xml' : 'text/plain')
      : 'text/html'
    const ext = artifact.kind === 'markdown' ? 'md'
      : artifact.kind === 'json' ? 'json'
      : artifact.kind === 'svg' ? 'svg'
      : artifact.kind === 'text' ? 'txt'
      : 'html'
    const blob = new Blob([blobBody], { type: mime })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    const safeName = artifact.name.replace(/[^a-zA-Z0-9-_ ]/g, '')
    a.download = `${safeName || artifact.slug}-v${effectiveVersion}.${ext}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(a.href), 60_000)
  }

  return {
    copyStatus, copyError, setCopyError, handleCopyContent, copyLabel, moreOpen, setMoreOpen, sendToSession,
    downloadAsHtml,
  }
}

export type ArtifactDetailPageMenuRead = ReturnType<typeof useArtifactDetailPageMenu>

/** The sticky header: title, chips, version picker, row actions and the More menu. */
export function ArtifactDetailPageHeader({
  stateRead, documentRead, editorRead, composerRead, menuRead, artifact, displayCommentCount, panel,
  toggleSidebar, handleRevert, openCompanionChat, popout,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  editorRead: ArtifactDetailPageEditorRead
  composerRead: ArtifactDetailPageComposerRead
  menuRead: ArtifactDetailPageMenuRead
  artifact: Artifact
  displayCommentCount: number
  panel: 'none' | 'comments' | 'chat'
  toggleSidebar: () => void
  handleRevert: () => Promise<void>
  openCompanionChat: (opts?: { address?: boolean }) => Promise<void>
  popout: boolean
}) {
  const {
    slug, navigate, confirm, renaming, setRenaming, nameDraft, setNameDraft, selectedVersion,
    setSelectedVersion, editing, previewDuringEdit, setPreviewDuringEdit, readingWidth, toggleReadingWidth,
    saving, saveConflict, showPublish, setShowPublish, addingTag, setAddingTag, newTag, setNewTag, ime,
  } = stateRead
  const { versionsQuery, versions, isCurrent, editable, dirty, usesIframe } = documentRead
  const {
    addTag, removeTag, pinning, togglePin, changingKind, changeKind, titleButtonRef, startRenaming,
    commitRename, startEditing, endEditing, cancelEditing, handleSave, handleSnapshotLive,
  } = editorRead
  const { guardCommentDraft } = composerRead
  const {
    copyStatus, handleCopyContent, copyLabel, moreOpen, setMoreOpen, sendToSession, downloadAsHtml,
  } = menuRead
  // Version-picker rows as two PARALLEL arrays, newest snapshot first, the order
  // the <option> list had. `live` leads as a static entry: Live is always-current
  // state, distinct from any numbered snapshot because in the explicit-snapshot
  // model saves update Live without bumping versions, so Live can be ahead of the
  // latest numbered snapshot.
  const versionsDesc = versions.slice().reverse()
  const versionOptions = ['live', ...versionsDesc.map(String)]
  const versionOptionLabels = [
    i18nT('pages.artifactDetailPage.live'),
    ...versionsDesc.map((v) => `${i18nT('pages.artifactDetailPage.v')}${v}`),
  ]
  return (
    <div className="sticky top-0 z-10 bg-bg border-b border-border">
      <PageHeader
        title={renaming ? (
          <Input
            autoFocus
            value={nameDraft}
            aria-label={i18nT('pages.artifactDetailPage.artifact_name')}
            onChange={(e) => setNameDraft(e.target.value)}
            {...ime.bindEnter({
              onEnter: () => void commitRename(),
              onEscape: () => setRenaming(false),
              onBlur: () => void commitRename(),
            })}
            className="px-2 py-0.5 text-2xl font-bold tracking-tight text-text-strong w-full max-w-[36rem]"
          />
        ) : (
          <HoverTip label={i18nT('pages.artifactDetailPage.rename_this_artifact')}>
            <Btn
              ref={titleButtonRef}
              onClick={startRenaming}
              className="group gap-2 bg-transparent border-none p-0 text-2xl font-bold tracking-tight text-text-strong cursor-text hover:bg-transparent hover:border-none"
            >
              {artifact.name}
              <Pencil size={14} className="text-muted opacity-0 group-hover:opacity-100 transition-opacity shrink-0" aria-hidden="true" />
            </Btn>
          </HoverTip>
        )}
        subtitle={i18nT('pages.artifactDetailPage.artifact_slug', { slug: artifact.slug })}
      />
      <div className="px-4 md:px-6 py-2 flex flex-wrap items-center gap-2">
        {!popout && (
          <Btn onClick={async () => {
            if (dirty && !(await confirm({
              title: i18nT('pages.artifactDetailPage.discard_unsaved_changes'),
              confirmLabel: i18nT('pages.artifactDetailPage.discard_changes_button'),
            }))) return
            navigate('/artifacts')
          }} className="flex items-center gap-1 h-8">
            <ArrowLeft size={13} /> {i18nT('pages.artifactDetailPage.back')}
          </Btn>
        )}
        {/* Type control. Markdown and plain text are indistinguishable by
          * content — "# Notes" is a heading in one and literal text in the
          * other — so the difference is intent and has to be stated, not
          * sniffed. Offered only for the inline-editable kinds; widget / html
          * render in a sandboxed iframe with no editor, so switching to one
          * would strand a document the user is typing in. Choosing a type also
          * PINS it, stopping the auto-detect from re-typing it later. */}
        {editable ? (
          <HoverTip label={i18nT('pages.artifactDetailPage.change_how_this_document_is_rendered')}>
            <SimpleSelect
              options={USER_SELECTABLE_KINDS}
              value={artifact.kind}
              aria-label={i18nT('pages.artifactDetailPage.document_type')}
              className={TOOLBAR_SELECT_CLASS}
              disabled={changingKind}
              onChange={(v) => void changeKind(v)}
            />
          </HoverTip>
        ) : (
          <Badge variant="aim">{artifact.kind}</Badge>
        )}
        <FolderChip artifact={artifact} />
        {/* Only on the current version: a version snapshot does not carry the
          * live record-level `pinned`, so rendering it there could show a
          * stale star for state the user cannot meaningfully toggle. */}
        {isCurrent && (
          <HoverTip label={artifact.pinned ? i18nT('pages.artifactsPage.starred_click_to_unstar') : i18nT('pages.artifactsPage.star_artifact')}>
            <button
              type="button"
              onClick={togglePin}
              disabled={pinning}
              className={`${TOOLBAR_CHIP_CLASS} border transition-colors cursor-pointer disabled:cursor-default ${
                artifact.pinned
                  ? 'bg-accent/10 border-accent text-accent'
                  : 'bg-bg-elevated border-border text-muted hover:text-accent hover:border-accent'
              }`}
              aria-label={artifact.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_artifact')}
              aria-pressed={!!artifact.pinned}
            >
              {pinning
                ? <RefreshCw size={13} className="animate-spin" />
                : <Star size={13} className={artifact.pinned ? 'fill-current' : ''} />}
              {artifact.pinned ? i18nT('pages.artifactsPage.starred') : i18nT('pages.artifactDetailPage.star')}
            </button>
          </HoverTip>
        )}
        {artifact.tags.map((t) => (
          <span key={t} className={`${TOOLBAR_CHIP_CLASS} bg-bg-elevated border border-border text-muted group`}>
            {/* Own element: the remove button's HoverTip keeps an sr-only copy of
                its label in this chip, so the bare text node would no longer be
                the chip's whole text. */}
            <span>{t}</span>
            <HoverTip label={i18nT('pages.artifactDetailPage.remove_tag', { name: t })}>
              <button
                type="button"
                onClick={() => removeTag(t)}
                className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 hover:text-danger transition-opacity bg-transparent border-none cursor-pointer p-0 inline-flex items-center"
                aria-label={i18nT('pages.artifactDetailPage.remove_tag', { name: t })}
              >
                <X size={12} />
              </button>
            </HoverTip>
          </span>
        ))}
        {addingTag ? (
          <input
            type="text"
            value={newTag}
            onChange={e => setNewTag(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                // Rule 2 shape: this handler carries more than Enter (comma/space
                // also commit), so only the Enter path is gated. Rule 1: single-line
                // input, so the guard alone is enough.
                if (ime.isComposing(e)) return
                addTag(newTag)
              }
              if (e.key === ',' || e.key === ' ') {
                // Space cycles IME candidates mid-composition; committing here
                // would post the composing buffer and kill candidate selection.
                // The claim is key-agnostic: it consumes the separator only
                // when the IME is not itself using the keypress.
                if (ime.claimEnter(e) && newTag.trim()) addTag(newTag)
              }
              if (e.key === 'Escape') { ime.reset(); setNewTag(''); setAddingTag(false) }
            }}
            {...ime.bindComposition({
              onBlur: () => {
                if (newTag.trim()) addTag(newTag)
                else setAddingTag(false)
              },
            })}
            autoFocus
            placeholder={i18nT('pages.artifactDetailPage.tag')}
            className={`${TOOLBAR_CHIP_CLASS} bg-bg-elevated border border-accent text-text outline-hidden focus-ring`}
            style={{ width: '90px' }}
            aria-label={i18nT('pages.artifactDetailPage.add_a_tag')}
          />
        ) : (
          <HoverTip label={i18nT('pages.artifactDetailPage.add_a_tag_comma_separated_tags_supported')}>
            <button
              type="button"
              onClick={() => setAddingTag(true)}
              className={`${TOOLBAR_CHIP_CLASS} border border-dashed border-border text-muted hover:text-text hover:border-border-strong cursor-pointer bg-transparent transition-colors`}
              aria-label={i18nT('pages.artifactDetailPage.add_a_tag')}
            >
              <Plus size={13} /> {i18nT('pages.artifactDetailPage.tag_2')}
            </button>
          </HoverTip>
        )}
        {/* `flex-wrap`: the parent row wraps, but this group did not, so at a
            narrow window the trailing controls (Download last) ran past the
            viewport edge and became unreachable. Wrapping keeps every action
            on screen; `justify-end` keeps the group right-aligned when it
            spills onto a second line. */}
        <span className="mc-art-toolbar ml-auto flex flex-wrap items-center justify-end gap-2 text-[13px] text-muted">
          <span>{i18nT('pages.artifactDetailPage.version')}</span>
          <SimpleSelect
            // Named so it is distinguishable from the document-type control
            // beside it -- both for assistive tech and for tests.
            aria-label={i18nT('pages.artifactDetailPage.version')}
            {...uiLocation('artifacts.detail.versions')}
            className={TOOLBAR_SELECT_CLASS}
            disabled={saving}
            options={versionOptions}
            optionLabels={versionOptionLabels}
            value={selectedVersion === null ? 'live' : String(selectedVersion)}
            onChange={async (raw) => {
              if (dirty && !(await confirm({
                title: i18nT('pages.artifactDetailPage.discard_unsaved_changes'),
                confirmLabel: i18nT('pages.artifactDetailPage.discard_changes_button'),
              }))) return
              // A typed comment draft is lost with the toolbar the switch unmounts.
              await guardCommentDraft(() => {
                endEditing()
                if (raw === 'live') {
                  setSelectedVersion(null)
                } else {
                  setSelectedVersion(parseInt(raw, 10))
                }
              })
            }}
          />
          {/* No hand-off: editor buffer editedContent may be dirty */}
          <ErrorNotice
            variant="inline"
            title={i18nT('pages.artifactDetailPage.versions_failed_to_load')}
            message={versionsQuery.error ? (errMessage(versionsQuery.error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
          />

          {/* At most two actions sit in the row: Save + Cancel while editing,
              otherwise Revert (historical version) or Edit, plus the
              companion chat. Everything else lives in the labelled "More"
              menu (`max-two-buttons-per-row`). */}
          {editing ? (
            <>
              <HoverTip label={saveConflict
                ? i18nT('pages.artifactDetailPage.save_overwrite_newer_content_title')
                : i18nT('pages.artifactDetailPage.save_to_live_cmd_s_updates_the_live_state_withou')}>
                <button
                  type="button"
                  onClick={() => handleSave(false, saveConflict)}
                  disabled={!dirty || saving}
                  className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`}
                >
                  {saving
                    ? i18nT('pages.artifactDetailPage.saving')
                    : saveConflict
                      ? i18nT('pages.artifactDetailPage.save_overwrite_newer_content')
                      : i18nT('pages.artifactDetailPage.save')}
                </button>
              </HoverTip>
              <HoverTip label={i18nT('pages.artifactDetailPage.cancel_esc')}>
                <button
                  type="button"
                  onClick={cancelEditing}
                  disabled={saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                >
                  <span className="inline-flex items-center gap-1"><X size={13} /> {i18nT('pages.artifactDetailPage.cancel')}</span>
                </button>
              </HoverTip>
            </>
          ) : (
            <>
              {!isCurrent ? (
                <HoverTip label={i18nT('pages.artifactDetailPage.revert_to_v', { version: selectedVersion })}>
                  <button
                    type="button"
                    onClick={handleRevert}
                    disabled={saving}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-warn/40 text-warn hover:border-warn cursor-pointer transition-all disabled:opacity-40"
                    aria-label={i18nT('pages.artifactDetailPage.revert_to_v', { version: selectedVersion })}
                  >
                    <span className="inline-flex items-center gap-1"><RotateCcw size={13} /> {i18nT('pages.artifactDetailPage.revert')}</span>
                  </button>
                </HoverTip>
              ) : editable && (
                <HoverTip label={i18nT('pages.artifactDetailPage.edit_content')}>
                  <button
                    type="button"
                    onClick={() => { void guardCommentDraft(startEditing) }}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
                    aria-label={i18nT('pages.artifactDetailPage.edit_content')}
                  >
                    <Pencil size={13} />
                  </button>
                </HoverTip>
              )}
              {/* Companion chat — toggles the embedded chat panel. Primary
                  "discuss with agent" action for all kinds; for widgets it is
                  the only way to ask the agent to change the artifact.
                  Comments are durable and read by the agent via
                  artifact_get_comments. Works in popout windows too — the
                  popout has its own store + WS. */}
              <HoverTip label={i18nT('pages.artifactDetailPage.chat_with_the_agent_about_this_artifact')}>
                <button
                  type="button"
                  onClick={() => { void openCompanionChat() }}
                  className={`p-1.5 rounded-md border cursor-pointer transition-all ${panel === 'chat' ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  aria-label={i18nT('pages.artifactDetailPage.toggle_agent_chat')}
                  aria-pressed={panel === 'chat'}
                >
                  <Sparkles size={13} />
                </button>
              </HoverTip>
            </>
          )}

          <DropdownMenu open={moreOpen} onOpenChange={setMoreOpen} guideScope="menu:artifacts.detail.more">
            <HoverTip label={i18nT('pages.artifactDetailPage.more_actions')}>
              <DropdownMenuTrigger asChild>
                <Btn
                  type="button"
                  aria-label={i18nT('pages.artifactDetailPage.more_actions')}
                  {...uiLocation('artifacts.detail.more')}
                >
                  <MoreHorizontal size={13} aria-hidden="true" /> {i18nT('pages.artifactDetailPage.more')}
                </Btn>
              </DropdownMenuTrigger>
            </HoverTip>
            <DropdownMenuContent align="end" className="min-w-[220px]">
              {editing && (
                <>
                  <DropdownMenuItem disabled={!dirty || saving} onSelect={() => handleSave(true)}>
                    <Camera size={13} className="shrink-0 text-muted" aria-hidden="true" />
                    <span>{i18nT('pages.artifactDetailPage.snapshot')}</span>
                    <kbd className="ml-auto pl-3 font-sans text-[11px] text-muted" aria-hidden="true">{IS_MAC ? '⌘⇧S' : 'Ctrl+Shift+S'}</kbd>
                  </DropdownMenuItem>
                  {artifact.kind !== 'svg' && (
                    <DropdownMenuItem disabled={saving} onSelect={() => setPreviewDuringEdit(p => !p)}>
                      {previewDuringEdit
                        ? <Pencil size={13} className="shrink-0 text-muted" aria-hidden="true" />
                        : <Eye size={13} className="shrink-0 text-muted" aria-hidden="true" />}
                      <span>{previewDuringEdit ? i18nT('pages.artifactDetailPage.back_to_editor') : i18nT('pages.artifactDetailPage.preview')}</span>
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuSeparator />
                </>
              )}
              {!editing && isCurrent && artifact.live_dirty && (
                <DropdownMenuItem disabled={saving} onSelect={handleSnapshotLive}>
                  <Camera size={13} className="shrink-0 text-muted" aria-hidden="true" />
                  <span>{i18nT('pages.artifactDetailPage.snapshot')}</span>
                </DropdownMenuItem>
              )}
              {(!editing || previewDuringEdit) && !usesIframe && (
                <DropdownMenuItem role="menuitemcheckbox" aria-checked={readingWidth === 'full'} onSelect={(e) => { e.preventDefault(); toggleReadingWidth() }}>
                  {readingWidth === 'full'
                    ? <FoldHorizontal size={13} className="shrink-0 text-muted" aria-hidden="true" />
                    : <UnfoldHorizontal size={13} className="shrink-0 text-muted" aria-hidden="true" />}
                  <span>{i18nT('components.readingWidthToggle.title_full')}</span>
                  {readingWidth === 'full' && <Check size={13} className="ml-auto shrink-0 text-accent" aria-hidden="true" />}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onSelect={toggleSidebar} {...uiLocation('artifacts.detail.comments')}>
                <MessageSquare size={13} className={`shrink-0 ${panel === 'comments' ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
                <span>{panel === 'comments' ? i18nT('pages.artifactDetailPage.hide_comments') : i18nT('pages.artifactDetailPage.show_comments')}</span>
                <CommentCountBadge count={displayCommentCount} />
              </DropdownMenuItem>
              {!popout && <ArtifactPopoutMenuItems slug={slug} name={artifact.name} />}
              {/* Copy raw source + hand a reference to a chat session. Copy is
                  hidden for image (bytes, not text) and webapp (its deploy card
                  has its own affordances); both hide while the editor owns the
                  surface, since leaving for a chat would discard the buffer.
                  Copy keeps the menu open so its Copied / failed label is the
                  confirmation. */}
              {!editing && (
                <>
                  <DropdownMenuSeparator />
                  {artifact.kind !== 'webapp' && artifact.kind !== 'image' && (
                    <DropdownMenuItem onSelect={(e) => { e.preventDefault(); handleCopyContent() }}>
                      {copyStatus === 'copied'
                        ? <Check size={13} className="shrink-0 text-ok" aria-hidden="true" />
                        : <Copy size={13} className="shrink-0 text-muted" aria-hidden="true" />}
                      <span aria-live="polite">{copyLabel}</span>
                    </DropdownMenuItem>
                  )}
                  <ArtifactSendToSessionSubmenu state={sendToSession} />
                </>
              )}
              <DropdownMenuSeparator />
              {/* Publish — the single publish surface. Web deploy (Publish to
                  public web on the user's own AWS) and any future publish
                  providers register into PublishHub, so this is the one and
                  only publish action. Shown for non-webapp kinds; webapp
                  artifacts use their own deploy card. NOTE: the internal
                  share/publish-provider surface (Link2 + ArtifactSharePanel) is
                  intentionally absent here — a deliberate public-edition
                  divergence, so an upstream sync must NOT re-add it. */}
              {artifact.kind !== 'webapp' && artifact.kind !== 'image' && (
                <DropdownMenuItem role="menuitemcheckbox" aria-checked={showPublish} onSelect={() => setShowPublish(v => !v)}>
                  <Upload size={13} className={`shrink-0 ${showPublish ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
                  <span>{i18nT('pages.artifactDetailPage.publish')}</span>
                  {showPublish && <Check size={13} className="ml-auto shrink-0 text-accent" aria-hidden="true" />}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onSelect={downloadAsHtml}>
                <Download size={13} className="shrink-0 text-muted" aria-hidden="true" />
                <span>{i18nT('pages.artifactDetailPage.download')}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </span>
      </div>
    </div>
  )
}
