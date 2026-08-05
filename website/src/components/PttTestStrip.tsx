/**
 * "Try it" strip for the push-to-talk binding.
 *
 * Two jobs, both of which the surrounding rows cannot do:
 *
 * 1. **Prove the key exists on THIS keyboard.** Whether a machine has a right
 *    Option at all — and whether its release event actually reaches the page —
 *    is unanswerable from code or CI; it can only be settled by pressing the key
 *    on the keyboard in front of you. A dropdown can list `Right Option`
 *    regardless, so without this strip the first sign of a bad choice is voice
 *    input silently never starting.
 * 2. **Be the discoverability surface.** "Hold right Option to talk" is not
 *    guessable, and the shortcuts reference cannot render it: `formatShortcut`
 *    only knows modifier+key chords, not a lone side-specific modifier.
 *
 * Deliberately does NOT touch the microphone, and the copy has to carry that:
 * a first-run review found the reassurance losing an argument with its own
 * verdicts, because a small grey "never records anything" footnote sat four
 * lines under a bright green "releasing stops recording". So the reassurance
 * leads, and every verdict is in the CONDITIONAL — "in a real chat this would
 * record" — because the present indicative reads as a live microphone.
 */
import { useEffect, useRef, useState } from 'react'

import {
  bindingLabel,
  matchesBinding,
  type PttBinding,
  type PttMode,
  toSeconds,
} from '../lib/pushToTalk'
import { i18nT } from '../i18n/t'

interface Props {
  binding: PttBinding
  mode: PttMode
  holdMs: number
  /** Localised name of the mode picker's current option, quoted in a verdict so
   *  "nothing would happen" can say WHICH mode it is talking about. */
  modeLabel: string
  /** Localised label of the key row, so the wrong-key verdict can point at the
   *  control the user has to go change. */
  fieldLabel: string
}

type Pressed = { code: string; ms: number; matched: boolean }
type Status =
  | { kind: 'idle' }
  | { kind: 'holding'; press: Pressed }
  | { kind: 'released'; press: Pressed }
  | { kind: 'lost' }

/**
 * True when the keystroke is going into a text field.
 *
 * The strip listens on the document in the capture phase, so without this every
 * character typed into a sibling setting (the Language box below it) lands here
 * and flashes the amber "that was a different key" state — a false-alarm loop
 * triggered by editing an unrelated row.
 *
 * `<select>` is deliberately NOT excluded: picking the shortcut key leaves focus
 * on that dropdown, which is exactly when the user reaches for the strip, and a
 * select does not accept character input anyway.
 *
 * Exported for its own unit test — the strip renders its live state from a
 * requestAnimationFrame loop, which makes a rendered assertion both slower and
 * easier to get wrong than testing the predicate that actually decides.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el.tagName !== 'string') return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable === true
}

export function PttTestStrip({ binding, mode, holdMs, modeLabel, fieldLabel }: Props) {
  const [status, setStatus] = useState<Status>({ kind: 'idle' })
  const heldRef = useRef<{ code: string; t0: number; matched: boolean } | null>(null)
  const rafRef = useRef<number | null>(null)
  // Live props for the document listeners, which are bound once.
  const propsRef = useRef({ binding, mode, holdMs, modeLabel, fieldLabel })
  propsRef.current = { binding, mode, holdMs, modeLabel, fieldLabel }

  // Reset when the user picks a different key or mode, so a stale verdict can't
  // be mistaken for a verdict on the NEW settings.
  useEffect(() => { setStatus({ kind: 'idle' }) }, [binding.code, mode])

  useEffect(() => {
    const stopRaf = () => {
      if (rafRef.current !== null) { cancelAnimationFrame(rafRef.current); rafRef.current = null }
    }
    const tick = () => {
      const h = heldRef.current
      if (!h) { stopRaf(); return }
      setStatus({ kind: 'holding', press: { code: h.code, ms: performance.now() - h.t0, matched: h.matched } })
      rafRef.current = requestAnimationFrame(tick)
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return
      const matched = matchesBinding(e, propsRef.current.binding)
      const held = heldRef.current
      if (held) {
        // Auto-repeat re-fires keydown ~30x/sec; the first press owns the timer.
        if (held.code === e.code) return
        // A CHORD binding arrives as SEPARATE keydowns — the modifiers first,
        // each non-matching on its own, then the primary key that completes it.
        // Staying pinned to the first modifier is why the default Windows/Linux
        // binding (Alt+Shift+Space) could never read as matched here, so the one
        // surface whose job is to prove the binding works reported it broken.
        // Re-anchor onto the completing key, and time the hold from there —
        // which is also where the real trigger starts its own arming timer.
        if (!matched || held.matched) return
        heldRef.current = { code: e.code, t0: performance.now(), matched: true }
        return
      }
      heldRef.current = {
        code: e.code,
        t0: performance.now(),
        matched,
      }
      if (rafRef.current === null) rafRef.current = requestAnimationFrame(tick)
    }
    const onKeyUp = (e: KeyboardEvent) => {
      const h = heldRef.current
      if (!h || h.code !== e.code) return
      heldRef.current = null
      stopRaf()
      setStatus({ kind: 'released', press: { code: h.code, ms: performance.now() - h.t0, matched: h.matched } })
    }
    // Losing focus mid-press is exactly the case where no key-up ever arrives —
    // showing it here is how the user learns the safety timer exists.
    const onBlur = () => {
      if (!heldRef.current) return
      heldRef.current = null
      stopRaf()
      setStatus({ kind: 'lost' })
    }

    document.addEventListener('keydown', onKeyDown, true)
    document.addEventListener('keyup', onKeyUp, true)
    window.addEventListener('blur', onBlur)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.removeEventListener('keyup', onKeyUp, true)
      window.removeEventListener('blur', onBlur)
      stopRaf()
    }
  }, [])

  const press = status.kind === 'holding' || status.kind === 'released' ? status.press : null
  const armed = status.kind === 'holding'
  const matched = !!press?.matched
  const bad = (!!press && !press.matched) || status.kind === 'lost'

  return (
    <div
      className={`rounded-lg px-4 py-3 transition-colors ${
        status.kind === 'idle'
          ? 'border border-dashed border-border-strong bg-bg-accent'
          : bad
            ? 'border border-warn/50 bg-warn/10'
            : 'border border-accent bg-accent/10'
      }`}
    >
      {/* The privacy reassurance LEADS. It used to be a grey footnote below the
          verdicts, where it read as fine print contradicting them. */}
      <p className="text-[12px] text-text mb-2.5">{i18nT('components.pttTestStrip.no_mic_note')}</p>

      <div className="flex items-center justify-between gap-3 mb-1.5">
        <span className="flex items-center gap-2 text-[12.5px] font-semibold text-text-strong">
          <span
            className={`w-2 h-2 rounded-full ${
              armed && matched ? 'bg-accent animate-pulse' : bad ? 'bg-warn' : 'bg-muted-strong'
            }`}
            aria-hidden="true"
          />
          {headline(status)}
        </span>
        <span className="text-[11.5px] text-muted">{stateNote(status, propsRef.current)}</span>
      </div>

      {/* role=status so a screen reader hears the verdict without needing to see
          the colour change. Deliberately NOT monospace: a review found the mono
          treatment made a user-facing panel read as a developer console. */}
      <div role="status" aria-live="polite" className="text-[12px] leading-[1.75] text-muted min-h-[3em]">
        {status.kind === 'idle' && (
          <span>{i18nT('components.pttTestStrip.idle_press', { name: bindingLabel(binding) })}</span>
        )}
        {status.kind === 'lost' && (
          <span className="text-warn">{i18nT('components.pttTestStrip.focus_lost')}</span>
        )}
        {press && (
          <>
            <div>{i18nT('components.pttTestStrip.readout_held', { secs: toSeconds(press.ms) })}</div>
            <div className={press.matched ? 'text-ok' : 'text-warn'}>{verdict(press, propsRef.current)}</div>
          </>
        )}
      </div>

      <div className="h-1 rounded-full bg-border overflow-hidden mt-2">
        <div
          className={`h-full transition-[width] duration-75 ${bad ? 'bg-warn' : 'bg-accent'}`}
          style={{ width: armed && press ? `${Math.min(100, (press.ms / (holdMs * 3)) * 100)}%` : '0%' }}
        />
      </div>
    </div>
  )
}

