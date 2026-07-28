import type { ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { motion, useReducedMotion } from 'framer-motion'
import { ArrowRight } from 'lucide-react'
import { KiroGhost } from './KiroGhost'

// Floating decorative mascot — the same treatment as the Kiro CLI setup gate
// (KiroPrerequisiteGate's FloatingGhost) and the Import setup panel: staggered
// fade + spring scale entrance and an infinite easeInOut bob. Honors the OS
// reduce-motion setting.
function FloatingGhost({
  className,
  delay,
  rotate = 0,
}: {
  className: string
  delay: number
  rotate?: number
}) {
  const reduceMotion = useReducedMotion()
  return (
    <motion.div
      aria-hidden="true"
      className={`pointer-events-none absolute z-0 text-white drop-shadow-[0_12px_20px_rgba(24,20,38,0.26)] ${className}`}
      initial={reduceMotion ? false : { opacity: 0, scale: 0.72 }}
      animate={{
        opacity: 1,
        scale: 1,
        y: reduceMotion ? 0 : [-5, 5, -5],
        rotate,
      }}
      transition={{
        opacity: { delay, duration: 0.35 },
        scale: { delay, duration: 0.45, type: 'spring', bounce: 0.45 },
        y: { delay, duration: 3.8, ease: 'easeInOut', repeat: Infinity },
      }}
    >
      <KiroGhost size={160} className="h-full w-full" />
    </motion.div>
  )
}

/**
 * Two-column onboarding "chapter" shell, mirroring the Import setup layout
 * (components/AgentImportFlow.tsx): a translucent scrim, an accent left panel
 * with the brand lockup + floating mascots, and a right column with a FIXED
 * header (eyebrow "<chapter> · N of M" + "Skip all") and stage title/description,
 * a SCROLLABLE body, and a PINNED footer for the step navigation.
 */
export default function OnboardingChapterShell({
  chapterLabel,
  stepIndex,
  stepCount,
  panelHeadline,
  panelBody,
  panelFootnote,
  title,
  description,
  onSkipAll,
  skipDisabled,
  footer,
  dialogRef,
  ariaLabel,
  children,
}: {
  chapterLabel: string
  stepIndex: number
  stepCount: number
  panelHeadline: string
  panelBody: string
  panelFootnote: string
  title: string
  description: string
  onSkipAll: () => void
  skipDisabled?: boolean
  footer: ReactNode
  dialogRef: RefObject<HTMLDivElement>
  ariaLabel: string
  children: ReactNode
}) {
  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      className="fixed inset-0 z-[120] flex min-h-0 overflow-y-auto bg-bg/70 backdrop-blur-sm p-0 text-text sm:items-center sm:justify-center sm:p-6"
    >
      <div className="relative flex min-h-screen w-full flex-col overflow-hidden bg-card shadow-xl sm:h-[min(760px,calc(100vh-48px))] sm:min-h-0 sm:max-w-6xl sm:flex-row sm:rounded-2xl sm:border sm:border-border">
        <aside className="relative flex min-h-[248px] w-full shrink-0 overflow-hidden bg-accent text-accent-fg sm:min-h-0 sm:w-[36%]">
          <FloatingGhost className="-left-8 top-[24%] h-24 w-20 rotate-90 lg:h-28 lg:w-24" delay={0.15} rotate={90} />
          <FloatingGhost className="-right-5 top-5 h-28 w-20 -rotate-12 lg:h-36 lg:w-28" delay={0.35} rotate={-12} />
          <FloatingGhost className="bottom-[-5.5rem] right-[-12%] hidden h-64 w-48 lg:block" delay={0.55} />
          <FloatingGhost className="-top-20 left-[40%] hidden h-48 w-36 rotate-180 lg:block" delay={0.75} rotate={180} />
          <div className="relative z-10 flex w-full flex-col p-7 sm:p-10">
            <div className="flex items-center gap-3">
              <KiroGhost size={28} className="h-8 w-7" />
              <span className="text-[15px] font-semibold tracking-wide">Kiro Crew</span>
            </div>
            <div className="mt-auto max-w-[290px]">
              <h1 className="text-4xl font-semibold leading-[1.05] tracking-[-0.02em] sm:text-[clamp(2.2rem,4vw,3.5rem)]">
                {panelHeadline}
              </h1>
              <p className="mt-5 max-w-[270px] text-sm leading-relaxed text-accent-fg/80">
                {panelBody}
              </p>
            </div>
            <p className="mt-8 text-[12px] font-medium text-accent-fg/75">{panelFootnote}</p>
          </div>
        </aside>
        <section className="flex min-h-[calc(100vh-248px)] min-w-0 flex-1 flex-col bg-card sm:min-h-0">
          <header className="shrink-0 px-6 pt-7 sm:px-10 sm:pt-10">
            <div className="flex items-center justify-between gap-4">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted">
                {chapterLabel} · {stepIndex} of {stepCount}
              </p>
              <button
                type="button"
                aria-label="Skip all setup and onboarding"
                disabled={skipDisabled}
                onClick={onSkipAll}
                className="inline-flex items-center gap-1.5 text-[13px] font-medium text-muted transition-colors hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
              >
                Skip all <ArrowRight className="lucide-inline" />
              </button>
            </div>
            <div className="mt-6">
              <h1 tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-none">
                {title}
              </h1>
              <p className="mt-2 text-sm leading-relaxed text-muted">{description}</p>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <main className="w-full px-6 pb-8 pt-6 sm:px-10 sm:pb-10 sm:pt-6">
              <div className="mx-auto max-w-2xl">{children}</div>
            </main>
          </div>
          <footer className="flex shrink-0 flex-wrap items-center justify-end gap-3 px-6 pt-4 pb-6 sm:px-10 sm:pb-10">
            {footer}
          </footer>
        </section>
      </div>
    </div>,
    document.body,
  )
}
