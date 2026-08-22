import { memo, useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, Container } from 'lucide-react'

import type { SessionExecution } from '../types'
import { i18nT } from '../i18n/t'

/** Width of the portaled panel, in px. Used for the left-edge clamp as well, so
 *  the two cannot disagree and let the panel hang off the viewport. */
const PANEL_WIDTH = 260

/**
 * Plain-language sentence for a host-fallback cause.
 *
 * A `switch` over literal keys rather than a lookup table, so every catalog
 * reference is a static string the key-reference gate can resolve. The default
 * branch is load-bearing: the backend owns this vocabulary and may add a cause
 * this build has never seen, and a raw token like `build_failed` on screen tells
 * a non-technical reader nothing. An unknown cause degrades to "the container
 * could not be used", which is the part that is true whatever the cause was.
 */
function reasonSentence(reason: string | null | undefined): string {
  switch (reason) {
    case 'untrusted':
      return i18nT('components.devcontainerExecutionChip.reason_untrusted')
    case 'build_failed':
      return i18nT('components.devcontainerExecutionChip.reason_build_failed')
    case 'docker_unavailable':
      return i18nT('components.devcontainerExecutionChip.reason_docker_unavailable')
    case 'config_changed':
      return i18nT('components.devcontainerExecutionChip.reason_config_changed')
    case 'unsupported_platform':
      return i18nT('components.devcontainerExecutionChip.reason_unsupported_platform')
    case 'sandbox_floor':
      return i18nT('components.devcontainerExecutionChip.reason_sandbox_floor')
    default:
      return i18nT('components.devcontainerExecutionChip.reason_unavailable')
  }
}

export interface DevcontainerExecutionChipProps {
  /**
   * Execution state of the active session. Absent/null means the work dir ships
   * no Dev Container config, so nothing is rendered at all — there is no second
   * world for the session to have landed in, and a chip saying "on your machine"
   * would invent a distinction the project does not have.
   */
  execution?: SessionExecution | null
  /** Drop the label span, leaving the glyph. Mirrors the shelf's own breakpoint. */
  compact?: boolean
}

/**
 * Which world THIS session's turns run in — the project's Dev Container, or the
 * user's own machine.
 *
 * The whole Dev Container feature is about where code executes, and granting
 * trust is not a guarantee: no Docker, a failed build, or a config edited after
 * the grant each fall back to running on the host. Without a session-visible
 * statement, a user who answered the trust prompt believes they are inside a
 * container while their commands touch their real filesystem, and nothing on
 * screen distinguishes the two.
 *
 * So the host case is the reason this exists, and it is styled as the degradation
 * it is (`warn` tokens, an alert glyph) rather than as neutral chrome. Both cases
 * name the consequence in plain words — what runs where — instead of container
 * vocabulary, and the cause is mapped to a sentence rather than shown as the raw
 * token the backend sends.
 *
 * Colours come from the theme's `warn`/`muted` custom properties, never literal
 * hex, so the chip follows a theme switch like the chips beside it.
 *
 * The explanation is a disclosure rather than a `title` tooltip: a tooltip is
 * unreachable by touch and by keyboard, and "why am I not in the container" is
 * exactly the question a degraded session needs answered. The chip keeps a
 * `title` as well, but nothing is ONLY in it.
 */
/**
 * Whether the execution chip has anything to render for *execution*.
 *
 * Exported so a caller that needs to know whether the chip will occupy space --
 * e.g. to avoid laying out an empty row around it -- asks this instead of
 * re-deriving the condition. A copied condition drifts the moment a mode is
 * added here, and the copy's failure mode is an empty bordered row with nothing
 * in it, which reads as a rendering bug.
 */
export function showsExecution(execution: SessionExecution | null | undefined): boolean {
  if (!execution) return false
  return execution.mode === 'container' || execution.mode === 'host'
}

