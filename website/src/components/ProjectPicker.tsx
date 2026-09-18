import { useState, useEffect, useRef, useCallback, RefObject } from 'react'
import { useImeGuard } from '../hooks/useImeGuard'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft, Clock, Search } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import ErrorNotice from './ErrorNotice'
import { findReport, type ErrorReport } from '../utils/errorReport'
import { endsWithSeparator, isWindowsPath, lastSegment, parentIsDriveList, pathSeparator, stripTrailingSeparator } from '../utils/browsePath'

import { i18nT } from '../i18n/t'
interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  anchorRef?: RefObject<HTMLElement | null>
  anchorRect?: DOMRect | null
  onSelect: (path: string) => void
  /**
   * Turn on the agent hand-off in the listing-failure notice. The hand-off
   * navigates to the chat and unmounts whatever this popover floats over, so
   * only a mount with nothing unsaved beneath it may set this: ChatPage's
   * project chooser does (its composer draft is persisted per slot by
   * `chatDrafts`). Off by default — the safe direction — for FolderConfigModal
   * (folder form), RepoSettings (repo form) and ProjectScaffolderPage (wizard).
   */
  errorHandoff?: boolean
}

export default function ProjectPicker({ open, onOpenChange, anchorRef, anchorRect, onSelect, errorHandoff = false }: Props) {
  const [tab, setTab] = useState<'recent' | 'browse'>('recent')
  const [input, setInput] = useState('')
  const ime = useImeGuard()
  const [browsePath, setBrowsePath] = useState('')
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [recentDirs, setRecentDirs] = useState<string[]>([])
  const [recentQuery, setRecentQuery] = useState('')
  const [browseSel, setBrowseSel] = useState(0)
  // Which listing failed last, if any: a directory (`browse`) or the drive
  // list (`browseDrives`). Said out loud through ErrorNotice — a swallowed
  // failure leaves Back or a typed path appearing to do nothing. Cleared by the
  // next successful listing of either kind, or by the next keystroke, since
  // typing is the recovery the notice suggests.
  const [listFailed, setListFailed] = useState<null | 'dir' | 'drives'>(null)
  // What failed, for the notice: the path that could not be opened, and the
  // structured report the API client journaled for that request (endpoint,
  // status, backend `code`), so the agent hand-off carries the real context
  // rather than only the localized sentence (GPT review on #11424).
  const [failedPath, setFailedPath] = useState('')
  const [failedReport, setFailedReport] = useState<ErrorReport | undefined>(undefined)
  const noteFailure = (kind: 'dir' | 'drives', path: string, err: unknown) => {
    setListFailed(kind)
    setFailedPath(path)
    setFailedReport(findReport(err instanceof Error ? err.message : String(err)))
  }
  // Which kind of listing is on screen. The drive list (Windows only) has no
  // path of its own, so the path field's hint switches to a drive-shaped
  // example there instead of the POSIX one (UX review on #11424).
  const [listing, setListing] = useState<'dir' | 'drives'>('dir')
  // Every listing request (a directory or the drive list) takes the next
  // ticket; a response only lands if its ticket is still the latest. Without
  // this a slow drive list answered after a faster drill into a child would
  // replace that child's rows with the drives (GPT review on #11424).
  const listingSeq = useRef(0)
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const recentSearchRef = useRef<HTMLInputElement>(null)
  const browseItemRefs = useRef<(HTMLElement | null)[]>([])
  const anchorRectRef = useRef<DOMRect | null>(anchorRect ?? null)
  anchorRectRef.current = anchorRect ?? null
  const getAnchorRect = useCallback((): DOMRect | null => {
    if (btnRef?.current && typeof btnRef.current.getBoundingClientRect === 'function') {
      return btnRef.current.getBoundingClientRect()
    }
    return anchorRectRef.current
  }, [btnRef])

  const browse = useCallback((path?: string, preserveInput = false) => {
    const ticket = ++listingSeq.current
    api.browseDirs(path).then(d => {
      if (ticket !== listingSeq.current) return
      setBrowsePath(d.path); setBrowseParent(d.parent); setBrowseDirs(d.dirs); setBrowseSel(0); setListFailed(null); setListing('dir')
      // Append the path delimiter after a browse/drill so the user can start
      // typing the next segment immediately (#1196). Derive the separator from
      // the returned path so a native Windows path (C:\Users\me) stays all-`\`
      // instead of rendering the mixed C:\Users\me/ . A path already ending in
      // its separator (e.g. a drive/filesystem root) is left as-is; the trailing
      // separator is a no-op for the auto-drill effect below (which keys on `/`).
      if (!preserveInput) {
        // `\` is a separator ONLY on a Windows-shaped path (drive-letter `C:...`
        // or UNC `\\...`); on POSIX it is a legal filename character, so always
        // append `/` there (GPT 5.6: never treat a trailing `\` as a separator on
        // a POSIX path). A path already ending in its separator is left as-is.
        const sep = pathSeparator(d.path)
        setInput(d.path.endsWith(sep) ? d.path : d.path + sep)
      }
      // Keep the combobox input focused so arrow/Enter nav continues after a drill.
      requestAnimationFrame(() => inputRef.current?.focus())
    }).catch((err: unknown) => { if (ticket === listingSeq.current) noteFailure('dir', path ?? '', err) })
  }, [])

  // Windows only: the virtual level above every drive root. The backend cues it
  // with `parent: ""` on a drive root (see parentIsDriveList); Back from there
  // lists the mounted drives so the user can cross to D:\ without typing it.
  const browseDrives = useCallback(() => {
    const ticket = ++listingSeq.current
    api.browseDrives().then(d => {
      if (ticket !== listingSeq.current) return
      setBrowsePath(''); setBrowseParent(''); setBrowseDirs(d.dirs); setBrowseSel(0); setListFailed(null); setListing('drives')
      setInput('')
      requestAnimationFrame(() => inputRef.current?.focus())
    }).catch((err: unknown) => { if (ticket === listingSeq.current) noteFailure('drives', '', err) })
  }, [])

  // "Back" has a target when the parent is a different directory, or when the
  // level above is the drive list. Every Back affordance (button, ArrowLeft at
  // the caret start) routes through goUp so the two cannot drift apart.
  // The failure notice names a drive the user is NOT on, so the example never
  // points back at the listing already on screen.
  const otherDriveExample = (current: string) => i18nT(/^[Dd]:/.test(current) ? 'components.projectPicker.drive_example_e' : 'components.projectPicker.drive_example_d')
  // The notices name the listing the way the path field shows it (with its
  // trailing separator), so the two read as the same place (UX review on #11424).
  const shownPath = browsePath && !browsePath.endsWith(pathSeparator(browsePath)) ? browsePath + pathSeparator(browsePath) : browsePath
  // Nothing to commit on the drive list; and after a failed folder listing,
  // nothing to commit while the field still names the path that failed — the
  // folder form would inherit that broken path (UX review on #11424). When the
  // field names the listing that IS on screen (a failed drill by click leaves
  // `D:\work\` in the field and D:\work's rows below), or when only the drive
  // list failed, the directory shown is intact and stays committable. Both the
  // Select button and Ctrl+Enter read this one predicate.
  const typed = stripTrailingSeparator(input.trim())
  const fieldNamesShownListing = !!browsePath && (isWindowsPath(typed) ? typed.toLowerCase() === browsePath.toLowerCase() : typed === browsePath)
  const canCommit = (!!input.trim() || !!browsePath) && (listFailed !== 'dir' || fieldNamesShownListing)
  const atDriveRoot = parentIsDriveList(browsePath, browseParent)
  const canGoUp = atDriveRoot || (!!browseParent && browseParent !== browsePath)
  const goUp = () => { if (atDriveRoot) browseDrives(); else browse(browseParent) }

  useEffect(() => {
    if (!open) return
    setRecentQuery('')
    setListFailed(null)
    api.recentProjects().then(d => {
      setRecentDirs(d.dirs || [])
      setTab(d.dirs?.length ? 'recent' : 'browse')
    }).catch(() => setTab('browse'))
    browse()
  }, [open, browse])

  useEffect(() => {
    if (!open) return
    let cleanup = () => {}
    const timer = setTimeout(() => {
      const handler = (e: MouseEvent) => {
        if (dropRef.current && dropRef.current.contains(e.target as Node)) return
        const target = e.target as Node | null
        const live = btnRef?.current
        if (live && typeof (live as Element).contains === 'function' && (live as Element).contains(target)) return
        const r = getAnchorRect()
        if (r && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
        onOpenChange(false)
      }
      document.addEventListener('mousedown', handler)
      cleanup = () => document.removeEventListener('mousedown', handler)
    }, 0)
    return () => { clearTimeout(timer); cleanup() }
  }, [open, onOpenChange, btnRef, getAnchorRect])

  const select = (path: string) => {
    // The browse input carries a trailing delimiter for typing continuation
    // (#1196); the committed project path must stay clean. `\` is a separator
    // ONLY on a Windows-shaped path (drive-letter `C:...` or UNC `\\...`); on
    // POSIX it is a legal filename char, so only `/` is stripped there and a real
    // trailing `\` is preserved (GPT 5.6). Bare roots stay intact: POSIX `/` and a
    // Windows drive root `C:\` / `C:/` (stripping `C:/` to `C:` would yield a
    // drive-RELATIVE path, not the drive root).
    onSelect(stripTrailingSeparator(path)); onOpenChange(false)
  }
  const rq = recentQuery.trim().toLowerCase()
  const filteredRecent = rq ? recentDirs.filter(d => d.toLowerCase().includes(rq)) : recentDirs

  // Recent tab uses the shared selected-index keyboard nav (same model as the
  // Skill/File pickers). The Browse tab has its own combobox input handler
  // below, so the hook is only armed on Recent to avoid double-handling keys.
  const recentNav = useListKeyboardNav({
    open: open && tab === 'recent',
    count: filteredRecent.length,
    onChoose: i => { const d = filteredRecent[i]; if (d) select(d) },
    onClose: () => onOpenChange(false),
  })

  // Reset the Recent highlight whenever the filtered list changes.
  useEffect(() => { recentNav.setSelected(0) }, [recentQuery]) // eslint-disable-line react-hooks/exhaustive-deps

  // Reset the Browse highlight whenever the visible list changes (tab switch,
  // drill into a new dir, or filter edit).
  useEffect(() => { setBrowseSel(0) }, [tab, input, browsePath])

  // Auto-drill on a typed trailing separator. Without this, typing "/foo/bar/"
  // only filters the *current* directory's children by the last segment — the
  // list never descends into the typed subdirectory. When the input ends with
  // its shape's separator (`/`, or also `\` on a Windows-shaped path such as
  // `D:\`) and differs from the dir we've already loaded, browse into it.
  // Debounced so intermediate keystrokes before the separator don't each fire
  // a request. A bare drive root keeps its separator: `D:` alone is a
  // drive-RELATIVE path the backend would resolve to that drive's cwd.
  useEffect(() => {
    if (!open || tab !== 'browse') return
    const trimmed = input.trim()
    if (!endsWithSeparator(trimmed) || trimmed.length <= 1) return
    const target = stripTrailingSeparator(trimmed)
    if (!target) return
    // Windows paths compare case-insensitively (the backend canonicalises `c:\` to `C:\`).
    const same = isWindowsPath(target) ? target.toLowerCase() === browsePath.toLowerCase() : target === browsePath
    if (same) return
    const t = setTimeout(() => browse(target, true), 250)
    return () => clearTimeout(t)
  }, [input, open, tab, browsePath, browse])

  // Keep the highlighted Browse subdir scrolled into view.
  useEffect(() => {
    if (!open || tab !== 'browse') return
    const el = browseItemRefs.current[browseSel]
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
  }, [browseSel, open, tab])

  const anchorR = getAnchorRect()
  if (!open || !anchorR) return null

  const q = input.toLowerCase()
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? browseDirs.filter(d => d.name.toLowerCase().includes(lastSegment(q)) || d.path.toLowerCase().includes(q)) : browseDirs

  // Keyboard isolation for the popover, matching the boundary `Modal` carries on
  // its own panel (see Modal.tsx's ModalDialog). It is needed SEPARATELY here
  // because this popover portals as a React SIBLING of the `<Modal>` it paints
  // above (FolderConfigModal renders it after `</Modal>`), and React routes
  // synthetic events along the REACT tree — so Modal's panel handler is not an
  // ancestor on this dispatch path and never sees these keystrokes. Sharing the
  // modal's stacking context is a PAINT-order fact and implies nothing about
  // event routing; conflating the two is what left this open (#6833).
  //
  // Unguarded, a global chord typed in either field here (the Ctrl+digit session
  // jumps and the Settings chord deliberately fire while an input has focus)
  // reaches `useKeyboardShortcuts`' bubble-phase `document` listener, navigates
  // away, and unmounts the dialog underneath with its part-filled draft.
  //
  // Escape is excepted. Both dismissal paths that exist today already consume it
  // before this handler runs — the Recent list at document CAPTURE
  // (useListKeyboardNav), the Browse field as the event's own target — so the
  // exception changes nothing observable today. What it protects is the
  // CONTRACT: `stopPropagation()` on a synthetic event stops the native event
  // too, and bubble-phase `window` is exactly where Modal's own dismissal
  // listens, so a blanket stop here would break any dismissal wired that way the
  // moment one appears. Measured, not assumed — a blanket-stop mutant passes
  // every OTHER assertion in ProjectPicker.keyboardIsolation.test.tsx, which is
  // why that file pins the window-bubble property on its own.
  //
  // One exception to the exception: an Escape the IME owns is cancelling a
  // candidate list, not the popover. This reuses the component's EXISTING
  // `ime` guard rather than mounting a second document-tracked latch, since a
  // third latch instance is the very cost flagged against this fix shape.
  // Bubble phase on purpose: the capture-phase listeners this surface depends
  // on (useListKeyboardNav's document capture, Modal's window-capture Tab trap)
  // run before the event reaches the target, so the boundary cannot starve them.
  const isolateKeys = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      // Consumes the native event AND React's propagation flag when the IME
      // owns it; leaves an accepted Escape entirely untouched for the handlers
      // above. See `claimSyntheticKey`'s contract in useImeGuard.ts.
      ime.claimKey(e)
      return
    }
    e.stopPropagation()
  }

  return createPortal(
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- keyboard-isolation barrier (see above), not an activatable control; there is no behaviour for a keyboard to be given, and every control inside here is a real input or button. Adding a role/tab stop would advertise an interaction this element does not have.
    <div ref={dropRef} onKeyDown={isolateKeys} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl w-[400px] max-w-[calc(100vw-16px)] flex flex-col overflow-hidden animate-slide-up" style={(() => {
      const dropMinH = 200
      const spaceBelow = window.innerHeight - anchorR.bottom - 8
      const flipUp = spaceBelow < dropMinH || anchorR.bottom > window.innerHeight / 2
      const left = Math.max(8, Math.min(anchorR.right - 400, window.innerWidth - 408))
      if (flipUp) {
        const spaceAbove = anchorR.top - 8
        return { bottom: window.innerHeight - anchorR.top + 4, left, height: Math.min(460, Math.max(200, spaceAbove)) }
      }
      return { top: anchorR.bottom + 4, left, height: Math.min(460, Math.max(200, spaceBelow)) }
    })()}>
      {/* Tabs */}
      <div className="flex border-b border-border">
        <button className={`flex-1 px-3 py-2 text-[12px] font-medium flex items-center justify-center gap-1.5 transition-colors ${tab === 'recent' ? 'text-accent border-b-2 border-accent' : 'text-muted hover:text-text'}`} onMouseDown={e => { e.preventDefault(); setTab('recent') }}>
          <Clock size={12} /> {i18nT('components.projectPicker.recent')}
        </button>
        <button className={`flex-1 px-3 py-2 text-[12px] font-medium flex items-center justify-center gap-1.5 transition-colors ${tab === 'browse' ? 'text-accent border-b-2 border-accent' : 'text-muted hover:text-text'}`} onMouseDown={e => { e.preventDefault(); setTab('browse') }}>
          <FolderOpen size={12} /> {i18nT('components.projectPicker.browse')}
        </button>
      </div>

      {tab === 'recent' ? (
        <>
          {recentDirs.length > 0 && (
            <div className="p-2 border-b border-border">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted pointer-events-none" />
                <input
                  ref={recentSearchRef}
                  autoFocus
                  type="text"
                  aria-label={i18nT('components.projectPicker.search_recent_projects')}
                  aria-controls="pp-recent-list"
                  placeholder={i18nT('components.projectPicker.search_recent_projects_2')}
                  value={recentQuery}
                  onChange={e => setRecentQuery(e.target.value)}
                  className="w-full bg-bg-elevated border border-border rounded pl-7 pr-3 py-1.5 text-[13px] text-text placeholder:text-muted focus:outline-none focus-visible:border-accent"
                />
              </div>
            </div>
          )}
          <div id="pp-recent-list" role="listbox" aria-label={i18nT('components.projectPicker.recent_projects')} className="overflow-y-auto flex-1 min-h-0">
            {recentDirs.length === 0 ? (
              <div className="px-3 py-6 text-[12px] text-muted text-center">{i18nT('components.projectPicker.no_recent_projects')}</div>
            ) : filteredRecent.length === 0 ? (
              <div className="px-3 py-6 text-[12px] text-muted text-center">{i18nT('components.projectPicker.no_matching_projects')}</div>
            ) : filteredRecent.map((d, i) => (
              <button
                key={d}
                role="option"
                aria-selected={i === recentNav.selected}
                id={`pp-recent-${i}`}
                tabIndex={-1}
                ref={el => { recentNav.itemRefs.current[i] = el }}
                className={`w-full text-left px-3 py-2 flex items-center gap-2 cursor-pointer transition-colors ${i === recentNav.selected ? 'bg-bg-hover' : 'hover:bg-bg-hover'}`}
                onMouseEnter={() => recentNav.setSelected(i)}
                onMouseDown={e => { e.preventDefault(); select(d) }}
              >
                <FolderOpen size={12} className="text-accent shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-mono font-semibold text-text truncate">{d.split('/').pop()}</div>
                  <div className="text-[11px] text-muted truncate">{d}</div>
                </div>
              </button>
            ))}
          </div>
        </>
      ) : (
        <>
          <div className="p-2 border-b border-border flex gap-1 items-center">
            {canGoUp && (
              /* One control, one shape: chevron plus a visible name. The name is
                 "Back" inside a drive and "All drives" at its root, where "back"
                 would read as a guess (there is no folder above C:\) — UX review
                 on #11424 asked for the destination, and for the label not to
                 appear and vanish between the two states. */
              <button aria-label={atDriveRoot ? i18nT('components.projectPicker.all_drives') : i18nT('components.projectPicker.back')} onClick={goUp} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0 flex items-center gap-0.5" title={atDriveRoot ? i18nT('components.projectPicker.all_drives') : i18nT('components.projectPicker.back_to', { path: browseParent })}>
                <ChevronLeft size={16} />
                <span className="text-[11px] font-medium pr-1">{atDriveRoot ? i18nT('components.projectPicker.all_drives') : i18nT('components.projectPicker.back')}</span>
              </button>
            )}
            <input
              ref={inputRef}
              autoFocus
              type="text"
              role="combobox"
              aria-expanded={true}
              aria-label={i18nT('components.projectPicker.project_directory_path')}
              aria-controls="pp-browse-list"
              aria-activedescendant={filteredBrowse.length ? `pp-dir-${browseSel}` : undefined}
              placeholder={listing === 'drives' ? i18nT('components.projectPicker.path_to_project_drive') : i18nT('components.projectPicker.path_to_project')}
              value={input}
              onChange={e => {
                setInput(e.target.value); setListFailed(null)
                // A keystroke retires every listing still in flight: a drive list
                // answering now would run `setInput('')` and erase what was just
                // typed before its own auto-drill fires (GPT review on #11424).
                listingSeq.current++
              }}
              {...ime.bindComposition()}
              onKeyDown={e => {
                const n = filteredBrowse.length
                const commit = () => { if (!canCommit) return; const p = input.trim() || browsePath; if (p) select(p) }
                if (e.key === 'ArrowDown') { e.preventDefault(); setBrowseSel(s => (n ? Math.min(s + 1, n - 1) : 0)) }
                else if (e.key === 'ArrowUp') { e.preventDefault(); setBrowseSel(s => Math.max(s - 1, 0)) }
                else if (e.key === 'Enter') {
                  // Rule 2: the handler also carries the arrow keys, so only the
                  // Enter path is claimed — arrow navigation stays untouched.
                  if (!ime.claimEnter(e)) return
                  if (e.metaKey || e.ctrlKey) commit()                               // ⌘/Ctrl+Enter commits the current dir
                  else if (n > 0 && filteredBrowse[browseSel]) browse(filteredBrowse[browseSel].path)  // Enter drills into the highlighted folder
                  else commit()                                                       // nothing to drill into -> commit typed path
                }
                else if (e.key === 'ArrowLeft' && e.currentTarget.selectionStart === 0 && e.currentTarget.selectionEnd === 0 && canGoUp) {
                  e.preventDefault(); goUp()                                          // caret at start -> go to parent (or the drive list)
                }
                else if (e.key === 'Escape' || e.key === 'Tab') {
                  // This input is a composable free-text path field. An Escape
                  // or Tab the IME owns is cancelling or cycling the candidate
                  // list, not leaving the picker — acting on it would close the
                  // popover and yank focus mid-composition. `claimKey` claims
                  // through this input's own tracked latch (the
                  // `bindComposition` spread above feeds it) and owns the
                  // whole decline: native consumption per the latch contract,
                  // and the synthetic propagation stop React ancestors read.
                  if (!ime.claimKey(e)) return
                  e.preventDefault(); onOpenChange(false); btnRef?.current?.focus()
                }
              }}
              className="flex-1 min-w-0 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus-visible:border-accent"
            />
            <button disabled={!canCommit} onMouseDown={e => { e.preventDefault(); if (canCommit) select(input.trim() || browsePath) }} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 disabled:opacity-40 disabled:cursor-not-allowed shrink-0">{i18nT('components.projectPicker.select')}</button>
          </div>
          {listFailed && (
            <div className="px-3 py-2 border-b border-border">
              {/* No hand-off unless the mount opts in (`errorHandoff`): three
                  of the four callers float this popover over an unsaved draft —
                  FolderConfigModal's folder form (name, colour, tags,
                  directory), RepoSettings' repo form, ProjectScaffolderPage's
                  wizard — and the hand-off would navigate away from it. ChatPage
                  opts in; its composer draft is persisted. When on, the popover
                  closes itself so it does not float over the chat it hands to. */}
              <ErrorNotice
                variant="inline"
                className="whitespace-normal"
                askAgent={errorHandoff}
                onHandoff={() => onOpenChange(false)}
                report={failedReport}
                message={listFailed === 'drives'
                  ? i18nT('components.projectPicker.drives_failed', { path: shownPath, example: otherDriveExample(browsePath) })
                  : browsePath
                    ? i18nT('components.projectPicker.listing_failed', { failed: failedPath, path: shownPath })
                    : i18nT('components.projectPicker.listing_failed_no_path', { failed: failedPath })}
                testId={listFailed === 'drives' ? 'pp-drives-error' : 'pp-listing-error'}
              />
            </div>
          )}
          <div id="pp-browse-list" role="listbox" aria-label={i18nT('components.projectPicker.subdirectories')} className="overflow-y-auto flex-1 min-h-0">
            {filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.projectPicker.no_subdirectories')}</div>}
            {filteredBrowse.map((d, i) => (
              <button
                key={d.path}
                role="option"
                aria-selected={i === browseSel}
                id={`pp-dir-${i}`}
                tabIndex={-1}
                ref={el => { browseItemRefs.current[i] = el }}
                className={`w-full text-left px-3 py-1.5 flex items-center gap-2 cursor-pointer transition-colors ${i === browseSel ? 'bg-bg-hover' : 'hover:bg-bg-hover'}`}
                onMouseEnter={() => setBrowseSel(i)}
                onClick={() => browse(d.path)}
              >
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
