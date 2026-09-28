import type React from 'react'
import { useEffect, useRef, useState } from 'react'
import { copyToClipboard } from '../../utils/clipboard'
import { InstantTip, useInstantTip } from '../InstantTip'
import ErrorNotice from '../ErrorNotice'

/**
 * The copy acknowledgment, shared by every chip that copies.
 *
 * One definition so the chips cannot drift on how long it lasts or whether it
 * appears at all — the session chip advertises Ctrl+click in its tooltip, so the
 * gesture owes the same confirmation the click-to-copy chip gives.
 *
 * `copy` is the ONLY write path: it writes, and confirms only when
 * `copyToClipboard` resolves true, per that helper's contract — a tick over an
 * unchanged clipboard is worse than no cue at all. A refused write reports
 * `failed` instead, which every caller renders through `CopyFailedNotice` in an
 * `InstantTip` bubble — the copy chip in the bubble that also carries its hint
 * and confirmation, the title-cued chips through `useTitleCuedCopy`. One
 * failure surface for one operation: two shapes of "Copy failed" (a bubble on
 * one chip, a red label pushed into the sentence on another) read as two
 * different features, and the in-flow one reflowed the line for as long as it
 * showed.
 *
 * Both outcomes are flashes that clear themselves: a confirmation already read
 * is noise, and the failure surface is a bubble with no dismiss control, so a
 * failure that never cleared would be a red mark the user could not remove.
 * The failure holds longer (`COPY_FAILED_FLASH_MS`): it is the outcome the user
 * did not expect and most needs — the text they asked for is NOT on their
 * clipboard — so it gets the time to be noticed and read.
 *
 * Each outcome REPLACES the other, never sits beside it. A press resolves
 * while the previous one's cue may still be showing — a refusal 500ms after a
 * success lands inside the confirmation window — and the chip must state the
 * LATEST outcome alone: "Copied!" beside "Copy failed" tells the user nothing
 * about what is on the clipboard. One `outcome` and one timer make that
 * structural: a new outcome cancels the old one's pending clear (a timer left
 * running would later clear a flash that no longer exists) and starts its own.
 *
 * "Latest" is the latest PRESS, not the latest settlement. Two presses can be
 * in flight together (`copyToClipboard` awaits the async API and falls back on
 * rejection, so a refusal settles later than a success), and nothing orders
 * their promises. Every press takes the next attempt number; a settlement whose
 * number is no longer current belongs to a press the user has since superseded
 * and is dropped, so a stale refusal cannot erase the confirmation the latest
 * press earned, and a stale success cannot hide the refusal it got. Unmount
 * advances the number too, so an in-flight settlement writes nothing.
 *
 * An outcome belongs to the TEXT that earned it. React reuses this hook when a
 * span's text changes under it — a streaming transcript rewriting a chip, an
 * editable preview — and the clipboard then holds the old text, so the new
 * text must not wear "Copied!", and a settlement for the old text must not
 * confirm the new one. A change of `text` therefore resets the outcome and
 * advances the attempt number, like an unmount would.
 */
/** How long "Copied!" shows. Exported so tests advance exactly this. */
export const COPIED_FLASH_MS = 1500
/** How long "Copy failed" shows — twice the confirmation, see `useCopiedFlash`. */
export const COPY_FAILED_FLASH_MS = 3000
/** The outcome on show, stamped with the press (attempt number) that earned
 *  it — so a consumer can tell a NEW outcome of the same kind from the one it
 *  is already showing (`InstantTip`'s hold needs that edge). */
type CopyFlash = { kind: 'copied' | 'failed'; seq: number } | null
/**
 * Press order across EVERY chip, not per chip: the latest press anywhere is
 * the one whose outcome the user is waiting for. A write can hang (a clipboard
 * permission prompt) while the user moves on and presses another chip; if the
 * old press then settles, a per-chip counter would accept it, and its "Copied!"
 * would evict the newer chip's failure from the one bubble. Every press takes
 * the next number here, and a settlement is applied only while its press is
 * still the newest. Invalidations (text change, unmount) stay LOCAL: they move
 * this chip's own marker off every attempt it has in flight, but must not
 * touch the shared order — a streaming transcript re-renders unrelated chips
 * constantly, and none of that is the user moving on from their press.
 */
let latestPress = 0
export function useCopiedFlash(text: string): {
  copied: boolean
  failed: boolean
  /** 0 while idle, else the attempt number of the outcome on show: a fresh
   *  value for every settled press. Hand it to `useInstantTip({ hold })`. */
  flashSeq: number
  copy: (text: string) => void
} {
  const [flash, setFlash] = useState<CopyFlash>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const attemptRef = useRef(0)
  const textRef = useRef(text)
  // Drop whatever is in flight or showing: pending settlements no longer
  // match the attempt number, and the pending clear is gone.
  const invalidate = () => {
    attemptRef.current += 1
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
  }
  useEffect(() => () => {
    attemptRef.current += 1
    if (timerRef.current) clearTimeout(timerRef.current)
  }, [])
  useEffect(() => {
    if (textRef.current === text) return
    textRef.current = text
    invalidate()
    setFlash(null)
  }, [text])
  const copy = (value: string) => {
    const attempt = ++latestPress
    attemptRef.current = attempt
    void copyToClipboard(value).then(ok => {
      // Superseded — by a later press on this chip or any other, by a text
      // change, or by an unmount: the user has moved on from this press.
      if (attempt !== attemptRef.current || attempt !== latestPress) return
      if (timerRef.current) clearTimeout(timerRef.current)
      setFlash({ kind: ok ? 'copied' : 'failed', seq: attempt })
      timerRef.current = setTimeout(() => {
        timerRef.current = null
        setFlash(null)
      }, ok ? COPIED_FLASH_MS : COPY_FAILED_FLASH_MS)
    })
  }
  return {
    copied: flash?.kind === 'copied',
    failed: flash?.kind === 'failed',
    flashSeq: flash?.seq ?? 0,
    copy,
  }
}

