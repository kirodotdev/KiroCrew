import { useState, useEffect, useMemo, useRef, useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { useQuery, type QueryClient } from '@tanstack/react-query'
import { useVisibleTabCatchUp, settleCatchUp, type CatchUpLatch } from '../hooks/useVisibleTabCatchUp'
import type { FileWatchFrame } from '../hooks/useFileWatch'
import { api } from '../api/client'
import { downloadFileName } from '../utils/fileReadUrl'
import { downloadBlob } from '../utils/download'
import { fetchFileRead, fileReadQueryKey, isPartialRead } from '../utils/fileReadQuery'
import { documentBodyEpochNow } from '../hooks/usePanelTabs'
import { copyToClipboard } from '../utils/clipboard'
import { i18nT } from '../i18n/t'
import type { ReportError } from './useMarkdownPanelFileState'

/**
 * `MarkdownPanel`'s two answers about the file behind the buffer.
 *
 * `useMarkdownPanelDisk` is the disk truth: the panel's own reads (only the
 * newest may land), the catch-up latch and the live watch of the visible tab,
 * the missing-file state with its retry, and the last-copy rescue (download and
 * copy of a buffer whose file is gone). `useMarkdownPanelDiff` is the working
 * tree against HEAD: the diff read, git's own patch for a partial buffer, the
 * no-change and no-baseline states, and the one-time auto-open of diff mode.
 * The panel calls each once per render and keeps the edit, save and close
 * paths that act on them.
 */

/** Outcome of one of this panel's own disk reads (Refresh, Cancel, the watch).
 *  `superseded` means a newer read of this panel started before this one
 *  answered, or the panel moved to another path: apply nothing, say nothing. */
type DiskRead =
  | { kind: 'ok'; text: string; binary: boolean; partial: boolean }
  /** The file is not on disk (404): a real answer, rendered as the same
   *  placeholder the chip click and the cold-tab hydration show for it. */
  | { kind: 'missing' }
  | { kind: 'failed' }
  | { kind: 'superseded' }

/** How often a visible tab whose file is gone asks the disk whether it is
 *  back: `api_file_watch`'s own poll cadence (1 s), so a recreated file lands
 *  as fast as a change would through the stream the tab cannot hold meanwhile. */
export const MISSING_FILE_RETRY_MS = 1_000
/** The ceiling the retry backs off to: each attempt doubles the wait, so a
 *  file that stays gone costs one GET (and one SEL `not_found` record) every
 *  30 s, not every second, until the tab is hidden or the file is back. */
export const MISSING_FILE_RETRY_MAX_MS = 30_000

export function useMarkdownPanelDisk({ filePath, content, binary, partial, liveWatch, editing, dirty, dirtyRef, active, qc, onDiskContent, onContentChange, reportActionError }: {
  filePath: string; content: string; binary?: boolean; partial?: boolean; liveWatch?: boolean
  editing: boolean; dirty: boolean; dirtyRef: MutableRefObject<boolean>; active: boolean; qc: QueryClient
  onDiskContent?: (c: string, binary?: boolean, partial?: boolean) => void
  onContentChange: (c: string) => void
  reportActionError: ReportError
}) {
  // Mirrors like the panel's `dirtyRef`, for the buffer and its verdict: a disk read that lands
  // after its await compares against what the tab shows NOW.
  const contentRef = useRef(content)
  contentRef.current = content
  const binaryRef = useRef(!!binary)
  binaryRef.current = !!binary
  /** Read the file as it is now, with the verdict the read produced.
   *
   * ALWAYS a fresh fetch of the disk. That is the one rule every read of this
   * panel rests on -- the catch-up on a mount, a remount or a return, the
   * post-edit read a save or an undo starts, Refresh, Cancel, the watch's
   * re-read: each exists to see the file as it is NOW, and any body that is
   * not this fetch's can predate the change it must find. Not the read that
   * opened the tab (a file deleted between that GET and the mount left the tab
   * counted as caught up while its stream 404'd into a silent terminal error,
   * and the edit and save that followed recreated the deleted file from the
   * stale copy); not a `['file-read', path]` entry still inside its 10 s
   * window (after a save it holds the pre-save body); not a fetch already in
   * flight for an older moment. The mount right after a chip open costs one
   * GET again -- the price of a buffer the disk vouches for.
   *
   * Only the NEWEST of this panel's reads may land: each read aborts the one
   * before it and answers `superseded` if a later one started while it was in
   * flight, so an older re-read cannot put back bytes -- or a verdict -- that
   * a newer one has already replaced. Reporting stays with each caller because
   * only it knows whether a human is waiting on the result. */
  const diskReadAbortRef = useRef<AbortController | null>(null)
  /** Reads of this panel not yet settled. The watch consults it: an equal frame
   *  is quiet only when no read is in flight (see the callback). */
  const diskReadsInFlightRef = useRef(0)
  /** Orders this panel's disk reads against its diff fetches. Each read takes
   *  the next tick; each diff fetch records the tick it STARTED at (see
   *  useMarkdownPanelDiff), so a fetch that started at or after a read's tick
   *  saw the disk no older than it. */
  const diskTickRef = useRef(0)
  const diffStartTickRef = useRef(-1)
  /** The newest partial read the patch must be at least as new as. State, not
   *  a ref: a read that lands equal to the buffer renders nothing else, and the
   *  diff hook must still see it to schedule the refetch. */
  const [patchOwedTick, setPatchOwedTick] = useState(-1)

  const readFromDisk = useCallback(async (): Promise<DiskRead> => {
    diskReadAbortRef.current?.abort()
    const ac = new AbortController()
    diskReadAbortRef.current = ac
    // A purge of document bodies (the owner's redaction switch flipped) between
    // start and finish makes this read's bytes stale under the pass now in
    // force: superseded, never applied. The tab rehydrates through a fresh read.
    const epoch = documentBodyEpochNow()
    const tick = ++diskTickRef.current
    diskReadsInFlightRef.current++
    try {
      const r = await fetchFileRead(filePath, ac.signal)
      if (ac.signal.aborted || documentBodyEpochNow() !== epoch) return { kind: 'superseded' }
      if (r.ok) {
        const partial = isPartialRead(r)
        // A partial buffer's diff is git's patch of the file, and the file can
        // change past the cut while the cut body stays equal. So the patch
        // is owed from this read on; useMarkdownPanelDiff fetches it (see
        // there), unless a diff fetch already started after the read began.
        if (partial && diffStartTickRef.current < tick) setPatchOwedTick(tick)
        return { kind: 'ok', text: r.text, binary: r.binary, partial }
      }
      return r.status === 404 ? { kind: 'missing' } : { kind: 'failed' }
    } catch {
      return ac.signal.aborted ? { kind: 'superseded' } : { kind: 'failed' }
    } finally {
      diskReadsInFlightRef.current--
    }
  }, [filePath])

  // A path change or unmount withdraws the read in flight, so it cannot apply
  // or report its result to a tab that has moved on.
  useEffect(() => () => {
    diskReadAbortRef.current?.abort()
    diskReadAbortRef.current = null
  }, [filePath])

  /** The file is not on disk (a read answered 404), whatever the buffer holds.
   *  This is what parks the stream (a 404 stream closes for good) and runs the
   *  retry, so a recreated file lands even when the buffer is empty or the
   *  host's placeholder. Whether the buffer is a LAST COPY worth a banner and a
   *  close guard is a separate question, answered by `lastCopy` below. Cleared
   *  by any read that finds the file, and by a re-target of the tab. */
  const [missingOnDisk, setMissingOnDisk] = useState(false)
  useEffect(() => { setMissingOnDisk(false) }, [filePath])
  /** Whether the buffer holds only PART of the file: the read that filled it
   *  was cut at the gateway's cap or had credentials redacted. The verdict is
   *  the host's (`partial`), stamped from the read's headers by whichever read
   *  filled the buffer -- the opening read before this panel mounted as much as
   *  the panel's own -- and never guessed from the body: a whole file may quote
   *  the redaction tag, and a guess about it could only be wrong. Such a buffer
   *  is not the whole file: the banner and the close prompt say so instead of
   *  calling it the file's remaining contents, and a download of it is named
   *  `.partial`. That is ALL the verdict changes -- words and a file name. The
   *  guard, the banner and the exits stand for a partial buffer exactly as for
   *  a whole one: it is the most of the file that survives. */
  const partialCopy = !!partial
  const partialCopyRef = useRef(partialCopy)
  partialCopyRef.current = partialCopy
  /** Whether the buffer IS a copy worth keeping once the file is gone: text the
   *  disk once vouched for. Not the "file not found" sentence the host seeds
   *  into a tab opened on a path already missing (`openFile`, the cold-tab
   *  hydration), not a binary tab's empty text buffer, not an empty file --
   *  a banner over those would offer to download the placeholder prose, or
   *  nothing, under the file's name. Judged by the buffer, not by whether THIS
   *  panel instance has seen a successful read: a panel remounted after the
   *  side panel was collapsed carries the tab store's copy of a file deleted
   *  meanwhile -- a real last copy with no successful read behind it here. */
  const bufferIsLastCopy = useCallback(() =>
    !binaryRef.current && contentRef.current !== '' &&
    contentRef.current !== i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or'), [])
  /** The banner and the close guard: the file is gone AND the buffer is a copy
   *  worth keeping. Rendered as a banner OVER the document, never in the
   *  buffer: `content` and its saved baseline keep the last text on screen. */
  const lastCopy = missingOnDisk && !binary && content !== '' &&
    content !== i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
  /** The banner's two exits, on the BUFFER: a gone file has no path to fetch,
   *  so the browser is handed the text on screen, under the file's name. The
   *  copy answers: "Copied" on the button for a moment, or the panel's action
   *  notice when the clipboard refused -- a silent miss here would sit right
   *  next to the invitation to discard the only copy. */
  const downloadLastCopy = useCallback(() => {
    // A partial copy goes out under a name that says so, never under the
    // file's own: with the original gone, that would pass a prefix off as it.
    downloadBlob(new Blob([content], { type: 'text/plain;charset=utf-8' }), downloadFileName(filePath) + (partialCopy ? '.partial' : ''))
  }, [content, filePath, partialCopy])
  const [lastCopyCopied, setLastCopyCopied] = useState(false)
  const lastCopyCopiedTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(lastCopyCopiedTimer.current), [])
  const copyLastCopy = useCallback(async () => {
    if (!(await copyToClipboard(content))) { reportActionError(i18nT('components.markdownPanel.copy_failed')); return }
    setLastCopyCopied(true)
    clearTimeout(lastCopyCopiedTimer.current)
    lastCopyCopiedTimer.current = setTimeout(() => setLastCopyCopied(false), 1500)
  }, [content, reportActionError])

  /** Drop the `['file-read', path]` cache entry rather than rewrite it, so a
   *  reopen inside its 10 s window refetches instead of rehydrating a body a
   *  disk read has just proven stale -- open, then save within the window, is
   *  exactly that. Every read this panel makes drops it, restamped or not. */
  const evictFileRead = useCallback(() => {
    qc.removeQueries({ queryKey: fileReadQueryKey(filePath), exact: true })
  }, [qc, filePath])

  /** Apply a disk read to the tab: buffer, saved baseline and verdict together,
   *  with the cache entry dropped. A body landed, so the file is there. */
  const applyDiskRead = useCallback((disk: { text: string; binary: boolean; partial: boolean }) => {
    evictFileRead()
    setMissingOnDisk(false)
    if (onDiskContent) onDiskContent(disk.text, disk.binary, disk.partial)
    else onContentChange(disk.text)
  }, [evictFileRead, onDiskContent, onContentChange])


  /** The catch-up latch: where this tab stands against its file since it last
   *  became visible. `due` -- a read has to run at the next clean moment: the
   *  tab is hidden, the user has edited a clean buffer (handleChange), the tab
   *  has left edit mode (the stretch it held no stream for), or the read it
   *  started was overtaken by the user's typing and dropped. `reading`
   *  -- one is in flight, and a re-render starts no second one. `done` --
   *  nothing to do until the tab is hidden or edited again: a read applied, or
   *  failed and was reported. Every mount starts `due`, a visible one too: the
   *  buffer a remounted tab is handed is the tab store's copy, which may
   *  predate a change made while the panel was unmounted, and the re-armed
   *  stream's first frame does not cover a file emptied meanwhile
   *  (`api_file_watch` emits only when content differs from its initial empty
   *  string). One GET per mount of the visible tab buys a buffer the disk
   *  vouches for. */
  const catchUpRef = useRef<CatchUpLatch>('due')

  /** Adopt the disk truth: THE catch-up procedure, shared by the activation
   *  effect, the watch and every clean moment that owes a read.
   *
   * One procedure, one rule. The read is always `readFromDisk` -- a fresh fetch
   * of the file as it is now, never the read that opened the tab, never a
   * cached body, never skipped for a mount, a remount or an edit edge -- and
   * the tab counts as caught up only inside the branch that adopts THAT
   * fetch's answer, right here. Nothing else writes `done`: the effect below
   * and the edit fences only ever move the latch to `due` or `reading`.
   *
   * A change on disk IS the truth, so it goes through onDiskContent when the
   * host can restamp its saved baseline; falling back to onContentChange keeps
   * non-tab hosts unchanged. Three results are dropped rather than applied:
   * superseded by a later read, or the user started typing while it was in
   * flight -- two ways this result has nothing to say -- and an unreadable file,
   * which is NOT quiet: the panel is then showing a revision it knows is
   * superseded and cannot describe the current one, which the user has to be
   * told rather than left to infer from a document that stopped changing.
   *
   * The catch-up latch moves on the read's OUTCOME, here, never on its start.
   * Only an applied read makes the tab current, so `reading` becomes `done`
   * once the bytes are on screen. A read the typing overtook puts the tab
   * back to `due`: the buffer it would have replaced is dirty now, and once
   * those edits are undone it is a clean buffer at the pre-change revision,
   * which a later save would write over the newer file -- so the next clean
   * moment re-reads instead of trusting it. A superseded read says nothing;
   * the newer read that overtook it decides. A failed read is reported, and
   * counts as this return's attempt (`done`) rather than staying `due`: the
   * host hands the panel fresh callbacks on every render, so a tab left `due`
   * would re-read and re-report as fast as the host renders. The next return
   * tries again.
   *
   * A file that is gone (404) is not a failure: it is a real answer, and the
   * chip click, the cold-tab hydration and the reload all render a tab opened
   * on a missing path as a placeholder -- so the catch-up says so too, quietly,
   * instead of raising the read-failure notice on every return to that tab. As
   * a STATE, not as content: the buffer and its saved baseline keep the last
   * text, which may be the only copy left of a file deleted outside the
   * dashboard, and that copy stays on screen under a banner that says what it
   * is and carries the two ways to keep it. Only when the buffer IS such a copy
   * (bufferIsLastCopy): a tab seeded with the host's placeholder sentence, or
   * a binary tab, has nothing the banner could truthfully call a last copy. A
   * read that finds the file again clears the banner. */
  const adoptDisk = useCallback(async (opts?: { quietFailure?: boolean }): Promise<DiskRead['kind']> => {
    const disk = await readFromDisk()
    if (disk.kind === 'superseded') return disk.kind
    // Whatever this read answered, the shared cache entry for the path does
    // not outlive it: a body older than the disk (open, then save within the
    // 10 s window) would rehydrate on a reopen, and a cached 404 or failure
    // would answer the next open.
    evictFileRead()
    // `missingOnDisk` is a fact about the FILE, not an edit to the buffer, so
    // it is recorded before the dirty early-return below: a 404 latches it and
    // a body clears it, dirty or not. The early-return protects the EDITS --
    // a dirty tab adopts nothing -- and the flag touches none of them; left
    // false for a dirty tab, the banner that names the gone file and the
    // Download that keeps the edits would both stay off exactly when the
    // buffer is the only place those edits exist.
    if (disk.kind === 'missing') setMissingOnDisk(true)
    else if (disk.kind === 'ok') setMissingOnDisk(false)
    if (dirtyRef.current) { catchUpRef.current = 'due'; return disk.kind }
    if (disk.kind === 'failed') {
      settleCatchUp(catchUpRef)
      // The retry reports its first failure and is quiet after: the banner
      // already stands, and a notice per tick would only repeat it.
      if (!opts?.quietFailure) reportActionError(i18nT('components.markdownPanel.cannot_read_file'))
      return disk.kind
    }
    // The disk says what the tab already shows -- the common outcome of a
    // mount or a return to an unchanged file: current, nothing to restamp.
    if (disk.kind === 'ok' && (disk.text !== contentRef.current || disk.binary !== binaryRef.current || disk.partial !== partialCopyRef.current)) applyDiskRead(disk)
    settleCatchUp(catchUpRef)
    return disk.kind
  }, [applyDiskRead, evictFileRead, readFromDisk, reportActionError, dirtyRef])
  const adoptDiskRef = useRef(adoptDisk)
  adoptDiskRef.current = adoptDisk

  // The watch tracks the disk for a clean, non-editing tab -- and ONLY the tab
  // the user can see. Every open tab used to arm its own watch, and each watch
  // is one EventSource: one HTTP/1.1 connection held open for as long as the
  // tab exists. Chromium allows six connections per host, so six idle tabs
  // starved every other request the dashboard makes -- uploads, chat sends,
  // polls -- for as long as the streams stayed open (#14236). A hidden tab holds
  // no stream; it catches up in the effect below when it comes back. Nor does a
  // tab whose file is gone: `api_file_watch` answers 404 for a missing path and
  // `useFileWatch` closes such a stream for good, so a watch armed while the
  // file was gone would never see it recreated. The retry below covers that
  // stretch, and the read that finds the file again arms the stream afresh.
  const watchArmable = !!liveWatch && !editing && !dirty && !missingOnDisk
  const onWatchFrame = useCallback(
    // The event's own `content` is never APPLIED. `/api/file-watch` decodes
    // with `errors="replace"` and carries no binary verdict, so a file replaced
    // on disk by binary bytes would push U+FFFD into the buffer AND leave the
    // card decision at its hydrated value — editor live, save destructive. The
    // event says "it changed"; the re-read says what it now is, and if the
    // re-read cannot answer then NOTHING is applied: the buffer stays at the
    // last revision whose verdict is known, which is stale but honest, where
    // the event body would be new content under an old verdict.
    //
    // It IS compared. The backend starts a stream from mtime 0, so a re-armed
    // stream's first poll tick always writes a frame with the whole body, and
    // on an activation that frame arrives beside the catch-up read; re-reading
    // on it made every tab switch two GETs. A frame whose text equals the clean
    // buffer says nothing the tab does not already show and costs no read --
    // unless a read is still in flight: that read may land with OLDER bytes
    // than this frame (the file changed and changed back between the two), and
    // equality with the displayed text would let the stale landing become the
    // baseline, so the frame supersedes the read instead. Any difference
    // re-reads -- a real change, a CRLF file (the stream's text-mode read
    // normalizes newlines, the re-read does not), a body past the stream's
    // cap -- and so does every frame on a binary tab, whose text buffer is
    // empty: equality with no read pending is the ONLY case skipped, and equal
    // text is nothing to adopt. One GET per activation of the one file on
    // screen, on a subscription only armed while the panel is visible, clean
    // and not editing.
    //
    // A PARTIAL buffer is the exception after the first frame. The stream
    // reads under the same cap, so a write past the cut sends a frame whose
    // text equals the buffer (or is empty, a signal-only frame); it still
    // follows a write, and the re-read is what refetches git's patch (see
    // readFromDisk). Only the connect-time frame, which the activation read
    // already covers, is compared.
    (frameText: string, { initial }: FileWatchFrame) => {
      const comparable = initial || !partialCopyRef.current
      if (comparable && diskReadsInFlightRef.current === 0 && !binaryRef.current && frameText === contentRef.current) return
      void adoptDisk()
    },
    [adoptDisk],
  )

  // The catch-up for a parked watch: a tab that was hidden re-reads its file
  // when it becomes the visible one, so a change made while it held no stream
  // lands the moment the user can see the tab, not never -- silently stale
  // would be the failure, because the saved baseline still looks current. The
  // same guards as the watch apply through adoptDisk: a dirty tab keeps its
  // edits and does not re-read (the edits are the user's work), and a read
  // that typing or a newer read has overtaken is dropped. `editing` gates the
  // stream, not this read: a live push would land under the user's cursor, but
  // a tab that was hidden has no cursor in it, and a clean editor buffer left
  // stale is exactly what a later save would write over the newer file. The
  // tab counts as caught up only once a read has APPLIED -- adoptDisk settles
  // the latch, this effect only starts the read: one that comes back with
  // unsaved edits reads nothing, and reads the moment those edits are undone,
  // saved or discarded while it is visible -- undone in particular, since that
  // leaves a clean buffer showing the pre-change revision -- and so does one
  // whose read the user's typing overtook. A mount reads too (the latch starts
  // `due`; see it). Leaving edit mode while visible re-arms the watch, and
  // re-arms this read as well: a clean editor buffer held no stream while it
  // was being edited, so a change made then reached the tab only through the
  // re-armed stream's connect-time frame -- and there is no such frame for a
  // file emptied meanwhile (`api_file_watch` compares against its initial
  // empty string). The read does not wait for the re-armed stream to say
  // anything: a saturated connection budget is exactly what delays that
  // stream, and the tab must be current whether or not its stream connects.
  const catchUpArmable = !!liveWatch && !dirty
  useVisibleTabCatchUp({
    path: filePath,
    active,
    watchArmable,
    catchUpArmable,
    editing,
    latch: catchUpRef,
    catchUp: adoptDisk,
    onFrame: onWatchFrame,
  })

  // While the file is gone, the stream cannot stand in for the disk (see the
  // watch above), so the visible tab asks the disk itself, on the cadence the
  // backend's own watch polls at. The retry runs only while the banner shows,
  // the tab is on screen and the buffer is clean -- a dirty buffer is the
  // user's work, and adoptDisk would drop the result anyway -- and the read
  // that finds the file clears `missingOnDisk`, which ends the retry and arms
  // the stream again. One GET a second for the one deleted file on screen: the
  // alternative is a banner that keeps saying "not found" over a buffer a save
  // would write over the recreated file. Each retry is scheduled only once the
  // previous read has SETTLED, never on a fixed interval: a read slower than
  // the cadence would otherwise be aborted by the next tick, every tick, and a
  // recreated file would never land.
  useEffect(() => {
    if (!missingOnDisk || !active || !catchUpArmable) return
    let stopped = false
    let delay = MISSING_FILE_RETRY_MS
    let reported = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      // Never over a read already out -- the activation catch-up, the close
      // guard, the watch: starting one would abort it. Skip the tick instead.
      // Through a ref: the host hands the panel fresh callbacks on every
      // render, and an effect keyed on adoptDisk itself would restart before
      // its first read went out.
      if (diskReadsInFlightRef.current === 0) {
        const verdict = await adoptDiskRef.current({ quietFailure: reported })
        if (verdict === 'failed') reported = true
      }
      delay = Math.min(delay * 2, MISSING_FILE_RETRY_MAX_MS)
      if (!stopped) timer = setTimeout(() => { void tick() }, delay)
    }
    timer = setTimeout(() => { void tick() }, delay)
    return () => { stopped = true; clearTimeout(timer) }
  }, [missingOnDisk, active, catchUpArmable])

  return {
    diskReadAbortRef, readFromDisk, missingOnDisk, setMissingOnDisk, partialCopy, partialCopyRef, bufferIsLastCopy,
    lastCopy, downloadLastCopy, lastCopyCopied, copyLastCopy, evictFileRead, applyDiskRead, catchUpRef, adoptDisk,
    diskTickRef, diffStartTickRef, patchOwedTick,
  }
}

