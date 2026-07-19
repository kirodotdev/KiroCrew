import { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Check, Monitor, Sun, Moon } from 'lucide-react'
import { useTheme, type ModePreference, type ColorTheme } from '../hooks/useTheme'
import { GhostVar1, GhostVar2 } from '../assets/onboarding/GhostIcons'
import { SettingsSelect } from '../components/settings'

/**
 * First-run onboarding flow (4 steps):
 *   1. Pick your look   — centered modal, reuses the real theme picker.
 *   2. Schedule intro   — popover anchored to the Schedule nav item.
 *   3. Apps intro       — popover anchored to the App Store nav item.
 *   4. Sessions intro   — popover anchored to the Chat nav item.
 *
 * Triggers:
 *   - First launch: App passes `initialOpen` (from the un-onboarded state).
 *   - Manual: the `/onboarding` slash command dispatches a global
 *     `mc-start-onboarding` event, which reopens the flow anytime.
 *
 * Theming for steps 2-4 is automatic: picking a theme in step 1 calls the
 * real `setColorTheme` / `setTheme`, which re-skins the whole app (including
 * these popovers) live via CSS custom properties.
 *
 * Every step has a Skip that dismisses the flow and marks the user onboarded.
 */

interface PopStep {
  navId: string
  route: string
  title: string
  body: string
}

// Steps 2-4 anchor to real left-rail nav items (see `data-onboarding-nav`
// on <NavItem> in App.tsx). The client's Sessions surface is the Chat rail
// item (navId 'chat').
const POPS: Record<number, PopStep> = {
  2: {
    navId: 'schedule',
    route: '/schedule',
    title: 'Work that runs on time',
    body: 'Set tasks to run automatically so things happen without you lifting a finger.',
  },
  3: {
    navId: 'apps',
    route: '/apps',
    title: 'Extend what you can do with KiroCrew',
    body: 'Install purpose-built tools that unlock new capabilities and workflows.',
  },
  4: {
    navId: 'chat',
    route: '/chat',
    title: 'Start your first session',
    body: 'Ask a question, assign a task, or just say hi — your agent is ready.',
  },
}

const RING_SHADOW = '0 20px 50px rgba(0,0,0,.42), 0 0 0 4px var(--accent-subtle)'
// 20% opacity tint of the active theme accent — used for selected states.
const ACCENT_20 = 'color-mix(in srgb, var(--accent) 20%, transparent)'


