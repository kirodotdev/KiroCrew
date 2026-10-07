// The meeting note's DRAFT: the text the user is typing, and the state machine that
// gets it onto disk.
//
// This lives in a hook ABOVE the panel rather than in the panel's own state, and
// the reason is the failure that motivated it: `NoteSidebar` is unmounted when the
// user closes it, so a draft held in component state died with the panel. Its
// unmount flush sent one PUT and then the text was gone — if that PUT was refused,
// the only copy of the note was lost with a transient toast as its epitaph. Here the
// draft outlives the panel: closing it flushes, a refusal leaves the draft dirty and
// the failure visible, and reopening shows the text that never landed.
//
// The draft lives exactly as long as the meeting view does, and no longer. Leaving
// the view goes through `exit`, which saves and reports whether the save landed, so
// the view can refuse to navigate away from text that is not on disk. Nothing is
// retained past the hook's own lifetime: an earlier design kept a module-level copy
// for a refused exit save, and every defect found in it since was a lost update
// inside that mechanism rather than in the note itself.
//
// Two invariants the design holds, both about ORDER:
//
// * `baseline` is what the server has CONFIRMED, advanced when a save LANDS, never
//   when it is sent. So a response for an older save can never replace newer typing
//   (a server value is adopted only while the draft still equals the baseline), and
//   a refused save leaves the draft dirty until one succeeds — blur, preview, closing
//   the panel and the debounce all retry it.
// * Every pasted image reserves its position with a unique placeholder. Uploads may
//   resolve in any order; each result replaces only its own token, and no save ever
//   carries a token: a flush waits while an upload is pending, and the exit flush
//   saves the text with any unresolved token removed.

import { useCallback, useEffect, useRef, useState } from 'react'

import { MeetingsApiError } from '../api'
import { imageSnippet, insertBlock } from '../noteText'

// Private-use characters, so a token can never collide with anything the user
// types. Numbered so two concurrent pastes cannot share one.
let nextImagePlaceholder = 0

function insertPlaceholder(text: string, caret: number, token: string): string {
  const at = Math.max(0, Math.min(caret, text.length))
  return `${text.slice(0, at)}${token}${text.slice(at)}`
}

function replacePlaceholder(text: string, token: string, snippet: string | null): string {
  const at = text.indexOf(token)
  if (at < 0) return text
  const before = text.slice(0, at)
  const after = text.slice(at + token.length)
  if (snippet === null) return `${before}${after}`
  return insertBlock(`${before}${after}`, before.length, snippet)
}

const IMAGE_PLACEHOLDER_PATTERN = /\uE000\d+\uE001/gu
// A token the user partly edited leaves a lone delimiter behind, which the whole-
// token pattern does not match. Its digits are ordinary text and stay.
const IMAGE_PLACEHOLDER_DELIMITER = /[\uE000\uE001]/gu

function stripImagePlaceholders(text: string): string {
  return text.replace(IMAGE_PLACEHOLDER_PATTERN, '').replace(IMAGE_PLACEHOLDER_DELIMITER, '')
}

/** Quiet period after the last keystroke before a save fires. */
export const SAVE_DEBOUNCE_MS = 800

export type NoteUploadError = 'tooLarge' | 'failed'

interface Options {
  /** Resets the draft when it changes: a different meeting is a different note. */
  meetingId: string
  /** Server content once the GET has answered; `undefined` while it has not. */
  serverContent: string | undefined
  /** The GET failed. Every save path stays shut: the field is empty because the note
   *  could not be read, not because it is empty, and saving would overwrite it. */
  loadFailed: boolean
  /** Sends one save. Resolves when the server ACKNOWLEDGED it, rejects on refusal. */
  save: (content: string) => Promise<unknown>
  /** Stores one pasted image, resolving with the markdown pieces to insert. */
  uploadImage: (file: File) => Promise<{ alt: string; src: string }>
}

export interface NoteDraft {
  draft: string
  /** The draft differs from what the server has confirmed. */
  dirty: boolean
  /** The first GET has answered, so the field may be edited. */
  loaded: boolean
  saving: boolean
  /** The most recent save was refused and nothing has landed since. */
  saveFailed: boolean
  uploading: boolean
  /** The most recent paste was refused; cleared by the next edit or paste. */
  uploadError: NoteUploadError | null
  change: (value: string) => void
  /** Save now if anything is unsaved. Blur, the preview toggle and closing the panel. */
  flush: () => void
  /**
   * The save that precedes leaving the meeting view. Unlike `flush` it does not
   * wait for a pending upload — the text is saved with the unresolved placeholder
   * removed — and it resolves once the outcome is known: `true` when the note is on
   * disk (or there was nothing to save), `false` when the save was refused, in
   * which case the draft is still here, dirty, with `saveFailed` set.
   */
  exit: () => Promise<boolean>
  /** Upload a pasted image and insert its markdown at *caret* once stored. */
  pasteImage: (file: File, caret: number) => void
}