export function useMarkdownPanelDiff({ filePath, isRichType, canTextDiff, partial, editing, dirty, diffMode, content, initialDiffMode, onDiffModeChange, setDiffMode, diskTickRef, diffStartTickRef, patchOwedTick, qc }: {
  filePath: string; isRichType: boolean; canTextDiff: boolean; partial?: boolean; editing: boolean; dirty: boolean; diffMode: boolean; content: string
  initialDiffMode?: boolean; onDiffModeChange?: (diffMode: boolean) => void; setDiffMode: Dispatch<SetStateAction<boolean>>
  /** The disk hook's read/fetch ordering (see readFromDisk): each diff fetch
   *  records the read tick it started at. */
  diskTickRef: MutableRefObject<number>; diffStartTickRef: MutableRefObject<number>
  /** The newest partial read git's patch must be at least as new as. */
  patchOwedTick: number; qc: QueryClient
}) {
  const diffInitFileRef = useRef<string | null>(null)
  // Detect if file has uncommitted changes and pre-fetch HEAD content
  const { data: diffData, isFetching: diffChecking, error: diffQueryError } = useQuery({
    queryKey: ['file-diff', filePath],
    queryFn: () => { diffStartTickRef.current = diskTickRef.current; return api.fileDiff(filePath) },
    // A rich type opens on its viewer; only a tab opened in diff mode (the
    // working-tree diff) needs its baseline, and only if it can text-diff.
    enabled: !!filePath && (!isRichType || (diffMode && canTextDiff)),
    staleTime: 10_000,
  })
  // An owed patch is fetched only while diff mode shows it, and only once no
  // fetch is out: one fetch at a time, the next started when it settles, so a
  // file written every second costs at most one /api/file-diff in flight and
  // a fetch is never cancelled before it can land. Turning diff mode on
  // fetches whatever came due while it was off.
  useEffect(() => {
    if (!diffMode || diffChecking || diffStartTickRef.current >= patchOwedTick) return
    void qc.invalidateQueries({ queryKey: ['file-diff', filePath] })
  }, [diffMode, diffChecking, patchOwedTick, filePath, qc, diffStartTickRef])
  const originalContent = diffData?.original ?? ''
  // `original` has LF line endings only: the gateway reads git's output with
  // universal newlines. The buffer keeps the file's own CRLF or lone CR, so
  // the body-pair diff compares HEAD with a copy of the buffer read the same
  // way, or such a file diffs as every line changed. Only the comparison reads
  // this copy: the buffer, and so what a save writes, is untouched.
  const comparedContent = useMemo(() => content.replace(/\r\n?/g, '\n'), [content])
  // A partial buffer (capped, redacted or lossily decoded read) is not the file
  // on disk, so comparing it with the WHOLE HEAD body invents changes: past a
  // capped read's end every HEAD line reads as deleted, and the real edits
  // there vanish. Git's own diff of the file is the truth then -- draw it and
  // count it instead. A whole buffer keeps the body-pair diff, which follows
  // the buffer live. Unsaved edits keep it too: git's patch cannot show edits
  // that are not on disk yet. Git's patch serves the viewer and the editor
  // alike -- a code file opens in the editor, so diff mode there draws this
  // patch in place of the editor's live diff. ONE value carries the choice:
  // non-null means "git's patch", '' a clean file, and every consumer (notice,
  // count, view) reads it. It stays null until diff data has arrived: a fetch
  // still out or failed must not swap the editor for an empty patch (the diff
  // error notice reports a failure).
  const gitPatch = partial && !dirty && diffData ? (diffData.diff ?? '') : null
  // The backend reports WHY there is nothing to diff against. `not_git` means
  // the file is outside any git work tree, so there is no baseline at all —
  // rendering the diff would present the whole file as added against a
  // baseline that does not exist ("untracked" keeps the all-added rendering:
  // there the repo IS the baseline and all-added is the true answer). `error`
  // means git itself failed, which must stay distinguishable from a clean
  // file's empty diff. Deliberately NOT gated on diffChecking: a background
  // refetch (window refocus past staleTime) keeps the cached data while
  // isFetching is true, and collapsing this state mid-refetch would flash the
  // exact all-added rendering this branch exists to suppress.
  const diffUnavailable = diffMode && (diffData?.status === 'not_git' || diffData?.status === 'error')
    ? diffData.status as 'not_git' | 'error' : null
  // Diff mode draws the diff view, not the editor: always in the viewer, and
  // in the editor when the diff is git's patch, once it has arrived (and git
  // could produce one).
  const diffViewShown = diffMode && (!editing || (gitPatch != null && !diffUnavailable))
  // Diff mode on a file identical to its baseline renders an empty canvas in
  // BOTH view and edit modes — show a notice instead. Editing past the
  // baseline (content diverges) flips this off automatically.
  const zeroDiff = diffMode && !diffChecking && diffData != null && !diffUnavailable
    && (gitPatch != null ? !gitPatch : originalContent === comparedContent)
  // `status` comes from git's diff of the raw file, the patch from the two
  // masked bodies. An empty patch on a `modified` file is a change the patch
  // cannot draw: inside a masked credential, or in the file mode or line
  // endings. "No changes" would be false there, so the notice says git saw a
  // change, naming no cause and no line (a line would point at the secret that
  // changed). A whole buffer is compared line by line, so its equal bodies do
  // mean unchanged text and keep "no changes".
  const zeroDiffText = zeroDiff && gitPatch != null && diffData?.status === 'modified'
    ? i18nT('components.markdownPanel.diff_change_not_shown') : undefined
  // Resolved in render position so a language switch re-renders it; the notice
  // reuses ZeroDiffNotice so the "Show full file" escape hatch is identical
  // across all three empty-diff states.
  const diffUnavailableText = diffUnavailable === 'not_git'
    ? i18nT('components.markdownPanel.diff_no_baseline_not_git')
    : diffUnavailable === 'error'
      ? i18nT('components.markdownPanel.diff_failed_to_compute')
      : null
  // `not_git` is a benign "no baseline here" state (informational), but `error`
  // means git itself failed computing the diff — a real failure that must reach
  // the shared ErrorNotice (and its agent hand-off), not be shown as muted text
  // like the no-change / no-baseline notices (errors-use-error-notice). Split
  // the two: the error text renders through ErrorNotice, the not_git text keeps
  // the lightweight ZeroDiffNotice.
  const diffErrorText = diffUnavailable === 'error' ? diffUnavailableText : null
  const diffNoBaselineText = diffUnavailable === 'not_git' ? diffUnavailableText : null
  // Auto-open diff mode once for a genuine edit unless this file tab already
  // carries an explicit choice. File-tab metadata survives ChatPage unmounts,
  // so returning to a session restores preview/source instead of re-enabling
  // diff after the user turned it off.
  useEffect(() => {
    if (!canTextDiff || !diffData || diffInitFileRef.current === filePath) return
    diffInitFileRef.current = filePath
    if (initialDiffMode !== undefined) {
      // A rich type fetches its baseline only once something asked for its
      // diff, so this first response always follows that request: applying a
      // `false` here would switch the diff straight back off.
      if (!(isRichType && !initialDiffMode)) setDiffMode(initialDiffMode)
      return
    }
    // A rich type opens on its viewer: only an explicit diff request (the
    // working-tree diff tab) shows its text diff.
    if (!isRichType && diffData.diff && diffData.status === 'modified') {
      setDiffMode(true)
      onDiffModeChange?.(true)
    }
  }, [canTextDiff, isRichType, diffData, filePath, initialDiffMode, onDiffModeChange, setDiffMode])

  return { diffData, diffChecking, diffQueryError, originalContent, comparedContent, gitPatch, diffUnavailable, diffViewShown, zeroDiff, zeroDiffText, diffErrorText, diffNoBaselineText }
}
