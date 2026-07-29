import { useState, useEffect, useLayoutEffect, useCallback, useContext, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Check, Monitor, Sun, Moon } from 'lucide-react'
import { useTheme, type ModePreference, type ColorTheme } from '../hooks/useTheme'
import { GhostVar2 } from '../assets/onboarding/GhostIcons'
import { Btn, SendBtn } from './ui'
import OnboardingChapterShell, { OnboardingShellContext } from './OnboardingChapterShell'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
/**
 * First-run onboarding flow (5 steps):
 *   1. Pick your look   — centered modal, reuses the real theme picker.
 *   2. About you        — centered modal: role + technical comfort. Persisted
 *                         to dashboard.user_role / dashboard.user_technical_level
 *                         and injected into the agent prompt ([USER PROFILE]
 *                         block in context.py) so responses match the user's
 *                         background. Also editable in Settings > General.
 *   3. Schedule intro   — popover anchored to the Schedule nav item.
 *   4. Apps intro       — popover anchored to the App Store nav item.
 *   5. Sessions intro   — popover anchored to the Chat nav item.
 *
 * Triggers:
 *   - First launch: App passes `initialOpen` (from the un-onboarded state).
 *   - Manual: the `/onboarding` slash command dispatches a global
 *     `mc-start-onboarding` event, which reopens the flow anytime.
 *
 * Theming for steps 2-5 is automatic: picking a theme in step 1 calls the
 * real `setColorTheme` / `setTheme`, which re-skins the whole app (including
 * these popovers) live via CSS custom properties.
 *
 * Every step has a Skip that dismisses the flow and marks the user onboarded.
 * Skipping still persists any profile answers already selected — the user
 * gave the information; losing it on Skip would be surprising.
 */

interface PopStep {
  navId: string
  route: string
  title: string
  body: string
}

// Steps 3-5 anchor to real left-rail nav items (see `data-onboarding-nav`
// on <NavItem> in App.tsx). The client's Sessions surface is the Chat rail
// item (navId 'chat').
const POPS: Record<number, PopStep> = {
  3: {
    navId: 'schedule',
    route: '/schedule',
    title: 'Work that runs on time',
    body: 'Set tasks to run automatically so things happen without you lifting a finger.',
  },
  4: {
    navId: 'apps',
    route: '/apps',
    title: 'Extend what you can do with KiroCrew',
    body: 'Install purpose-built tools that unlock new capabilities and workflows.',
  },
  5: {
    navId: 'chat',
    route: '/chat',
    title: 'Start your first session',
    body: 'Ask a question, assign a task, or just say hi — your agent is ready.',
  },
}

const LAST_STEP = 5

// Step-2 profile options. Values are the slugs accepted by the
// dashboard.user_role / dashboard.user_technical_level enums in the config
// PATCH allowlist (handlers/core.py) and mapped to prompt descriptions in
// context.py — keep all three in sync.
const ROLE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'developer', label: 'Developer' },
  { value: 'designer', label: 'UX Designer' },
  { value: 'product-manager', label: 'Product Manager' },
  { value: 'data-ml', label: 'Data / ML' },
  { value: 'it-ops', label: 'IT / Ops' },
  { value: 'other', label: 'Other' },
]
const TECH_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'codes', label: 'I write code' },
  { value: 'somewhat-technical', label: 'Somewhat' },
  { value: 'non-technical', label: 'Not technical' },
]

