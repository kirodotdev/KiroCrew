import { Check, ChevronDown, Mic, TriangleAlert } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { i18nT } from '../i18n/t'
import { getPreferredMicId, listMicrophones } from '../hooks/mic'

interface Props {
  /** Label of the device actually capturing, for the trigger text. */
  deviceLabel?: string
  /**
   * deviceId of the track actually capturing (from `activeDeviceId(stream)`),
   * `''`/absent when unknown. While recording, the checkmark keys on THIS —
   * the saved preference is an intent, not a fact, and the two diverge
   * whenever acquisition fell back (chosen device gone or busy).
   */
  activeDeviceId?: string
  /** Called with a deviceId, or `''` for "system default". */
  onSelect: (deviceId: string) => void
  /**
   * True while audio is being captured. Only affects the footnote: the user
   * deserves to know the consequence of switching mid-capture BEFORE clicking.
   */
  recording?: boolean
  /**
   * True when a switch takes effect on the live capture; false when it only
   * applies to the next recording (the `MediaRecorder` batch path cannot change
   * its source mid-recording). Drives which footnote is shown.
   */
  liveSwitch?: boolean
  /** Tailwind color classes for the trigger, so each host keeps its own tint. */
  triggerClass?: string
}

/**
 * Input-source dropdown, mounted from wherever the active microphone is already
 * displayed.
 *
 * Shared by `VoiceStatusBar` and `VoiceDictationPanel` because those two render
 * MUTUALLY EXCLUSIVELY (`ChatInput`: `showDictation ? Panel : StatusBar`) and
 * `stt.dictation_panel` defaults ON — a picker in only one of them is invisible
 * to most users.
 *
 * Devices are enumerated when the menu OPENS, not on mount: labels are only
 * populated once mic permission has been granted, and a device can be plugged
 * in at any moment, so an open menu should reflect the hardware as it is now.
 */