/**
 * The refused-clipboard-write notice a chip renders.
 *
 * One component so the chips cannot drift on the surface (`ErrorNotice`, the
 * rule `errors-use-error-notice` requires), the wording, or the hand-off
 * decision. The wording names the next step, not only the outcome: the text
 * the user wanted is still on screen, so selecting it IS the recovery, and a
 * bare "Copy failed" left them asking what to do about it. Its `role="alert"`
 * is the accessible error surface and the ONLY announcement of the refusal:
 * nothing else carries the string, so it is heard once. Its `message` is the
 * report key `ErrorNotice` looks up; a refused clipboard write is a
 * browser-side outcome with no entry in the error journal, so the lookup finds
 * nothing and the notice stands on the message alone.
 *
 * One placement: inside an `InstantTip` bubble, held for `COPY_FAILED_FLASH_MS`
 * and closed by the flash's own timer. The copy chip's bubble also carries its
 * hint and confirmation, so success and failure never read from different
 * spots; the title-cued chips (session, path, broken image) open a bubble for
 * the refusal alone (`useTitleCuedCopy`). Nothing enters the text flow — the
 * in-flow red label those chips used to render pushed the sentence around for
 * as long as it showed and read as a second, different feature beside the
 * bubble. The bubble is `pointer-events-none`, so there is no dismiss control:
 * the flash clears itself.
 *
 * The wording is the caller's, chosen by what the chip copies, and all four
 * share one family, "Couldn’t copy …". The copy chip, whose clipboard text IS
 * the span the reader sees, names the recovery: "Couldn’t copy — select the
 * text to copy it manually". The title-cued chips (`useTitleCuedCopy`) copy
 * something their label need not show — the session chip the normalised key
 * behind the author's spelling or nickname, the broken-image chip the path
 * behind its alt — so that sentence would have the reader copy the wrong
 * thing; each names what failed to copy instead, in the reader's own terms:
 * the session chip names the full session ID for its visible label ("Couldn’t
 * copy the full session ID for chat-42…"), the path chip the path's tail
 * ("Couldn’t copy the path vitest.config.mts" — a bubble the viewport clamp has
 * pulled left still says which chip it answers), the broken-image chip its
 * object ("… the image path").
 */
export function CopyFailedNotice({ message }: { message: string }) {
  return (
    <>
      {/* No hand-off: this renderer is embedded in hosts that hold unsaved
          drafts it cannot identify — MarkdownPanel's editable preview and the
          chat composer's — and the hand-off navigates away from them. Same
          decision as `MarkdownTable`'s copy notice. (A button inside a
          pointer-events-none bubble that closes itself would be dead anyway.) */}
      <ErrorNotice
        variant="inline"
        message={message}
        testId="md-chip-copy-error"
      />
    </>
  )
}

/**
 * The copy path of a chip whose hint and confirmation are its native `title`
 * — the session and path chips' Ctrl/Cmd+click, the broken-image chip's click.
 *
 * The same gated write as the copy chip (`useCopiedFlash`), and the SAME
 * failure surface: `CopyFailedNotice` in an `InstantTip` bubble opened at the
 * pressed element — worded by the caller as the object that failed to copy,
 * because what these chips copy is not always the text they show (see
 * `CopyFailedNotice`). `press(el)` names the anchor (`arm`) and writes; a refusal
 * then holds the bubble open for the failure's flash, a later outcome or the
 * flash's end closes it. The bubble carries no hover or focus handlers — the
 * native `title` is still this chip's hint, and its `Copied!` swap still its
 * confirmation (moving those into the bubble too is #13608) — so it exists for
 * the refusal alone and is held only while one shows.
 */
export function useTitleCuedCopy(text: string, failureMessage: string): {
  copied: boolean
  /** Write `text`, naming `el` as where the outcome's bubble opens. */
  press: (el: HTMLElement) => void
  /** Render beside the chip: the bubble (a portal) that carries a refusal. */
  failureBubble: React.ReactNode
} {
  const { copied, failed, flashSeq, copy } = useCopiedFlash(text)
  const { tip, tipId, arm } = useInstantTip({ hold: failed ? flashSeq : 0, placement: 'flow' })
  const press = (el: HTMLElement) => {
    arm(el)
    copy(text)
  }
  const failureBubble = (
    <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[calc(100vw-1rem)]">
      <CopyFailedNotice message={failureMessage} />
    </InstantTip>
  )
  return { copied, press, failureBubble }
}
