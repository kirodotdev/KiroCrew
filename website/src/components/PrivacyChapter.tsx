import { useContext, useEffect, useRef } from 'react'
import { SendBtn } from './ui'
import OnboardingChapterShell, { OnboardingShellContext } from './OnboardingChapterShell'
import { setupChapterAside } from './AgentImportFlow'
import {
  PrivacyCommandList,
  PrivacyDisclosureSections,
  TelemetryToggle,
} from './PrivacyDisclosure'

import { i18nT } from '../i18n/t'

/**
 * Privacy — the second first-run chapter, between Import setup and Customize.
 *
 * A SINGLE-SCREEN chapter, so its eyebrow is the chapter name alone: there is no
 * "x of y" counter to show when x and y are both 1, and a counter that never
 * moves reads like a stalled wizard.
 *
 * MANDATORY, which here means exactly one thing — there is no way past it but
 * forward:
 *   - no "Skip all" (the shell renders none when no handler is passed),
 *   - no Escape-to-dismiss (the Tab trap below deliberately omits it),
 *   - and the paths that abandon the flows on EITHER side still route through
 *     it: skipping Import setup lands here, and "Skip all" from Import setup
 *     shows this screen before the user reaches the product (see App.tsx).
 *
 * Mandatory is not the same as a consent gate. The screen discloses what is
 * sent and offers the opt-out; "Continue" is always enabled and never requires
 * a choice, because the heartbeat's default is a decision the user can change
 * here, later in Settings → Privacy, or from the CLI.
 *
 * The disclosure body is the SAME `TelemetryToggle` + sections that Settings →
 * Privacy renders, so the first-run explanation and the durable panel cannot
 * drift.
 */
export default function PrivacyChapter({
  open,
  onContinue,
}: {
  open: boolean
  onContinue: () => void
}) {
  // The focus trap queries the dialog element. Inside a persistent shell host
  // the dialog is host-owned, so use its ref; standalone we own it locally.
  const shellHost = useContext(OnboardingShellContext)
  const localDialogRef = useRef<HTMLDivElement>(null)
  const dialogRef = shellHost?.dialogRef ?? localDialogRef
  const headingRef = useRef<HTMLHeadingElement>(null)

  // Move focus to the heading on open, then trap Tab inside the dialog
  // (website/AGENTS.md modal a11y). Escape is NOT wired: this chapter cannot be
  // dismissed, and a key that silently does nothing is better than one that
  // looks like it should close the screen.
  useEffect(() => {
    if (!open) return
    headingRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter(element => element.getAttribute('aria-hidden') !== 'true')
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const activeIndex = focusable.indexOf(document.activeElement as HTMLElement)
      if (event.shiftKey && activeIndex <= 0) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (activeIndex < 0 || activeIndex === focusable.length - 1)) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // shellHost?.sectionSlot: in host mode the content is portaled a pass after
    // open, so re-run once the slot exists to install the trap + initial focus.
  }, [open, dialogRef, shellHost?.sectionSlot])

  if (!open) return null

  return (
    <OnboardingChapterShell
      {...setupChapterAside(i18nT('components.privacyChapter.title'))}
      eyebrow={i18nT('components.privacyChapter.eyebrow')}
      dialogRef={dialogRef}
      header={
        <div className="mt-6">
          <h1
            ref={headingRef}
            tabIndex={-1}
            className="text-2xl font-semibold text-text-strong outline-none"
          >
            {i18nT('components.privacyChapter.title')}
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-muted">
            {i18nT('components.privacyChapter.subtitle')}
          </p>
        </div>
      }
      footer={
        <SendBtn type="button" onClick={onContinue}>
          {i18nT('components.privacyChapter.continue')}
        </SendBtn>
      }
    >
      {/* Control FIRST, detail below. The body scrolls, and burying the opt-out
          under three paragraphs of disclosure puts the only actionable thing on
          the screen below the fold — a control the user must scroll to find is a
          worse offer than one they can see. Settings → Privacy keeps the reverse
          order, where the durable explanation is what the reader came for. */}
      <TelemetryToggle />
      <div className="mt-6 border-t border-border pt-5">
        <PrivacyDisclosureSections />
        <p className="mt-5 mb-3 text-sm leading-relaxed text-muted">
          {i18nT('privacyDisclosure.controlsBody')}
        </p>
        <PrivacyCommandList />
      </div>
    </OnboardingChapterShell>
  )
}
