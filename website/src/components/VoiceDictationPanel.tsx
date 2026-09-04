import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import Strands, { strandsSupported } from './Strands'
import type { AudioSample } from '../hooks/mic'
import MicSourceMenu from './MicSourceMenu'
import { downloadLabel } from '../lib/sttProviders'
import { i18nT } from '../i18n/t'

/** One tracked token of the in-flight partial hypothesis. */
interface RevisionToken {
  /** The token text: a word, or the whitespace run between words. */
  text: string
  /** Null for whitespace. How many consecutive hypotheses this word survived. */
  stability: number | null
  /** Bumped when the word was revised, remounting its span so the flash
   *  animation restarts. 0 = never revised (no flash). */
  generation: number
  /** Stable identity threaded through the alignment: a surviving word keeps its
   *  id across hypotheses even when an insertion shifts its position, so its
   *  span's key is position-independent and React never remounts (re-flashes)
   *  a word the recogniser did not touch. Unique for the hook's lifetime, so
   *  keys cannot collide across phrase commits either. */
  id: number
}

/**
 * Track word-level revisions across consecutive partial hypotheses.
 *
 * The STT backend re-decodes the current phrase every few hundred ms and the
 * new hypothesis replaces the old wholesale, so which words actually changed is
 * invisible. This aligns each hypothesis against the previous one and marks
 * three cases apart: a word that came back identical gets its stability count
 * bumped (rendered as increasing opacity — the longer a word survives, the more
 * solid it looks); a word revised in place gets its generation bumped
 * (remounting its span, which restarts a brief bright-to-dim flash); and words
 * appended past the previous hypothesis are new dictation, so they start dim
 * without flashing.
 *
 * Alignment anchors on text with a small lookahead instead of matching by
 * position alone: an insertion, split, or deletion mid-hypothesis shifts every
 * later index, and a positional diff would then mark the entire tail as revised
 * — flashing and de-solidifying words the recogniser never touched. The
 * lookahead re-anchors on the unchanged tail so only the words that actually
 * changed carry the revision signal. Anchoring is skipped when the SAME token
 * also occupies the current new position ("a b c" → "a c c"): jumping to the
 * old "c" would read that replacement as deletion-plus-append and the changed
 * word would never flash. An inserted word flashes (it IS a revision); a
 * dropped word simply disappears.
 *
 * Recomputes only when (committed, partial) actually changed and returns the
 * cached result otherwise, which keeps the render pure under StrictMode's
 * double-invoke. A committed-text change means the phrase was cut and a new one
 * began, so tracking resets; ids are never reused, so a fresh phrase's words
 * mount as new elements instead of tweening from the dead phrase's spans.
 */
function useWordRevisions(committed: string, partial: string): RevisionToken[] {
  const ref = useRef<{
    committed: string | null
    partial: string
    nextId: number
    tokens: RevisionToken[]
  }>({ committed: null, partial: '', nextId: 1, tokens: [] })
  if (ref.current.committed !== committed || ref.current.partial !== partial) {
    const reset = ref.current.committed !== committed
    const prev = reset ? [] : ref.current.tokens.filter(t => t.stability !== null)
    let nextId = ref.current.nextId
    const raw = partial.split(/(\s+)/).filter(Boolean)
    const nextTexts = raw.filter(t => !/^\s+$/.test(t))
    const LOOKAHEAD = 3
    let pi = 0
    let j = 0
    const tokens = raw.map<RevisionToken>(text => {
      if (/^\s+$/.test(text)) return { text, stability: null, generation: 0, id: 0 }
      const at = j
      j += 1
      // Anchor: the same text within a small window of the previous hypothesis.
      // Jumping past unmatched previous words is what keeps an insertion or
      // deletion from cascading down the tail — but it is skipped when the same
      // text ALSO appears among the upcoming new tokens ("a b c" → "a c c"):
      // the old copy then belongs to that later slot, and consuming it here
      // would read the in-place replacement as deletion-plus-append, so the
      // changed word would never flash.
      const limit = Math.min(pi + LOOKAHEAD, prev.length)
      const upcoming = nextTexts.slice(at + 1, at + 1 + LOOKAHEAD)
      for (let k = pi; k < limit; k += 1) {
        if (prev[k].text === text && (k === pi || !upcoming.includes(text))) {
          const survived = {
            text,
            stability: (prev[k].stability ?? 0) + 1,
            generation: prev[k].generation,
            id: prev[k].id,
          }
          pi = k + 1
          return survived
        }
      }
      if (pi < prev.length) {
        // The unmatched previous word re-appearing just ahead means this word
        // was INSERTED: flash it, keep the previous word for the next slot so
        // the unchanged tail keeps its stability.
        if (upcoming.includes(prev[pi].text)) {
          return { text, stability: 0, generation: 1, id: nextId++ }
        }
        // Revised in place: a NEW id remounts the span, restarting the flash.
        const replaced = { text, stability: 0, generation: prev[pi].generation + 1, id: nextId++ }
        pi += 1
        return replaced
      }
      return { text, stability: 0, generation: 0, id: nextId++ }
    })
    ref.current = { committed, partial, nextId, tokens }
  }
  return ref.current.tokens
}