type ProfileConfig = { dashboard?: { user_role?: string; user_technical_level?: string } }

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
  // The focus trap queries the dialog element. Inside a persistent shell host
  // the dialog is host-owned, so use its ref; standalone we own it locally.
  const shellHost = useContext(OnboardingShellContext)
  const localDialogRef = useRef<HTMLDivElement>(null)
  const dialogRef = shellHost?.dialogRef ?? localDialogRef

  // ── Step-2 profile state ──────────────────────────────────────────────────
  const [role, setRole] = useState('')
  const [techLevel, setTechLevel] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)
  const [profileSaveError, setProfileSaveError] = useState(false)
  // Armed after a dismissal-save fails; the next Skip/Escape discards
  // explicitly. Reset on reopen and on any successful save.
  const skipDiscardArmed = useRef(false)
  // Guards the server-seed effect from clobbering in-flow choices, and lets
  // persistProfile skip unchanged fields (no config churn / SEL noise).
  const profileTouched = useRef(false)
  const initialProfile = useRef<{ role: string; tech: string }>({ role: '', tech: '' })

  const qc = useQueryClient()
  // Preselect previously saved answers (matters for `/onboarding` replays).
  // Shares the app-wide config query; on true first-run it resolves to ''.
  const { data: cfgData } = useQuery<ProfileConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: open,
    staleTime: 60_000,
  })
  useEffect(() => {
    if (!open || !cfgData || profileTouched.current) return
    const r = cfgData.dashboard?.user_role ?? ''
    const t = cfgData.dashboard?.user_technical_level ?? ''
    initialProfile.current = { role: r, tech: t }
    setRole(r)
    setTechLevel(t)
  }, [open, cfgData])

  // Persist changed profile answers. Returns true when every changed field
  // was written. The baseline (initialProfile) advances PER FIELD and only
  // after its PATCH resolves — a failed write is never treated as persisted
  // (GPT review finding), so a retry re-attempts exactly the failed fields.
  // Step-2 Next awaits this and blocks on failure; finish() calls it
  // best-effort so Skip/Escape never trap the user in the modal.
  const persistProfile = useCallback(async (): Promise<boolean> => {
    const cur = initialProfile.current
    const jobs: Array<{ key: 'role' | 'tech'; value: string; p: Promise<unknown> }> = []
    if (role !== cur.role) {
      jobs.push({ key: 'role', value: role, p: api.patchConfig('dashboard.user_role', role) })
    }
    if (techLevel !== cur.tech) {
      jobs.push({
        key: 'tech',
        value: techLevel,
        p: api.patchConfig('dashboard.user_technical_level', techLevel),
      })
    }
    if (jobs.length === 0) return true
    const results = await Promise.allSettled(jobs.map(j => j.p))
    let ok = true
    results.forEach((r, i) => {
      if (r.status === 'fulfilled') {
        if (jobs[i].key === 'role') initialProfile.current = { ...initialProfile.current, role: jobs[i].value }
        else initialProfile.current = { ...initialProfile.current, tech: jobs[i].value }
      } else {
        ok = false
      }
    })
    qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    return ok
  }, [role, techLevel, qc])

  // Sync with the server-confirmed onboarding flag on BOTH transitions: open
  // (reset to step 1) when the un-onboarded flag arrives, and close when it
  // clears (e.g. the server confirms the user is already onboarded) so a
  // stale flow can't linger. Manual replay via `mc-start-onboarding` sets
  // `open` independently and is unaffected, since it never touches `initialOpen`.
  useEffect(() => {
    if (initialOpen) {
      profileTouched.current = false
      skipDiscardArmed.current = false
      setProfileSaveError(false)
      setStep(1)
      setOpen(true)
    } else {
      setOpen(false)
    }
  }, [initialOpen])

  // Manual re-trigger via the `/onboarding` slash command.
  useEffect(() => {
    const handler = () => {
      profileTouched.current = false
      skipDiscardArmed.current = false
      setProfileSaveError(false)
      setStep(1)
      setOpen(true)
    }
    window.addEventListener('mc-start-onboarding', handler)
    return () => window.removeEventListener('mc-start-onboarding', handler)
  }, [])

  // Dismissal (Skip / Escape / Done). Awaits the profile save so a transient
  // PATCH failure can't silently drop selected answers while onboarding marks
  // itself complete (GPT round-2 finding). On failure the modal stays with an
  // error explaining the choice; a SECOND Skip/Escape discards explicitly —
  // informed dismissal, never a trap. Succeeding or no-op saves close in one
  // press (the await is a local loopback call, imperceptible).
  const finish = useCallback(async () => {
    if (!skipDiscardArmed.current) {
      // Freeze inputs for this await too (same race as Next's save): the
      // PATCH payload is snapshotted, so edits during the flight would be
      // silently dropped by the completion that follows.
      setSavingProfile(true)
      const ok = await persistProfile()
      setSavingProfile(false)
      if (!ok) {
        skipDiscardArmed.current = true
        setProfileSaveError(true)
        return
      }
    }
    setOpen(false)
    onComplete()
  }, [persistProfile, onComplete])

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

  // Modal-step a11y (website/AGENTS.md): move focus into the dialog, trap Tab,
  // and dismiss on Escape. Applies to the centered modals (steps 1-2); the
  // anchored popovers (steps 3-5) are non-modal and exempt.
  useEffect(() => {
    if (!open || step > 2) return
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
        // Dismissal is frozen while a save is in flight (same reason the
        // Skip button is disabled): the PATCH payload is already snapshotted.
        if (!savingProfile) finish()
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
    // shellHost?.sectionSlot: in host mode the dialog mounts a pass after the
    // flow opens, so re-run once it exists to install the trap + initial focus.
  }, [open, step, finish, savingProfile, dialogRef, shellHost?.sectionSlot])

  if (!open) return null

  const next = async () => {
    if (step === 2) {
      // Await the write so a gateway hiccup can't silently drop the answers
      // the user just gave. Failure keeps the modal open with a retry hint;
      // Skip remains available as the escape hatch.
      setSavingProfile(true)
      setProfileSaveError(false)
      const ok = await persistProfile()
      setSavingProfile(false)
      if (!ok) {
        setProfileSaveError(true)
        return
      }
      skipDiscardArmed.current = false
    }
    if (step < LAST_STEP) setStep(step + 1)
    else finish()
  }

  // ── Step 1: Pick your look (Customize chapter — import-setup layout) ──────
  if (step === 1) {
    return (
      <OnboardingChapterShell
        eyebrow={`Customize · 1 ${i18nT('components.onboardingChapterShell.of')} 2`}
        ariaLabel={i18nT('components.onboardingFlow.customize_kirocrew')}
        panelHeadline="Make it yours."
        panelBody="Set your look and tell Kiro about you so responses fit the way you work."
        panelFootnote="Change anything later in Settings."
        header={
          <div className="mt-6">
            <h1 tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-none">
              {i18nT('components.onboardingFlow.pick_your_look')}
            </h1>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              {i18nT('components.onboardingFlow.choose_a_color_theme_and_mode_you_can_change_it')}
            </p>
          </div>
        }
        onSkipAll={finish}
        dialogRef={dialogRef}
        footer={<SendBtn type="button" onClick={next}>{i18nT('components.onboardingFlow.continue')}</SendBtn>}
      >
        <div className="flex gap-1 border border-border rounded-[10px] p-1" style={{ background: 'var(--panel-strong)' }}>
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

        <div className="mt-5 text-[11px] uppercase tracking-wide text-muted mb-1.5">
          {i18nT('components.onboardingFlow.color_theme')}
        </div>
        <div className="grid grid-cols-3 gap-2" role="group" aria-label={i18nT('components.onboardingFlow.color_theme')}>
          {allThemes.map(t => (
            <button
              key={t.value}
              onClick={() => setColorTheme(t.value as ColorTheme)}
              aria-pressed={colorTheme === t.value}
              className={`flex min-w-0 items-center justify-center gap-1.5 truncate rounded-lg border px-3 py-2.5 text-[13px] cursor-pointer transition-colors ${
                colorTheme === t.value
                  ? 'border-accent font-medium'
                  : 'border-border bg-transparent text-text hover:text-text-strong'
              }`}
              style={colorTheme === t.value ? { background: ACCENT_20, color: 'var(--accent)' } : undefined}
            >
              {t.label}
            </button>
          ))}
        </div>
      </OnboardingChapterShell>
    )
  }

  // ── Step 2: About you (Customize chapter — import-setup layout) ──────────
  if (step === 2) {
    // Freeze all inputs while a save is in flight: changing a chip after Next
    // snapshots the PATCH payload would advance the flow with a stale value
    // persisted (GPT round-3 finding). The freeze is brief (loopback PATCH).
    const pickRole = (v: string) => {
      if (savingProfile) return
      profileTouched.current = true
      setRole(r => (r === v ? '' : v))
    }
    const pickTech = (v: string) => {
      if (savingProfile) return
      profileTouched.current = true
      setTechLevel(t => (t === v ? '' : v))
    }
    return (
      <OnboardingChapterShell
        eyebrow={`Customize · 2 ${i18nT('components.onboardingChapterShell.of')} 2`}
        ariaLabel={i18nT('components.onboardingFlow.customize_kirocrew')}
        panelHeadline="Make it yours."
        panelBody="Set your look and tell Kiro about you so responses fit the way you work."
        panelFootnote="Change anything later in Settings."
        header={
          <div className="mt-6">
            <h1 tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-none">
              {i18nT('components.onboardingFlow.tell_kiro_about_you')}
            </h1>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              {i18nT('components.onboardingFlow.answers_set_how_kiro_explains_things_plain_langu')}
            </p>
          </div>
        }
        onSkipAll={finish}
        skipDisabled={savingProfile}
        dialogRef={dialogRef}
        footer={
          <>
            <Btn type="button" className="h-9 rounded-lg px-4" disabled={savingProfile} onClick={() => setStep(1)}>
              {i18nT('components.onboardingFlow.back')}
            </Btn>
            <SendBtn type="button" disabled={savingProfile} onClick={next}>
              {savingProfile ? 'Saving…' : 'Continue'}
            </SendBtn>
          </>
        }
      >
        <div
          id="onboarding-role-label"
          className="text-[11px] uppercase tracking-wide text-muted mb-1.5"
        >
          {i18nT('components.onboardingFlow.your_role')}
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-labelledby="onboarding-role-label">
          {ROLE_OPTIONS.map(o => (
            <button
              key={o.value}
              onClick={() => pickRole(o.value)}
              disabled={savingProfile}
              aria-pressed={role === o.value}
              className={`flex items-center gap-1 rounded-full px-3 py-1.5 text-[13px] cursor-pointer transition-colors border ${
                role === o.value
                  ? 'border-accent font-medium'
                  : 'border-border bg-transparent text-text hover:text-text-strong'
              }`}
              style={role === o.value ? { background: ACCENT_20, color: 'var(--accent)' } : undefined}
            >
              {role === o.value && <Check size={13} aria-hidden />}
              {o.label}
            </button>
          ))}
        </div>

        <div
          id="onboarding-tech-label"
          className="text-[11px] uppercase tracking-wide text-muted mt-4 mb-1.5"
        >
          {i18nT('components.onboardingFlow.how_technical_are_you')}
        </div>
        <div
          className="flex flex-col gap-1.5"
          role="group"
          aria-labelledby="onboarding-tech-label"
        >
          {TECH_OPTIONS.map(o => (
            <button
              key={o.value}
              onClick={() => pickTech(o.value)}
              disabled={savingProfile}
              aria-pressed={techLevel === o.value}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2.5 text-[13px] cursor-pointer transition-colors ${
                techLevel === o.value
                  ? 'border-accent font-medium'
                  : 'border-border bg-transparent text-text hover:text-text-strong'
              }`}
              style={techLevel === o.value ? { background: ACCENT_20, color: 'var(--accent)' } : undefined}
            >
              {techLevel === o.value && <Check size={13} aria-hidden />}
              {o.label}
            </button>
          ))}
        </div>

        {profileSaveError && (
          <p role="alert" className="text-[12.5px] mt-3 mb-0" style={{ color: 'var(--danger)' }}>
            {i18nT('components.onboardingFlow.couldn_t_save_your_answers_press_next_to_retry_o')}
          </p>
        )}
      </OnboardingChapterShell>
    )
  }

  // ── Steps 3-5: anchored feature popovers ─────────────────────────────────
  const pop = POPS[step]
  if (!pop || !coords) return null
  const dotIdx = step - 3

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
            {step !== LAST_STEP && (
              <button
                onClick={finish}
                className="text-[13px] text-muted hover:text-text-strong cursor-pointer bg-transparent border-none"
              >
                {i18nT('components.onboardingFlow.skip')}
              </button>
            )}
            <button
              onClick={next}
              aria-label={step === LAST_STEP ? 'Finish onboarding' : 'Next'}
              className="flex items-center gap-1.5 rounded-[10px] bg-accent text-accent-fg text-[13px] font-semibold px-3 py-2 cursor-pointer border-none hover:opacity-90 transition-opacity"
            >
              {step === LAST_STEP ? 'Done' : 'Next'}
              {step === LAST_STEP ? <Check size={15} /> : <ArrowRight size={15} />}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
