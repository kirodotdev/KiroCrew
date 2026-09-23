import { useState, useEffect, useRef, useCallback, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft } from 'lucide-react'
import { api } from '../api/client'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'
import { useImeGuard } from '../hooks/useImeGuard'
import { LISTING_FAILURE_KEYS, searchErrorCause, type SearchErrorCause } from '../lib/searchErrorCause'

// Copy for a failed listing that names the path the FAILED read asked for. After a failed drill
// the input and the rows still show the listing the drill left from, so the shared folder-panel
// copy ("No access to this folder", "Folder listing timed out") named the wrong folder: the one on
// screen, not the one that failed. Every arm is here: the permanent ones name the refused path
// and the next step, and the timed-out and failed ones name it because Retry re-asks that same
// path. Only a read with no path of its own -- the opening `browse()`, which the backend resolves
// to `$HOME` -- falls back to the shared pathless copy in `LISTING_FAILURE_KEYS`. Kept as this
// picker's own keys rather than ProjectPicker's: its copy also restates the listing still shown,
// and one key serving two pickers lets a rewording for one silently change the other.
const NAMED_PATH_KEYS: Record<SearchErrorCause, string> = {
  timed_out: 'components.workspacePicker.listing_timed_out',
  failed: 'components.workspacePicker.listing_failed',
  denied: 'components.workspacePicker.listing_denied',
  root_missing: 'components.workspacePicker.listing_root_missing',
}
interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  anchorRef: RefObject<HTMLElement | null>
  onCreated: (name: string) => void
}