/** Rest opacity for a provisional word: dim when fresh, solid once it has
 *  survived a few consecutive hypotheses. The ramp is the agreement count made
 *  visible, not decoration. The floor stays legible over the live shader —
 *  these spans already sit inside the muted color, so the alpha here compounds
 *  on that; dipping much below ~0.7 puts the newest word (the one a dictating
 *  user glances at to confirm recognition) at its faintest exactly when they
 *  check it. */
function restOpacity(stability: number): number {
  return Math.min(1, 0.7 + stability * 0.1)
}

/**
 * True when the animated panel should be used. Kept as a hook (rather than a
 * module constant) so a runtime change to the OS reduced-motion preference
 * takes effect without a reload.
 *
 * Under reduced motion we fall back to the bar meter rather than freezing the
 * shader on a static frame: a frozen frame communicates nothing about input
 * level, which is the entire job of this surface.
 */
export function useDictationPanelUsable(enabled: boolean): boolean {
  const [reduced, setReduced] = useState(
    () =>
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches === true,
  )
  const [supported] = useState(strandsSupported)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia('(prefers-reduced-motion: reduce)')
    const handler = () => setReduced(mql.matches)
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [])

  return enabled && supported && !reduced
}

interface Props {
  /** Live audio features. Handed to the shader as a ref, never as a value. */
  sampleRef: { current: AudioSample }
  /** Full composer text (frozen prefix + committed transcript + partial). */
  value: string
  /** Latest partial hypothesis, when streaming STT is on. */
  partial?: string
  /** Active capture device label. */
  deviceLabel?: string
  /** deviceId of the track actually capturing — see MicSourceMenu.activeDeviceId. */
  deviceId?: string
  /** Change the capture device. Receives a deviceId, or '' for system default. */
  onSelectDevice: (deviceId: string) => void
  /** True when a switch applies immediately rather than to the next recording. */
  deviceSwitchIsLive?: boolean
  /** True for streaming STT (live transcript in the composer → Enter sends). In
   *  batch STT there is no transcript until the mic is stopped, so the hint must
   *  point at the mic instead of promising Enter can send. */
  streaming?: boolean
  /**
   * True when a hold gesture is driving this session, which makes the keyboard
   * hint actively wrong: there is no Esc key on a phone, and the mic button is a
   * mode switch there rather than the control that finishes a recording. The hold
   * bar and the slide-up cue carry the affordance instead, so the row is dropped
   * rather than reworded — a second copy of "release to send" on screen at the
   * same time as the button saying it is noise.
   */
  gestureDriven?: boolean
  /** Byte progress of the one-time speech-model download this session waits on. */
  download?: { done: number; total: number } | null
}

/**
 * Dictation panel shown in place of the composer's status bar while recording.
 *
 * The transcript is rendered over the shader: text already committed by the STT
 * backend is solid, the in-flight partial hypothesis is muted. Both come from
 * the composer's own value, so what is shown here is exactly what will be sent.
 */