export default function OnboardingFlow({
  initialOpen,
  onComplete,
}: {
  initialOpen: boolean
  onComplete: () => void
}) {
  const navigate = useNavigate()
  const {
    colorTheme,
    setColorTheme,
    allThemes,
    preference: modePref,
    setTheme: setModePref,
  } = useTheme()
  const [open, setOpen] = useState(initialOpen)
  const [step, setStep] = useState(1)
  const [coords, setCoords] = useState<{ left: number; top: number } | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  // Sync with the server-confirmed onboarding flag on BOTH transitions: open
  // (reset to step 1) when the un-onboarded flag arrives, and close when it
  // clears (e.g. the server confirms the user is already onboarded) so a
  // stale flow can't linger. Manual replay via `mc-start-onboarding` sets
  // `open` independently and is unaffected, since it never touches `initialOpen`.
  useEffect(() => {
    if (initialOpen) {
      setStep(1)
      setOpen(true)
    } else {
      setOpen(false)
    }
  }, [initialOpen])

  // Manual re-trigger via the `/onboarding` slash command.
  useEffect(() => {
    const handler = () => {
      setStep(1)
      setOpen(true)
    }
    window.addEventListener('mc-start-onboarding', handler)
    return () => window.removeEventListener('mc-start-onboarding', handler)
  }, [])

  const finish = useCallback(() => {
    setOpen(false)
    onComplete()
  }, [onComplete])

  const positionFor = useCallback((navId: string) => {
    // Popover is w-[288px]; keep it fully on-screen with a small margin so the
    // controls stay reachable even when the rail is collapsed/unmounted (mobile).
    const POP_W = 288
    const M = 12
    const APPROX_H = 200
    const clamp = (left: number, top: number) => ({
      left: Math.max(M, Math.min(left, window.innerWidth - POP_W - M)),
      top: Math.max(M, Math.min(top, window.innerHeight - APPROX_H - M)),
    })
    const el = document.querySelector<HTMLElement>(`[data-onboarding-nav="${navId}"]`)
    if (!el) {
      // Fallback when the rail isn't found (e.g. collapsed/mobile). Clamped so
      // the popover never lands outside the viewport.
      setCoords(clamp(260, 120))
      return
    }
    const r = el.getBoundingClientRect()
    // Anchor the bubble ~24px below the nav item top so the mascot (which sits
    // above the bubble's top-left corner) lines its left hand up with the
    // center of the nav item (e.g. "Schedule") in the rail.
    setCoords(clamp(r.right + 12, r.top + 20))
  }, [])

  // Position the popover BEFORE paint, using the target rail item's rect. The
  // left rail is mounted on every route, so the item exists even before we
  // navigate — so coords update to the NEW step's spot synchronously and the
  // bubble never flashes at the previous step's position.
  useLayoutEffect(() => {
    if (!open) return
    const pop = POPS[step]
    if (!pop) {
      setCoords(null)
      return
    }
    positionFor(pop.navId)
  }, [open, step, positionFor])

  // Switch to the step's surface AFTER paint. The route mount (e.g. ChatPage)
  // can block the main thread for a while; doing it here — not before the
  // positioning above — keeps that cost from delaying the anchor. Re-anchor
  // once it settles in case the layout shifted.
  useEffect(() => {
    if (!open) return
    const pop = POPS[step]
    if (!pop) return
    navigate(pop.route)
    const t = window.setTimeout(() => positionFor(pop.navId), 120)
    return () => window.clearTimeout(t)
  }, [open, step, navigate, positionFor])

  // Keep the popover anchored on viewport resize.
  useEffect(() => {
    if (!open || !POPS[step]) return
    const onResize = () => positionFor(POPS[step].navId)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [open, step, positionFor])

  // Step-1 modal a11y (website/CLAUDE.md): move focus into the dialog, trap Tab,
  // and dismiss on Escape. Steps 2-4 are non-modal popovers and are exempt.
  useEffect(() => {
    if (!open || step !== 1) return
    const node = dialogRef.current
    if (!node) return
    const getFocusable = () =>
      Array.from(
        node.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter(el => !el.hasAttribute('disabled'))
    // Focus the first control on open so keyboard/SR users land inside the dialog.
    getFocusable()[0]?.focus()
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        finish()
        return
      }
      if (e.key !== 'Tab') return
      const items = getFocusable()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, step, finish])

  if (!open) return null

  const next = () => {
    if (step < 4) setStep(step + 1)
    else finish()
  }

  // ── Step 1: Pick your look (centered modal) ──────────────────────────────
  if (step === 1) {
    return createPortal(
      <div className="fixed inset-0 z-[120] flex items-center justify-center bg-bg/70 backdrop-blur-sm animate-rise">
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="onboarding-look-title"
          className="relative bg-card border border-accent p-6 w-[412px] max-w-[92vw]"
          style={{ boxShadow: RING_SHADOW, borderRadius: '0px 16px 16px 16px' }}
        >
          <div className="absolute" style={{ bottom: 'calc(100% + 6px)', left: 0 }}>
            <GhostVar1 width={52} />
          </div>
          <div className="flex items-start justify-between">
            <h2 id="onboarding-look-title" className="text-[22px] font-semibold text-text-strong">Pick your look</h2>
            <button
              onClick={finish}
              className="flex items-center gap-1 text-[13px] text-muted hover:text-text-strong cursor-pointer bg-transparent border-none"
            >
              Skip <ArrowRight size={13} />
            </button>
          </div>
          <p className="text-[13.5px] text-muted mt-2">
            Choose a color theme and mode — you can change it anytime.
          </p>

          <div className="flex gap-1 border border-border rounded-[10px] p-1 mt-4" style={{ background: 'var(--panel-strong)' }}>
            {(['system', 'light', 'dark'] as ModePreference[]).map(m => (
              <button
                key={m}
                onClick={() => setModePref(m)}
                className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-[7px] text-[13px] cursor-pointer border-none transition-colors ${
                  modePref === m ? 'font-medium' : 'bg-transparent text-muted hover:text-text'
                }`}
                style={modePref === m ? { background: ACCENT_20, color: 'var(--accent)' } : undefined}
              >
                {m === 'system' ? <Monitor size={14} /> : m === 'light' ? <Sun size={14} /> : <Moon size={14} />}
                {m[0].toUpperCase() + m.slice(1)}
              </button>
            ))}
          </div>

          <div className="mt-4">
            <SettingsSelect
              label="Color Theme"
              description="Select a color palette for the dashboard"
              value={colorTheme}
              options={allThemes.map(t => t.value)}
              optionLabels={allThemes.map(t => t.label)}
              onChange={v => setColorTheme(v as ColorTheme)}
            />
          </div>

          <button
            onClick={next}
            className="w-full mt-5 py-3 rounded-[11px] bg-accent text-accent-fg text-[14.5px] font-semibold cursor-pointer border-none hover:opacity-90 transition-opacity"
          >
            Next
          </button>
        </div>
      </div>,
      document.body,
    )
  }

  // ── Steps 2-4: anchored feature popovers ─────────────────────────────────
  const pop = POPS[step]
  if (!pop || !coords) return null
  const dotIdx = step - 2

  return createPortal(
    <div className="fixed z-[120] w-[288px] animate-rise" style={{ left: coords.left, top: coords.top }}>
      <div
        className="relative bg-card border border-accent p-5"
        style={{ boxShadow: RING_SHADOW, borderRadius: '0px 24px 24px 24px' }}
      >
        <div className="absolute" style={{ bottom: 'calc(100% + 6px)', left: -4 }}>
          <GhostVar2 width={44} />
        </div>
        <h3 className="text-[18px] font-semibold text-text-strong leading-tight">{pop.title}</h3>
        <p className="text-[13px] text-muted mt-2.5 leading-relaxed">{pop.body}</p>
        <div className="flex items-center mt-[18px]">
          <div className="flex items-center gap-1.5">
            {[0, 1, 2].map(i => (
              <span
                key={i}
                className={`h-1.5 rounded-full transition-all ${i === dotIdx ? 'w-5 bg-accent' : 'w-1.5'}`}
                style={i === dotIdx ? undefined : { background: 'var(--border-strong)' }}
              />
            ))}
          </div>
          <div className="ml-auto flex items-center gap-4">
            {step !== 4 && (
              <button
                onClick={finish}
                className="text-[13px] text-muted hover:text-text-strong cursor-pointer bg-transparent border-none"
              >
                Skip
              </button>
            )}
            <button
              onClick={next}
              aria-label={step === 4 ? 'Finish onboarding' : 'Next'}
              className="flex items-center gap-1.5 rounded-[10px] bg-accent text-accent-fg text-[13px] font-semibold px-3 py-2 cursor-pointer border-none hover:opacity-90 transition-opacity"
            >
              {step === 4 ? 'Done' : 'Next'}
              {step === 4 ? <Check size={15} /> : <ArrowRight size={15} />}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