export default function WorkspacePicker({ open, onOpenChange, anchorRef, onCreated }: Props) {
  // One instance covers both inputs; the binding's focus/blur reset makes sharing safe.
  const ime = useImeGuard()
  const [input, setInput] = useState('')
  const [browsePath, setBrowsePath] = useState('')
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [selectedDir, setSelectedDir] = useState('')
  const [wsName, setWsName] = useState('')
  /** Client-side hint ("name is required"): not a failure, so not an ErrorNotice. */
  const [error, setError] = useState('')
  /** A request failure, from the backend or the transport. */
  const [requestError, setRequestError] = useState('')
  const [requestErrorCause, setRequestErrorCause] = useState<SearchErrorCause | null>(null)
  const [failedBrowsePath, setFailedBrowsePath] = useState<string | undefined>(undefined)
  const [creating, setCreating] = useState(false)
  // A listing request is unsettled for the CURRENT ticket. Set by every `browse`, cleared only by
  // the settlement that still holds the ticket -- a superseded request's landing must not read
  // as the live one having settled. Renders as an inert "Retrying…" in place of Retry: without
  // it the failure notice and an enabled Retry sat unchanged for the whole re-ask, and a second
  // press took a fresh ticket and restarted the wait. The sites that retire a ticket without a
  // new request (Select, close) leave it as is: each also clears the notice the control sits
  // in, and the next `browse` resets it, so a stale `true` has nothing to make inert.
  const [browsing, setBrowsing] = useState(false)
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)
  // Every listing request takes the next ticket. Only the latest settlement may replace the
  // visible rows or the failure notice. A ticket is retired by a newer request, and by the
  // sites that leave the browse pane (Select, Escape, a click outside) -- never by typing.
  const listingSeq = useRef(0)
  // Every keystroke in the path input takes the next edit. A keystroke takes ownership of the
  // FIELD, not of the listing: a request that started before the latest edit still lands its
  // rows, but does not run `setInput(d.path)` over what was typed, and drops a failure notice
  // that names a path the user is replacing. It never leaves the browse pane with no rows, no
  // empty-state, no notice and nothing in flight -- which is what retiring the ticket did: the
  // field is `autoFocus`ed, a keystroke inside the opening read's round trip is ordinary, and
  // on a slow gateway that round trip is long. Nothing but close-and-reopen could then start
  // another listing.
  const inputEdits = useRef(0)
  const clearRequestFailure = useCallback(() => {
    setRequestError('')
    setRequestErrorCause(null)
    setFailedBrowsePath(undefined)
  }, [])

  const browse = useCallback((path?: string, seedField = true) => {
    const ticket = ++listingSeq.current
    const edit = inputEdits.current
    setBrowsing(true)
    api.browseDirs(path).then(d => {
      if (ticket !== listingSeq.current) return
      setBrowsing(false)
      clearRequestFailure()
      setBrowsePath(d.path)
      setBrowseParent(d.parent)
      setBrowseDirs(d.dirs)
      // The field is the user's once they have typed since this request started; a listing
      // they asked for AFTER typing (a drill, Back, Retry) seeds it as a drill always has. A
      // caller that knows the field already holds the user's text passes `seedField` false.
      if (seedField && edit === inputEdits.current) setInput(d.path)
    }).catch((err: unknown) => {
      if (ticket !== listingSeq.current) return
      setBrowsing(false)
      // A failed read of a path the user has typed over since it started would name a path
      // they are replacing, and its Retry would re-ask that path, not the input: dropped. The
      // pathless read (the opening `browse()`, or its Retry) names no path, so its notice lands
      // whatever was typed -- with nothing listed, it is the only remedy the pane has.
      if (path && edit !== inputEdits.current) return
      const cause = searchErrorCause(err)
      // The path the FAILED request asked for, never the input's value: the input still
      // shows the listing the drill left from.
      setRequestError(path ? i18nT(NAMED_PATH_KEYS[cause], { path }) : i18nT(LISTING_FAILURE_KEYS[cause]))
      setRequestErrorCause(cause)
      setFailedBrowsePath(path)
    })
  }, [clearRequestFailure])

  useEffect(() => {
    if (!open) return
    browse()
  }, [open, browse])

  useEffect(() => {
    if (!open) return
    const timer = setTimeout(() => {
      const handler = (e: MouseEvent) => {
        if (dropRef.current && !dropRef.current.contains(e.target as Node) &&
            btnRef.current && !btnRef.current.contains(e.target as Node)) {
          listingSeq.current++; onOpenChange(false); setSelectedDir(''); setWsName(''); setError(''); clearRequestFailure()
        }
      }
      document.addEventListener('mousedown', handler)
      cleanup = () => document.removeEventListener('mousedown', handler)
    }, 0)
    let cleanup = () => {}
    return () => { clearTimeout(timer); cleanup() }
    // `btnRef` is a stable ref object and the handler reads `.current` fresh;
    // `onOpenChange` is a parent callback that may not be memoized, so we only
    // (re)attach the click-outside listener on `open` transitions to avoid
    // tearing it down on every parent re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const selectDir = (dir: string) => {
    listingSeq.current++
    setSelectedDir(dir)
    setWsName(dir.split('/').filter(Boolean).pop() || '')
    setInput(dir)
    setError('')
    clearRequestFailure()
  }

  // Every exit from the create form that RETURNS to the browse pane (its Back, the name input's
  // Escape) -- not the ones that close the popover. Select retired the listing ticket and this
  // clears the notice, so with no listing ever landed (`browsePath` '': the opening read failed,
  // or Select retired it) the pane came back with no rows, no empty-state, no notice and nothing
  // in flight. Re-run the opening read instead: it lands rows, the empty-state, or the pathless
  // notice with its Retry. It races nothing: the create form starts no listing and Select
  // retired the last ticket, and a reopen's opening read that could still be in flight asks for
  // the same listing, which the new ticket supersedes. The field holds the path the user typed
  // before Select (Select needs one when nothing has listed), so the read does not seed it.
  const leaveCreateForm = () => {
    setSelectedDir(''); setWsName(''); clearRequestFailure()
    if (!browsePath) browse(undefined, false)
  }

  const create = async () => {
    const name = wsName.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-')
    if (!name) { setError(i18nT('components.workspacePicker.name_required')); return }
    setCreating(true); setError(''); clearRequestFailure()
    try {
      const res = await api.createWorkspace({ name, dir: selectedDir }) as { ok?: boolean; error?: string }
      if (res.error) { setRequestError(res.error); setCreating(false); return }
      onCreated(name)
      onOpenChange(false); setSelectedDir(''); setWsName('')
    } catch { setRequestError(i18nT('components.workspacePicker.failed_to_create_workspace')) }
    setCreating(false)
  }

  if (!open || !btnRef.current) return null

  const q = input.toLowerCase()
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? browseDirs.filter(d => d.name.toLowerCase().includes(q.split('/').pop() || '') || d.path.toLowerCase().includes(q)) : browseDirs
  const canRetryBrowse = requestErrorCause === 'timed_out' || requestErrorCause === 'failed'
  // The "No subdirectories" empty-state and the notice for a failed read of the listing ON
  // SCREEN describe the same list, so those two never share the screen. Only that failure
  // hides it. `requestError` on this view is always a listing failure: a create failure is
  // shown on the create form, which replaces the list, and every way out of that form clears
  // it. The failed read was this listing's when its path names `browsePath`, or when either
  // side has no name -- the opening read is `browse()` with no path, and a first open that
  // never listed has nothing on screen to be empty. Both sides are backend-canonical (this
  // picker reads no typed path: Enter selects), so the name check is exact. A failed read of a
  // DIFFERENT path -- Back from an empty directory whose parent then fails -- leaves the shown
  // listing intact, so its empty-state stays beside the notice rather than a blank list region.
  // The empty-state also describes a listing that was READ, so it waits for one to land
  // (`browsePath`): until the opening read lands -- however long a slow gateway takes, and
  // whatever is typed meanwhile -- "No subdirectories" would claim a directory nothing ever
  // listed.
  const shownListingFailed = !!requestError
    && (!failedBrowsePath || !browsePath || failedBrowsePath === browsePath)

  return createPortal(
        <div ref={dropRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg w-[400px] max-h-[460px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const maxH = window.innerHeight - r.bottom - 8; return { top: r.bottom + 4, left: Math.max(8, r.right - 400), maxHeight: Math.max(200, maxH) } })()}>
          {selectedDir ? (
            <div className="p-3 flex flex-col gap-2">
              <div className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.workspacePicker.create_workspace')}</div>
              <div className="text-[13px] font-mono text-text truncate bg-bg-elevated rounded px-2 py-1.5 border border-border">{selectedDir}</div>
              <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.workspace_name')} placeholder={i18nT('components.workspacePicker.workspace_name_2')} value={wsName} onChange={e => { setWsName(e.target.value); setError(''); clearRequestFailure() }} {...ime.bindEnter({ onEnter: create, onEscape: leaveCreateForm })} className="bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-hidden focus-visible:border-accent" />
              {error && <div className="text-[11px] text-danger">{error}</div>}
              {/* No hand-off: the workspace name in `wsName` and the chosen directory are unsaved until Create. */}
              <ErrorNotice message={requestError} />
              <div className="flex gap-2 justify-end">
                <button onClick={leaveCreateForm} className="px-3 py-1.5 text-[12px] text-muted hover:text-text rounded">{i18nT('components.workspacePicker.back')}</button>
                <button onClick={create} disabled={creating} className="px-3 py-1.5 text-[12px] bg-accent text-accent-fg rounded hover:bg-accent/80 disabled:opacity-50">{creating ? i18nT('components.workspacePicker.creating') : i18nT('components.workspacePicker.create')}</button>
              </div>
            </div>
          ) : (
            <>
              <div className="p-2 border-b border-border flex gap-1 items-center">
                {browseParent && browseParent !== browsePath && (
                  <button onClick={() => browse(browseParent)} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0" title={i18nT('components.workspacePicker.back')} aria-label={i18nT('components.workspacePicker.back')}><ChevronLeft size={16} /></button>
                )}
                <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.project_directory_path')} placeholder={i18nT('components.workspacePicker.path_to_project')} value={input} onChange={e => {
                  // Typing is the recovery, as in ProjectPicker: a notice that names a path
                  // (a failed drill or Back) names one the user is replacing, and its Retry
                  // re-asks that path, not the input. The pathless notice of the opening read
                  // stays: it names no path, and with nothing listed it is the pane's only
                  // remedy. The keystroke takes an edit, not the listing ticket: whatever is in
                  // flight still lands its rows (see `inputEdits`).
                  setInput(e.target.value); inputEdits.current++
                  if (failedBrowsePath) clearRequestFailure()
                }} {...ime.bindEnter({ onEnter: () => { if (input.trim()) selectDir(input.trim()) }, onEscape: () => { listingSeq.current++; clearRequestFailure(); onOpenChange(false) } })} className="flex-1 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-hidden focus-visible:border-accent" />
                {/* Nothing to select (a blank field and nothing listed yet) is a no-op, as Enter on a
                    blank field is: `selectDir('')` stayed on this pane yet retired the listing ticket
                    and cleared the notice, which left it with nothing on it and nothing in flight. */}
                <button onClick={() => { const dir = input.trim() || browsePath; if (dir) selectDir(dir) }} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0">{i18nT('components.workspacePicker.select')}</button>
              </div>
              {/* No hand-off: the path typed into `input` and the browse position
                  (`browsePath`) are unsaved until Select, and a hand-off would navigate
                  away from both. The remedy is local instead: Retry re-runs the listing
                  that failed, since the surface has no Refresh of its own. */}
              {requestError && (
                <div className="flex items-center gap-2 pr-2" aria-busy={browsing || undefined}>
                  <ErrorNotice className="flex-1 min-w-0" message={requestError} />
                  {canRetryBrowse && (
                    // Relabelled and inert while its request is in flight, so the wait is visible
                    // and a second press is impossible. Inert-but-focusable like FolderPanel's
                    // Refresh: `aria-disabled` plus the in-handler guard, NOT `disabled`, which
                    // leaves the tab order and so blurs the focused element in real browsers --
                    // a keyboard press would drop focus to <body> for the whole re-ask.
                    <button
                      type="button"
                      onClick={() => { if (browsing) return; browse(failedBrowsePath) }}
                      aria-disabled={browsing || undefined}
                      className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0 aria-disabled:opacity-50"
                    >
                      {browsing ? i18nT('components.workspacePicker.retrying') : i18nT('components.workspacePicker.retry')}
                    </button>
                  )}
                </div>
              )}
              <div className="overflow-y-auto flex-1 min-h-0">
                {browsePath && !shownListingFailed && filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.workspacePicker.no_subdirectories')}</div>}
                {filteredBrowse.map(d => (
                  <button key={d.path} className="w-full text-left px-3 py-1.5 flex items-center gap-2 cursor-pointer hover:bg-bg-hover transition-colors" onClick={() => browse(d.path)}>
                    <FolderOpen size={12} className="text-accent shrink-0" />
                    <span className="text-[13px] font-mono text-text truncate">{d.name}</span>
                    <ChevronRight size={12} className="text-muted ml-auto shrink-0" />
                  </button>
                ))}
              </div>
            </>
          )}
        </div>,
        document.body
      )
}