function headline(s: Status): string {
  // Deliberately NOT `title` here: that string is the section label directly
  // above the strip, so reusing it renders the same heading twice.
  if (s.kind === 'idle') return i18nT('components.pttTestStrip.headline_idle')
  if (s.kind === 'lost') return i18nT('components.pttTestStrip.headline_lost')
  if (s.kind === 'released') {
    return s.press.matched
      ? i18nT('components.pttTestStrip.headline_captured')
      : i18nT('components.pttTestStrip.headline_other_key')
  }
  return s.press.matched
    ? i18nT('components.pttTestStrip.headline_matched')
    : i18nT('components.pttTestStrip.headline_other_key')
}

/** The short right-aligned note: what the strip is doing right now. */
function stateNote(s: Status, p: { mode: PttMode; holdMs: number }): string {
  if (s.kind === 'holding') {
    return s.press.matched && p.mode !== 'toggle' && s.press.ms >= p.holdMs
      ? i18nT('components.pttTestStrip.state_recording_sim')
      : i18nT('components.pttTestStrip.state_deciding')
  }
  if (s.kind === 'released') return i18nT('components.pttTestStrip.state_done')
  return '\u2014'
}

/**
 * What would have happened, in the conditional. Never the present indicative —
 * "this records while you hold" next to "your microphone stays off" is a
 * self-contradiction a first-run reader resolves by distrusting both.
 */
function verdict(
  press: Pressed,
  p: { binding: PttBinding; mode: PttMode; holdMs: number; modeLabel: string; fieldLabel: string },
): string {
  if (!press.matched) {
    return i18nT('components.pttTestStrip.verdict_wrong_key', {
      got: bindingLabel({ code: press.code }),
      want: bindingLabel(p.binding),
      field: p.fieldLabel,
    })
  }
  if (p.mode === 'toggle') return i18nT('components.pttTestStrip.verdict_toggle')
  if (press.ms >= p.holdMs) return i18nT('components.pttTestStrip.verdict_hold')
  return p.mode === 'hybrid'
    ? i18nT('components.pttTestStrip.verdict_tap_latch')
    : i18nT('components.pttTestStrip.verdict_tap_noop', { mode: p.modeLabel })
}
