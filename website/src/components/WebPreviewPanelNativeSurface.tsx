import type { Dispatch, RefObject, SetStateAction } from 'react'
import { ExternalLink, Loader2, Monitor, MousePointerClick, Pencil, Plus, X } from 'lucide-react'

import ErrorNotice from './ErrorNotice'
import { describeAnnotationTarget } from '../utils/browserAnnotations'
import type { useNativeBrowser } from '../hooks/useNativeBrowser'
import { annotateRoleNames, type useWebPreviewPanelAnnotate } from './useWebPreviewPanelAnnotate'

import { i18nT } from '../i18n/t'

// Native browse surface — the counterpart to `liveMirror` for the case where
// the browser runs in THIS process. The page is a real Chromium view the main
// process composites over `hostRef`'s rectangle, so the div below is
// deliberately empty: it exists to be measured, not painted. Nothing may be
// rendered inside it, because the native layer would cover it.
export default function WebPreviewPanelNativeSurface({ native, annotate, draft, setDraft, commitNative, nativeInputRef }: {
  native: ReturnType<typeof useNativeBrowser>
  annotate: ReturnType<typeof useWebPreviewPanelAnnotate>
  draft: string
  setDraft: Dispatch<SetStateAction<string>>
  commitNative: (raw: string) => void
  nativeInputRef: RefObject<HTMLInputElement>
}) {
  const {
    canAnnotate, toggleAnnotate, annotateBusy, picking, annotateError, annotateErrorAskAgent, setAnnotateError,
    annotations, clearAnnotations, clearArmed, sendAnnotations, canSend, addedAck, editing, noteInputRef,
    setEditingText, commitNote, cancelNote, removeAnnotation, editAnnotation,
  } = annotate
  return (
    <div className="absolute inset-0 z-10 flex flex-col h-full min-h-0 bg-bg">
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0" style={{ backgroundColor: 'var(--bg-elevated)' }}>
        <Monitor size={14} className="shrink-0 text-muted" />
        <span className="shrink-0 text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.browser_live')}</span>
        <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ backgroundColor: 'var(--ok)' }} aria-hidden />
        <div className="flex-1" />
        {/* Annotate: pick elements on the page and type a note on each. Lives
            on the native header (which had no actions); the preview toolbar's
            4-control row is untouched. Labelled, not icon-only: the verb is new
            to users. While picking, the same button reads "Done" -- one control,
            two states, so there is always a visible way out besides Esc. */}
        {canAnnotate && (
          <button
            type="button"
            onClick={() => { void toggleAnnotate() }}
            disabled={annotateBusy}
            aria-pressed={picking}
            className={`inline-flex items-center gap-1.5 h-6 px-2 rounded-md text-[12px] font-medium border transition-colors cursor-pointer shrink-0 disabled:opacity-60 disabled:cursor-default ${
              picking
                ? 'text-accent bg-accent-subtle border-accent hover:bg-accent-subtle'
                : 'text-text bg-transparent border-border hover:bg-bg-hover hover:border-border-strong'
            }`}
            title={picking ? i18nT('components.webPreviewPanel.annotate_done_tooltip') : i18nT('components.webPreviewPanel.annotate_tooltip')}
            data-testid="browser-annotate"
          >
            <MousePointerClick size={13} aria-hidden />
            <span>{picking ? i18nT('components.webPreviewPanel.annotate_done') : i18nT('components.webPreviewPanel.annotate')}</span>
          </button>
        )}
      </div>
      {/* Annotate errors get their own row under the header: inline in the
          header they would wrap into a sliver beside the button. askAgent is
          safe here -- the notes live in the panel's mirror (persisted per slot),
          so the hand-off destroys nothing. */}
      {annotateError && (
        <div className="px-3 py-1.5 border-b border-border shrink-0" style={{ backgroundColor: 'var(--bg-elevated)' }}>
          <ErrorNotice message={annotateError} variant="inline" askAgent={annotateErrorAskAgent} onDismiss={() => setAnnotateError('')} />
        </div>
      )}
      {/* Address bar. The preview subtree in WebPreviewPanel.tsx (which owns the other URL form)
          is hidden while the native surface is up, so without this the user
          could reach a page and then have no way to leave it. */}
      <form
        className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0"
        onSubmit={e => { e.preventDefault(); commitNative(draft) }}
      >
        <div className="flex-1 min-w-0 flex items-center gap-0.5 h-7 pl-1 pr-1 rounded-md bg-bg-elevated border border-border focus-within:border-accent transition-colors">
          <input
            ref={nativeInputRef}
            type="text"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder={i18nT('components.webPreviewPanel.localhost_5173')}
            aria-label={i18nT('components.webPreviewPanel.preview_url')}
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            className="flex-1 min-w-0 h-full bg-transparent border-none text-[12px] text-text placeholder:text-muted px-1"
          />
          <a
            href={native.state?.url || undefined}
            target="_blank"
            rel="noreferrer"
            aria-disabled={!native.state?.url}
            className={`flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 no-underline ${!native.state?.url ? 'opacity-40 pointer-events-none' : ''}`}
            title={i18nT('components.webPreviewPanel.open_in_browser')}
            aria-label={i18nT('components.webPreviewPanel.open_in_browser')}
          >
            <ExternalLink size={13} />
          </a>
        </div>
      </form>
      {/* Measured host for the native view. Empty by design. */}
      <div ref={native.hostRef} className="flex-1 min-h-0 bg-black" />
      {/* Annotation list: mirrors the notes typed on the page. Below the view so
          the address bar and the page stay together; the host above shrinks to
          make room, which the bounds report picks up. Shown while a session is
          alive (picking, or notes exist). */}
      {(picking || annotations.length > 0) && (
        <div className="shrink-0 border-t border-border" style={{ backgroundColor: 'var(--bg-elevated)' }} data-testid="browser-annotations">
          <div className="flex items-center gap-2 px-3 py-1.5">
            <span className="text-[12px] font-medium text-text">
              {i18nT('components.webPreviewPanel.annotations_count', { count: annotations.length })}
            </span>
            <div className="flex-1" />
            <button
              type="button"
              onClick={clearAnnotations}
              disabled={!annotations.length || annotateBusy}
              className={`text-[12px] px-2 h-6 rounded-md border transition-colors cursor-pointer bg-transparent disabled:opacity-40 disabled:cursor-default ${
                clearArmed ? 'text-danger hover:text-danger-text border-danger hover:bg-danger/10' : 'text-text border-border hover:bg-bg-hover hover:border-border-strong'
              }`}
              aria-label={clearArmed
                ? i18nT('components.webPreviewPanel.annotate_clear_confirm', { count: annotations.length })
                : i18nT('components.webPreviewPanel.annotate_clear')}
            >
              {clearArmed
                ? i18nT('components.webPreviewPanel.annotate_clear_confirm', { count: annotations.length })
                : i18nT('components.webPreviewPanel.annotate_clear')}
            </button>
            <button
              type="button"
              onClick={() => { void sendAnnotations() }}
              disabled={!canSend || annotateBusy}
              // Disabled = muted fill, not accent-at-40%: on dark themes a faded
              // accent still reads as clickable, and this button is disabled
              // exactly while the first note is being typed.
              className="inline-flex items-center gap-1.5 text-[12px] px-2.5 h-6 rounded-md bg-accent text-white hover:opacity-90 transition-opacity cursor-pointer border-none disabled:bg-bg-hover disabled:text-muted disabled:cursor-default disabled:hover:opacity-100"
              title={i18nT('components.webPreviewPanel.annotate_send_tooltip')}
              data-testid="browser-annotations-send"
            >
              {annotateBusy ? <Loader2 size={12} className="animate-spin" aria-hidden /> : <Plus size={12} aria-hidden />}
              {i18nT('components.webPreviewPanel.annotate_send')}
            </button>
          </div>
          {addedAck && (
            <div className="px-3 pb-1.5 text-[11px] leading-snug text-accent" role="status" data-testid="browser-annotations-added">
              {i18nT('components.webPreviewPanel.annotate_added_ack')}
            </div>
          )}
          {annotations.some(a => a.detached) && (
            <div className="px-3 pb-1.5 text-[11px] text-muted leading-snug">
              {i18nT('components.webPreviewPanel.annotate_detached')}
            </div>
          )}
          {annotations.length === 0 ? (
            <div className="px-3 pb-2 text-[11px] text-muted leading-snug">
              {i18nT('components.webPreviewPanel.annotate_empty_hint')}
            </div>
          ) : (
            <ul className="m-0 p-0 list-none max-h-40 overflow-y-auto border-t border-border">
              {annotations.map(a => (
                <li
                  key={a.id}
                  // Wraps: the editor takes a full row of its own, so a 320px
                  // panel still shows badge + descriptor above a usable input.
                  className={`flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-1 border-b border-border last:border-b-0 text-[12px] ${a.detached ? 'opacity-70' : ''}`}
                  title={a.detached ? i18nT('components.webPreviewPanel.annotate_detached') : undefined}
                  data-testid={editing?.id === a.id ? 'browser-annotation-editing' : undefined}
                >
                  <span
                    className="shrink-0 min-w-[18px] h-[18px] px-1.5 rounded-full text-[11px] font-semibold text-white text-center leading-[18px]"
                    // Marker identity colour, matching the on-page badges; not
                    // the danger hue, which belongs to Remove and the armed Clear.
                    style={{ backgroundColor: 'var(--accent)' }}
                    aria-hidden
                  >
                    {a.n}
                  </span>
                  <span className={`shrink-0 font-mono text-[11px] text-muted truncate ${editing?.id === a.id ? 'max-w-[calc(100%-1.75rem)]' : 'max-w-[40%]'}`} title={`${a.ref} · ${a.selector}`}>
                    {describeAnnotationTarget(a, annotateRoleNames())}
                  </span>
                  {editing?.id === a.id ? (
                    // The note editor. Typed HERE, not in the page: the page is
                    // external content and must never see the user's prose.
                    // Enter saves, Esc cancels (a fresh pick with nothing typed
                    // is dropped), Save/Remove for pointer users.
                    <div className="flex items-center gap-2 basis-full min-w-0">
                      <input
                        ref={noteInputRef}
                        type="text"
                        value={editing.text}
                        onChange={e => setEditingText(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') { e.preventDefault(); commitNote() }
                          else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelNote() }
                        }}
                        placeholder={i18nT('components.webPreviewPanel.annotate_note_placeholder')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_note_for_n', { n: a.n })}
                        className="flex-1 min-w-0 h-6 px-2 rounded-md bg-bg-elevated border border-border text-[12px] text-text placeholder:text-muted focus-ring"
                        data-testid="browser-annotation-note-input"
                      />
                      <button
                        type="button"
                        onClick={commitNote}
                        className="text-[12px] px-2 h-6 rounded-md bg-accent text-white border-none cursor-pointer hover:opacity-90 transition-opacity shrink-0"
                      >
                        {i18nT('components.webPreviewPanel.annotate_save')}
                      </button>
                      <button
                        type="button"
                        onClick={() => { void removeAnnotation(a.id) }}
                        className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-danger hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                        title={i18nT('components.webPreviewPanel.annotate_remove')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_remove_n', { n: a.n })}
                      >
                        <X size={12} />
                      </button>
                    </div>
                  ) : (
                    <>
                      <span className="flex-1 min-w-0 truncate text-text" title={a.note}>{a.note}</span>
                      <button
                        type="button"
                        onClick={() => editAnnotation(a.id)}
                        className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                        title={i18nT('components.webPreviewPanel.annotate_edit_note')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_edit_note_n', { n: a.n })}
                      >
                        <Pencil size={12} />
                      </button>
                      <button
                        type="button"
                        onClick={() => { void removeAnnotation(a.id) }}
                        className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-danger hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                        title={i18nT('components.webPreviewPanel.annotate_remove')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_remove_n', { n: a.n })}
                      >
                        <X size={12} />
                      </button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