function DevcontainerExecutionChip({ execution, compact }: DevcontainerExecutionChipProps) {
  const [open, setOpen] = useState(false)
  const [rect, setRect] = useState<DOMRect | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const headingId = useId()

  // Anchor rect for the portaled panel, refreshed on scroll/resize so the
  // floating panel tracks the chip instead of stranding beside it.
  useEffect(() => {
    if (!open) return
    const sync = () => {
      if (btnRef.current) setRect(btnRef.current.getBoundingClientRect())
    }
    sync()
    window.addEventListener('scroll', sync, true)
    window.addEventListener('resize', sync)
    return () => {
      window.removeEventListener('scroll', sync, true)
      window.removeEventListener('resize', sync)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      // The panel is portaled to <body>, so it is NOT inside wrapRef — a click on
      // it would otherwise read as an outside click.
      if (panelRef.current && panelRef.current.contains(target)) return
      if (wrapRef.current && !wrapRef.current.contains(target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  useEffect(() => {
    if (!open) return
    // Escape closes and returns focus to the chip: a keyboard user has no pointer
    // to click outside with, and without the focus return the caret lands nowhere.
    // Captured and stopped so the composer's own Escape handling does not fire.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      setOpen(false)
      btnRef.current?.focus()
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [open])

  const toggle = useCallback(() => setOpen(o => !o), [])

  // No config for this work dir, or a mode this build cannot interpret. Either
  // way there is nothing truthful to claim about where the session runs, and a
  // wrong claim here is worse than silence.
  if (!showsExecution(execution)) return null
  // Narrowed for the reads below; showsExecution already rejected null.
  if (!execution) return null

  const host = execution.mode === 'host'
  const name = execution.container_name || ''
  const heading = host
    ? i18nT('components.devcontainerExecutionChip.running_on_your_machine')
    : name
      ? i18nT('components.devcontainerExecutionChip.running_in_container_named', { name })
      : i18nT('components.devcontainerExecutionChip.running_in_container')
  const effect = host
    ? i18nT('components.devcontainerExecutionChip.host_effect')
    : i18nT('components.devcontainerExecutionChip.container_effect')
  // The name is rendered in full and clipped by CSS rather than sliced here: an
  // ellipsis tells the reader the name continues, whereas a JS cut produces a
  // plausible-looking wrong name.
  const label = host
    ? i18nT('components.devcontainerExecutionChip.on_your_machine')
    : name || i18nT('components.devcontainerExecutionChip.in_container')
  // Container, not Box: the trust chip beside this one already uses Box, and in
  // the compact shelf both collapse to icon-only buttons -- two identical glyphs
  // side by side would be two indistinguishable controls.
  const Glyph = host ? AlertTriangle : Container

  return (
    // `status`, not `alert`: a degraded session is a standing condition the user
    // should find when they look, not an interruption of the turn in progress.
    <div ref={wrapRef} role="status" className="relative flex items-center shrink-0">
      <button
        type="button"
        ref={btnRef}
        onClick={toggle}
        aria-haspopup="dialog"
        aria-expanded={open}
        // Named explicitly rather than through the label span: below the shelf's
        // compact breakpoint the span is not rendered and the glyph is
        // aria-hidden, which would leave an unnamed control. The full sentence is
        // used, not the short label, so a screen reader gets the state and not
        // just a container name.
        aria-label={heading}
        title={heading}
        className={`inline-flex items-center gap-1.5 h-7 shrink-0 text-[12px] px-2.5 rounded-md border-none cursor-pointer transition-colors ${
          host
            ? `text-warn ${open ? 'bg-warn-subtle' : 'bg-transparent hover:bg-warn-subtle'}`
            : `text-muted ${open ? 'bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] text-text' : 'bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] hover:text-text'}`
        }`}
      >
        <Glyph size={13} className="shrink-0 opacity-70" aria-hidden="true" />
        {!compact && <span className="truncate max-w-[140px]">{label}</span>}
      </button>
      {open && rect && createPortal(
        /* Portaled to <body> with fixed positioning, like the Dev Container
           chip's own menu: the composer wrapper is `overflow-hidden` and the shelf
           strip is `overflow-x-auto`, so an absolutely-positioned panel is
           clipped by both instead of floating over the transcript. */
        <div
          ref={panelRef}
          role="dialog"
          aria-labelledby={headingId}
          className="fixed z-[60] rounded-xl border border-border bg-bg-elevated shadow-xl px-3 py-2.5 animate-slide-up"
          style={{
            width: PANEL_WIDTH,
            left: Math.max(8, Math.min(rect.left, window.innerWidth - PANEL_WIDTH - 8)),
            bottom: window.innerHeight - rect.top + 6,
          }}
        >
          <div
            id={headingId}
            className={`text-[12px] font-medium leading-snug ${host ? 'text-warn' : 'text-text'}`}
          >
            {heading}
          </div>
          <div className="text-[11px] text-muted leading-relaxed mt-1">{effect}</div>
          {/* Only the host case has a cause to give. Mapped to a sentence: the raw
              token is a backend identifier, and an unmapped one still reads as
              plain English rather than leaking through. */}
          {host && (
            <div className="text-[11px] text-muted leading-relaxed mt-1.5">
              {reasonSentence(execution.reason)}
            </div>
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}

export default memo(DevcontainerExecutionChip)