export default function MicSourceMenu({ deviceLabel, activeDeviceId, onSelect, recording, liveSwitch, triggerClass = '' }: Props) {
  const [open, setOpen] = useState(false)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [preferred, setPreferred] = useState(getPreferredMicId())
  const [rect, setRect] = useState<{ left: number; top: number; bottom: number } | null>(null)
  const wrapRef = useRef<HTMLSpanElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    setPreferred(getPreferredMicId())
    listMicrophones().then(list => { if (alive) setDevices(list) })
    return () => { alive = false }
  }, [open])

  // Dismiss on outside pointer-down and on Escape. Pointer-down rather than
  // click so the menu closes before a click on a control behind it lands. The
  // menu is portalled out of `wrapRef`, so it needs its own containment check.
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (!wrapRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false) } }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [open])

  const toggle = () => {
    if (open) { setOpen(false); return }
    const r = wrapRef.current?.getBoundingClientRect()
    if (r) setRect({ left: r.left, top: r.bottom, bottom: r.top })
    setOpen(true)
  }

  const pick = (id: string) => {
    setPreferred(id)
    setOpen(false)
    onSelect(id)
  }

  // Reposition-or-close when the anchor moves under an open menu. `rect` is
  // captured once and consumed as fixed viewport coordinates, and the composer
  // GROWS as dictated text accumulates — the feature's primary scenario — so
  // without this the menu visibly detaches from its trigger.
  useEffect(() => {
    if (!open) return
    const sync = () => {
      const r = wrapRef.current?.getBoundingClientRect()
      if (r) setRect({ left: r.left, top: r.bottom, bottom: r.top })
      else setOpen(false)
    }
    window.addEventListener('resize', sync)
    // Capture phase: a scroll inside the chat transcript does not bubble to window.
    window.addEventListener('scroll', sync, true)
    return () => {
      window.removeEventListener('resize', sync)
      window.removeEventListener('scroll', sync, true)
    }
  }, [open])

  // The saved device is gone (unplugged, or its permission-scoped id rotated).
  // Session-start acquisition falls back to the default in that case, so say so
  // instead of rendering a checkmark next to a device that is not there.
  const savedMissing = !!preferred && devices.length > 0 && !devices.some(d => d.deviceId === preferred)

  // Which row gets the checkmark. While capturing, the mark is DATA-DRIVEN: it
  // reports the device that is actually live (track deviceId, or its label when
  // the id is permission-redacted), never the saved preference — so a switch
  // that did not really land is visible as the mark staying put. When no truth
  // is available mid-capture, no row is marked rather than marking a guess.
  // Idle, there is no live track; the mark shows the intent (what the NEXT
  // capture will request).
  const isChecked = (d: MediaDeviceInfo): boolean => {
    if (recording) {
      if (activeDeviceId) return d.deviceId === activeDeviceId
      if (deviceLabel) return d.label === deviceLabel
      return false
    }
    return d.deviceId === preferred
  }
  // "System default" is an intent, not a device: mid-capture the concrete
  // device row carries the truth, so the default row is never marked then.
  const defaultChecked = recording ? false : !preferred

  return (
    <span ref={wrapRef} className="relative flex-1 min-w-0 flex items-center">
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={i18nT('components.micSourceMenu.change_input_source')}
        title={deviceLabel || undefined}
        className={`flex items-center gap-1 min-w-0 max-w-full bg-transparent border-none font-inherit px-1 -ml-1 py-px rounded cursor-pointer hover:bg-bg-hover ${triggerClass}`}
      >
        <Mic size={12} className="shrink-0 opacity-70" aria-hidden="true" />
        <span className="truncate">{deviceLabel || i18nT('components.voiceStatusBar.default_microphone')}</span>
        <ChevronDown size={11} className="shrink-0 opacity-70" aria-hidden="true" />
      </button>

      {open && rect && createPortal(
        // PORTALLED to <body> on purpose: VoiceDictationPanel's root is
        // `h-[168px] overflow-hidden` (it clips the shader), which clipped the
        // bottom of an in-flow menu — "System default" and the footnote were
        // simply not there. Fixed positioning off the trigger's rect is what
        // lets the same component work inside a clipping host.
        <div
          ref={menuRef}
          role="menu"
          style={(() => {
            // Flip and clamp from AVAILABLE SPACE, not a guessed height. A dev Mac
            // routinely has 7+ inputs (BlackHole, Loopback, Krisp, aggregates), and
            // the trigger sits at the bottom of the composer, so the upward branch
            // is the normal case — an unclamped menu grows past the viewport top
            // with no scroll container and its first entries become unreachable.
            // Same shape as WorkspacePicker: compute the budget, keep a floor.
            const below = window.innerHeight - rect.top - 8
            const above = rect.bottom - 8
            const flip = below < Math.min(320, above)
            return {
              position: 'fixed' as const,
              left: Math.max(8, Math.min(rect.left - 4, window.innerWidth - 330)),
              ...(flip
                ? { bottom: window.innerHeight - rect.bottom + 4, maxHeight: Math.max(160, above) }
                : { top: rect.top + 4, maxHeight: Math.max(160, below) }),
            }
          })()}
          className="z-[9999] min-w-[246px] max-w-[320px] p-[5px] rounded-lg bg-bg-elevated border border-border-strong shadow-xl overflow-y-auto"
        >
          <div className="px-2 pt-1 pb-1.5 text-[10.5px] font-mono font-semibold tracking-wide text-muted">
            {i18nT('components.micSourceMenu.input_source')}
          </div>
          {devices.map(d => (
            <button
              key={d.deviceId}
              type="button"
              // menuitemradio, not menuitem: this is a single-select group, and
              // the checkmark is the ONLY signal of which device is live. As a
              // plain menuitem that state reached assistive tech not at all —
              // the tick is an aria-hidden svg and the rest is colour. Same
              // shape as the repo's other single-select rows (ChatSidebar).
              role="menuitemradio"
              aria-checked={isChecked(d)}
              onClick={() => pick(d.deviceId)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[12.5px] text-left bg-transparent border-none cursor-pointer hover:bg-bg-hover ${isChecked(d) ? 'text-accent' : 'text-text'}`}
            >
              <span className="w-3 shrink-0">
                {isChecked(d) && <Check size={12} aria-hidden="true" />}
              </span>
              <span className="truncate">{d.label || i18nT('components.micSourceMenu.unnamed_input')}</span>
            </button>
          ))}
          <div className="h-px bg-border my-1" />
          <button
            type="button"
            role="menuitemradio"
            aria-checked={defaultChecked}
            onClick={() => pick('')}
            className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[12.5px] text-left bg-transparent border-none cursor-pointer hover:bg-bg-hover ${defaultChecked ? 'text-accent' : 'text-text'}`}
          >
            <span className="w-3 shrink-0">{defaultChecked && <Check size={12} aria-hidden="true" />}</span>
            <span className="truncate">{i18nT('components.micSourceMenu.system_default')}</span>
          </button>
          {savedMissing && (
            <div className="flex gap-1.5 items-start px-2 py-1.5 text-[11px] leading-snug text-warn">
              <TriangleAlert size={12} className="shrink-0 mt-px" aria-hidden="true" />
              {i18nT('components.micSourceMenu.saved_device_unavailable')}
            </div>
          )}
          {recording && (
            <div className="flex gap-1.5 items-start px-2 py-1.5 text-[11px] leading-snug text-warn">
              <TriangleAlert size={12} className="shrink-0 mt-px" aria-hidden="true" />
              {liveSwitch
                ? i18nT('components.micSourceMenu.switching_drops_audio')
                : i18nT('components.micSourceMenu.applies_next_recording')}
            </div>
          )}
        </div>,
        document.body,
      )}
    </span>
  )
}