export default function VoiceDictationPanel({ sampleRef, value, partial, deviceLabel, deviceId, onSelectDevice, deviceSwitchIsLive, streaming, gestureDriven, download }: Props) {
  // Split committed vs partial without coupling to STT internals: the partial
  // is appended to the composer value, so it is the suffix — but only trust
  // that when it actually matches (the user may have typed since).
  const hasPartial = !!partial && value.endsWith(partial)
  const committed = hasPartial ? value.slice(0, value.length - partial.length) : value
  const tokens = useWordRevisions(committed, hasPartial ? partial : '')

  return (
    <div
      className="relative h-[168px] overflow-hidden border-b border-border bg-bg"
      data-testid="voice-dictation-panel"
    >
      <Strands sampleRef={sampleRef} />
      <div className="absolute inset-0 z-[3] flex flex-col justify-between px-[18px] py-3.5 pointer-events-none">
        <div className="flex items-center gap-2 text-[11.5px] font-medium text-danger">
          <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full rounded-full bg-danger opacity-60 animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-danger" />
          </span>
          <span aria-live="polite">{i18nT('components.voiceDictationPanel.listening')}</span>
          {/* The overlay is `pointer-events-none` so the shader stays visible and
              un-clickable; the picker is the one interactive child, so it opts
              itself back in. Still gated on a known label, preserving this
              panel's deliberate difference from VoiceStatusBar (an extra row at
              17px over a live shader is noise, and the panel's job is the
              transcript) — by the time the panel is up the label has resolved,
              so the picker is reachable in practice. `max-w-[40%]` keeps a long
              device name from pushing the keyboard hint out of the row. */}
          {deviceLabel && (
            <span className="pointer-events-auto max-w-[40%] min-w-0 flex items-center">
              <MicSourceMenu
                deviceLabel={deviceLabel}
                activeDeviceId={deviceId}
                onSelect={onSelectDevice}
                recording
                liveSwitch={deviceSwitchIsLive}
                triggerClass="text-muted font-normal hover:text-text"
              />
            </span>
          )}
          {!gestureDriven && (
            <span className="ml-auto text-muted font-normal font-mono text-[11px]">
              {streaming
                ? i18nT('components.voiceDictationPanel.esc_to_cancel_enter_to_send')
                : i18nT('components.voiceDictationPanel.esc_to_cancel_click_mic_to_finish')}
            </span>
          )}
        </div>
        {/* One flex child, not two, so the outer `justify-between` keeps placing
            the status row at the top and this block at the bottom whether or not
            a download line is present. */}
        <div className="flex flex-col gap-1">
          {/* Text sits over a live shader, so it carries its own shadow floor
              rather than relying on the background staying dark. */}
          <div
            className="text-[17px] leading-[1.45] text-text-strong max-h-20 overflow-hidden [text-shadow:0_1px_12px_var(--bg),0_0_3px_var(--bg)]"
            data-testid="voice-dictation-transcript"
          >
            {committed}
            {hasPartial && (
              <span className="text-muted">
                {tokens.map((t, i) =>
                  t.stability === null ? (
                    <span key={`ws-${i}`}>{t.text}</span>
                  ) : (
                    <motion.span
                      // The alignment-stable id: a surviving word keeps its span
                      // across insertions (no remount, no spurious re-flash),
                      // and a revised word gets a fresh id, which is what
                      // restarts the flash. Ids are never reused, so keys also
                      // cannot collide across phrase commits.
                      key={t.id}
                      data-testid="dictation-word"
                      data-stability={t.stability}
                      data-generation={t.generation}
                      // A revised word enters bright — full text color, not just
                      // full-alpha muted, so the flash clears the muted ceiling
                      // and reads over the live shader — then settles into the
                      // dimness it has earned. Fresh words (generation 0) skip
                      // the flash: appending is dictation, not revision.
                      initial={
                        t.generation > 0 ? { opacity: 1, color: 'var(--text-strong)' } : false
                      }
                      animate={{ opacity: restOpacity(t.stability), color: 'var(--muted)' }}
                      transition={{ duration: 0.35 }}
                    >
                      {t.text}
                    </motion.span>
                  ),
                )}
              </span>
            )}
          </div>
          {/* Under the transcript rather than in the status row: while the weights
              are still arriving there IS no transcript, and this line is the only
              thing distinguishing a first-run download from a dead microphone. */}
          {download && (
            <div
              aria-live="polite"
              className="text-[12px] text-muted [text-shadow:0_1px_12px_var(--bg),0_0_3px_var(--bg)]"
              data-testid="voice-dictation-download"
            >
              {downloadLabel(download)}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