export function useNoteDraft({
  meetingId,
  serverContent,
  loadFailed,
  save,
  uploadImage,
}: Options): NoteDraft {
  const [draft, setDraftState] = useState('')
  // What the server has confirmed. `null` until the first GET answers — before that
  // there is nothing to compare against and the field is read-only.
  const [baseline, setBaselineState] = useState<string | null>(null)
  const [pendingSaves, setPendingSaves] = useState(0)
  const [saveFailed, setSaveFailed] = useState(false)
  const [pendingUploads, setPendingUploads] = useState(0)
  const [uploadError, setUploadError] = useState<NoteUploadError | null>(null)

  // Refs mirror the two pieces of state `flush` compares, and are written EAGERLY
  // by the setters below rather than on the next render: a flush can run inside the
  // same tick as the change that made the text dirty (a blur right after an unmount
  // effect, say), and it has to see that change.
  const draftRef = useRef(draft)
  const baselineRef = useRef(baseline)
  const setDraft = (value: string) => {
    draftRef.current = value
    setDraftState(value)
  }
  const setBaseline = (value: string | null) => {
    baselineRef.current = value
    setBaselineState(value)
  }
  const meetingIdRef = useRef(meetingId)
  const loadFailedRef = useRef(loadFailed)
  loadFailedRef.current = loadFailed
  const saveRef = useRef(save)
  saveRef.current = save
  const uploadRef = useRef(uploadImage)
  uploadRef.current = uploadImage
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The save most recently SENT, so a flush does not resend a body that is already
  // on the wire, and so `exit` can await it instead. Distinct from the baseline on
  // purpose: the baseline is what LANDED.
  const inFlightRef = useRef<{ text: string; done: Promise<boolean> } | null>(null)
  // Sends are numbered and an acknowledgement only advances the baseline when it is
  // the newest one seen. Saves are serialized upstream, so in practice they land in
  // order; this is what keeps the invariant true even if they did not.
  const sendSeqRef = useRef(0)
  const ackedSeqRef = useRef(0)
  // The placeholders of THIS instance's unresolved uploads. Instance-local on
  // purpose: an upload belongs to the hook that started it, and the hook lives as
  // long as the meeting view.
  const pendingTokensRef = useRef(new Set<string>())

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }

  useEffect(() => {
    if (meetingIdRef.current !== meetingId) {
      // A different meeting is a different note: nothing typed into one may leak
      // into another's first save, and no in-flight bookkeeping carries over.
      meetingIdRef.current = meetingId
      clearTimer()
      inFlightRef.current = null
      sendSeqRef.current = 0
      ackedSeqRef.current = 0
      pendingTokensRef.current.clear()
      setPendingSaves(0)
      setPendingUploads(0)
      setSaveFailed(false)
      setUploadError(null)
      setDraft(serverContent ?? '')
      setBaseline(serverContent ?? null)
      return
    }
    // Adopt a server value: the first load, or a genuinely external change (another
    // tab). Guarded on the BASELINE, not the draft, and only while the draft still
    // equals it — a draft the user has moved past is newer than anything the server
    // can send, so it is kept and the next flush saves it (last-write-wins, which is
    // the store's documented semantics). The baseline still advances, so `dirty`
    // compares against what is actually on disk.
    if (serverContent === undefined) return
    const previous = baselineRef.current
    if (serverContent === previous) return
    if (previous === null || draftRef.current === previous) {
      setDraft(serverContent)
    }
    setBaseline(serverContent)
  }, [meetingId, serverContent])

  const send = useCallback((text: string): Promise<boolean> => {
    const seq = ++sendSeqRef.current
    const sendMeetingId = meetingIdRef.current
    let settle: (landed: boolean) => void = () => {}
    const done = new Promise<boolean>(resolve => { settle = resolve })
    inFlightRef.current = { text, done }
    setPendingSaves(n => n + 1)
    void (async () => {
      let landed = false
      try {
        await saveRef.current(text)
        landed = true
        if (meetingIdRef.current === sendMeetingId && seq > ackedSeqRef.current) {
          ackedSeqRef.current = seq
          setBaseline(text)
          setSaveFailed(false)
        }
      } catch {
        // Left dirty on purpose: `baseline` did not move, so every later flush retries.
        if (meetingIdRef.current === sendMeetingId) setSaveFailed(true)
      } finally {
        if (meetingIdRef.current === sendMeetingId) {
          if (inFlightRef.current?.text === text) inFlightRef.current = null
          setPendingSaves(n => n - 1)
        }
        settle(landed)
      }
    })()
    return done
  }, [])

  /**
   * One flush. Resolves with whether the note is on disk afterwards: `true` when
   * the save landed or nothing needed saving, `false` when it was refused.
   *
   * `leaving` is the exit flush. A pending upload holds an ordinary flush back —
   * its placeholder must not reach disk, and the upload is about to resolve into a
   * hook that is still here — but on the way out of the meeting view there is no
   * "later", so the text is saved with the unresolved token removed instead of not
   * at all. The token stays in the FIELD in that case: a refusal keeps the user
   * here, and the upload may yet land in its place.
   */
  const flushImpl = useCallback((leaving: boolean): Promise<boolean> => {
    clearTimer()
    // Before the dirty check, not after: a note that failed to load (or has not
    // loaded) must not be written on ANY trigger, however the text compares.
    if (loadFailedRef.current || baselineRef.current === null) return Promise.resolve(true)
    const uploadsPending = pendingTokensRef.current.size > 0
    if (uploadsPending && !leaving) return Promise.resolve(true)
    const rawText = draftRef.current
    // The floor under the placeholder invariant: whatever the gates above missed, a
    // private-use character never reaches disk.
    const text = stripImagePlaceholders(rawText)
    if (text !== rawText && !uploadsPending) setDraft(text)
    if (text === baselineRef.current) return Promise.resolve(true)
    if (inFlightRef.current?.text === text) return inFlightRef.current.done
    return send(text)
  }, [send])

  const flush = useCallback(() => { void flushImpl(false) }, [flushImpl])
  const exit = useCallback(() => flushImpl(true), [flushImpl])

  const change = useCallback((value: string) => {
    setDraft(value)
    setUploadError(null)
    clearTimer()
    timerRef.current = setTimeout(flush, SAVE_DEBOUNCE_MS)
  }, [flush])

  const pasteImage = useCallback((file: File, caret: number) => {
    // Same gate as `flush`: a note that has not loaded, or failed to, takes no
    // paste — the upload would store an image the note can never legitimately
    // reference, and the insertion would dirty a draft that must not be saved.
    if (loadFailedRef.current || baselineRef.current === null) return
    setUploadError(null)
    const token = `\uE000${++nextImagePlaceholder}\uE001`
    pendingTokensRef.current.add(token)
    setPendingUploads(n => n + 1)
    change(insertPlaceholder(draftRef.current, caret, token))
    const finish = (snippet: string | null, error: NoteUploadError | null) => {
      // Not in the set means the meeting changed underneath this upload: the note
      // it was pasted into is no longer the one shown, so there is nothing to insert
      // into and nothing left to count down.
      if (!pendingTokensRef.current.delete(token)) return
      // `draftRef`, not the draft captured at paste time: the user may have kept
      // typing while the upload ran, and the panel may have closed — this hook is
      // still here either way, which is the point of owning the paste. Each result
      // replaces only its own token, so concurrent pastes land where each was made.
      const next = replacePlaceholder(draftRef.current, token, snippet)
      // Always, not only when the text changed: while this upload was pending every
      // debounce was held back, so edits made meanwhile — including ones that
      // removed or broke this token, which leave `next` unchanged — are saved only
      // if something re-arms the debounce now.
      change(next)
      if (error) setUploadError(error)
      setPendingUploads(n => n - 1)
    }
    void (async () => {
      try {
        const stored = await uploadRef.current(file)
        finish(imageSnippet(stored.alt, stored.src), null)
      } catch (error) {
        const tooLarge = error instanceof MeetingsApiError && error.status === 413
        finish(null, tooLarge ? 'tooLarge' : 'failed')
      }
    })()
  }, [change])

  // Unmounting is the last exit, and it saves too. The meeting view calls `exit`
  // BEFORE it lets the user leave and stays put on a refusal, so by the time this
  // runs the note is normally already on disk and this is a no-op; it is the floor
  // under the paths that unmount the view without asking (the browser leaving the
  // page, say). Nothing holds the draft past this point, so a refusal here is the
  // one accepted residual: the notification feed still reports it.
  const exitRef = useRef(exit)
  exitRef.current = exit
  useEffect(() => () => { void exitRef.current() }, [])

  return {
    draft,
    dirty: baseline !== null && draft !== baseline,
    loaded: baseline !== null,
    saving: pendingSaves > 0,
    saveFailed,
    uploading: pendingUploads > 0,
    uploadError,
    change,
    flush,
    exit,
    pasteImage,
  }
}
