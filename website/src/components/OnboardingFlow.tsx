import { useState, useEffect, useLayoutEffect, useCallback, useContext, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Check, Monitor, Sun, Moon } from 'lucide-react'
import { useTheme, type ModePreference, type ColorTheme } from '../hooks/useTheme'
import { GhostWithArm } from '../assets/onboarding/GhostIcons'
import { Btn, SendBtn } from './ui'
import OnboardingChapterShell, { OnboardingShellContext } from './OnboardingChapterShell'
import { api } from '../api/client'
import { capRoleOther, clampRoleOther } from '../lib/userProfile'
import { ROLE_SLUGS, TECH_SLUGS } from '../lib/profileOptions'

import { i18nT } from '../i18n/t'
/**
 * First-run onboarding flow (5 steps) — the Customize chapter plus the feature
 * tour. It is the LAST of the three first-run chapters: Import setup runs first,
 * then the mandatory Privacy chapter (`PrivacyChapter`), then this.
 *   1. Pick your look   — centered modal, reuses the real theme picker.
 *   2. About you        — centered modal: role + technical comfort. Persisted
 *                         to dashboard.user_role / dashboard.user_technical_level
 *                         and injected into the agent prompt ([USER PROFILE]
 *                         block in context.py) so responses match the user's
 *                         background. Also editable in Settings > General.
 *   3. Schedule intro   — popover anchored to the Schedule nav item.
 *   4. Apps intro       — popover anchored to the App Store nav item.
 *   5. Sessions intro   — popover anchored to the Chat nav item, and the end of
 *                         first-run: its primary reads "Done" and finishes.
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
 * Every step except the last tour popover has a Skip that dismisses the flow and
 * marks the user onboarded. Skipping still persists any profile answers already
 * selected — the user gave the information; losing it on Skip would be
 * surprising.
 */

interface PopStep {
  navId: string
  route: string
}

// Steps 3-5 anchor to real left-rail nav items (see `data-onboarding-nav`
// on <NavItem> in App.tsx). The client's Sessions surface is the Chat rail
// item (navId 'chat').
const POPS: Record<number, PopStep> = {
  3: { navId: 'schedule', route: '/schedule' },
  4: { navId: 'apps', route: '/apps' },
  5: { navId: 'chat', route: '/chat' },
}

/**
 * Catalog KEYS for the step 3-5 popover copy, keyed by step number.
 *
 * Held apart from `POPS` and as flat `Record`s of full literal keys, indexed
 * inline at the `i18nT()` call, because that is the only shape
 * `scripts/check-i18n-keys.mjs` can resolve statically — nested inside `POPS` and
 * read as `i18nT(pop.titleKey)` the gate cannot see the key at all, and a key it
 * cannot resolve is a key it cannot verify exists. Keys rather than strings
 * because these tables are built at module load, where an `i18nT()` call would
 * freeze the boot language; the lookup runs during render.
 *
 * Keyed by `navId` rather than by step number: the gate refuses an object whose
 * property names are numeric literals (`resolveObjectLiteral` accepts only
 * identifier / string names), and the surface a popover describes is the stable
 * thing about it — inserting a step should not renumber its copy.
 */
const POP_TITLE_KEY: Record<string, string> = {
  schedule: 'components.onboardingFlow.pop_schedule_title',
  apps: 'components.onboardingFlow.pop_apps_title',
  chat: 'components.onboardingFlow.pop_chat_title',
}
const POP_BODY_KEY: Record<string, string> = {
  schedule: 'components.onboardingFlow.pop_schedule_body',
  apps: 'components.onboardingFlow.pop_apps_body',
  chat: 'components.onboardingFlow.pop_chat_body',
}

// Steps 1-2 are the centered Customize modals; 3-5 are the anchored tour
// popovers. Popovers 3-4 show "Next"; popover 5 is the last step of first-run,
// so it shows "Done", drops its Skip, and finishes the flow.
const LAST_STEP = 5

// Step-2 profile options. Values are the slugs accepted by the
// dashboard.user_role / dashboard.user_technical_level enums in the config
// PATCH allowlist (handlers/core.py) and mapped to prompt descriptions in
// context.py — keep all three in sync.
const ROLE_OPTIONS: ReadonlyArray<{ value: string }> = ROLE_SLUGS.map(value => ({ value }))
const TECH_OPTIONS: ReadonlyArray<{ value: string }> = TECH_SLUGS.map(value => ({ value }))

/**
 * Catalog KEY for each chip's visible text, keyed by the enum slug above. Flat
 * and indexed inline at the `i18nT()` call for the same static-resolution reason
 * as `POP_TITLE_KEY`; the slug in `value` is persisted config and stays verbatim.
 */
const ROLE_LABEL_KEY: Record<string, string> = {
  developer: 'components.onboardingFlow.role_developer',
  designer: 'components.onboardingFlow.role_designer',
  'product-manager': 'components.onboardingFlow.role_product_manager',
  'data-ml': 'components.onboardingFlow.role_data_ml',
  'it-ops': 'components.onboardingFlow.role_it_ops',
  other: 'components.onboardingFlow.role_other',
}
const TECH_LABEL_KEY: Record<string, string> = {
  codes: 'components.onboardingFlow.tech_codes',
  'somewhat-technical': 'components.onboardingFlow.tech_somewhat',
  'non-technical': 'components.onboardingFlow.tech_non_technical',
}

type ProfileConfig = {
  dashboard?: { user_role?: string; user_role_other?: string; user_technical_level?: string }
}

const RING_SHADOW = '0 20px 50px rgba(0,0,0,.42), 0 0 0 4px var(--accent-subtle)'
// 20% opacity tint of the active theme accent — used for selected states.
const ACCENT_20 = 'color-mix(in srgb, var(--accent) 20%, transparent)'


export default function OnboardingFlow({
  initialOpen,
  onComplete,
  onSkipAll,
}: {
  initialOpen: boolean
  onComplete: () => void
  // Abandoning the tour early — "Skip all", a popover's "Skip", or Escape. Kept
  // SEPARATE from onComplete because a skip must still pass through the mandatory
  // Privacy chapter if that has not happened yet, while finishing the tour with
  // "Done" cannot possibly need to (Privacy precedes this chapter). Falls back to
  // onComplete when the host does not distinguish the two.
  onSkipAll?: () => void
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
  // Which dialog step already received its initial focus (see the trap effect).
  // Reset on close so reopening focuses again.
  const initialFocusKeyRef = useRef('')

  // ── Step-2 profile state ──────────────────────────────────────────────────
  const [role, setRole] = useState('')
  // Free text behind "Other". Kept in state even while another chip is
  // selected, so toggling away and back does not lose what the user typed —
  // but only PERSISTED while role === 'other' (see persistProfile).
  const [roleOther, setRoleOther] = useState('')
  const roleOtherRef = useRef<HTMLInputElement>(null)
  const [techLevel, setTechLevel] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)
  const [profileSaveError, setProfileSaveError] = useState(false)
  // Armed after a dismissal-save fails; the next Skip/Escape discards
  // explicitly. Reset on reopen and on any successful save.
  const skipDiscardArmed = useRef(false)
  // Guards the server-seed effect from clobbering in-flow choices, and lets
  // persistProfile skip unchanged fields (no config churn / SEL noise).
  const profileTouched = useRef(false)
  const initialProfile = useRef<{ role: string; other: string; tech: string }>({
    role: '',
    other: '',
    tech: '',
  })

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
    const o = cfgData.dashboard?.user_role_other ?? ''
    const t = cfgData.dashboard?.user_technical_level ?? ''
    initialProfile.current = { role: r, other: o, tech: t }
    setRole(r)
    setRoleOther(o)
    setTechLevel(t)
  }, [open, cfgData])

  // Picking "Other" reveals an input the user must reach; focusing it turns the
  // reveal into a continuation of the same gesture instead of a second hunt for
  // the field. Runs on the transition INTO 'other' only, so re-renders from
  // typing don't steal the caret back to the start.
  useEffect(() => {
    if (step === 2 && role === 'other') roleOtherRef.current?.focus()
  }, [step, role])

  // Persist changed profile answers. Returns true when every changed field
  // was written. The baseline (initialProfile) advances PER FIELD and only
  // after its PATCH resolves — a failed write is never treated as persisted
  // (GPT review finding), so a retry re-attempts exactly the failed fields.
  // Step-2 Next awaits this and blocks on failure; finish() calls it
  // best-effort so Skip/Escape never trap the user in the modal.
  const persistProfile = useCallback(async (): Promise<boolean> => {
    const cur = initialProfile.current
    const jobs: Array<{ key: 'role' | 'other' | 'tech'; value: string; p: Promise<unknown> }> = []
    if (role !== cur.role) {
      jobs.push({ key: 'role', value: role, p: api.patchConfig('dashboard.user_role', role) })
    }
    // Persisted whenever it changed, INDEPENDENTLY of which chip is selected.
    // Deliberately not cleared when the user picks a real role: clearing means
    // a second PATCH that can succeed while the role PATCH fails, which would
    // leave the server holding `user_role=other` with its description deleted —
    // the answer silently thrown away. The value is inert instead: context.py
    // reads it ONLY while `user_role == 'other'` (see `_role_description`), so
    // a retained value can never contradict the picked role, and toggling back
    // to "Other" restores what the user typed.
    const otherValue = clampRoleOther(roleOther)
    if (otherValue !== cur.other) {
      jobs.push({
        key: 'other',
        value: otherValue,
        p: api.patchConfig('dashboard.user_role_other', otherValue),
      })
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
        initialProfile.current = { ...initialProfile.current, [jobs[i].key]: jobs[i].value }
      } else {
        ok = false
      }
    })
    qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    return ok
  }, [role, roleOther, techLevel, qc])

  // Sync with the server-confirmed onboarding flag on BOTH transitions: open
  // (reset to step 1) when the un-onboarded flag arrives, and close when it
  // clears (e.g. the server confirms the user is already onboarded) so a
  // stale flow can't linger. Manual replay via `mc-start-onboarding` sets
  // `open` independently and is unaffected, since it never touches `initialOpen`.
  useEffect(() => {
    if (initialOpen) {
      profileTouched.current = false
      skipDiscardArmed.current = false
      initialFocusKeyRef.current = ''
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
      initialFocusKeyRef.current = ''
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
  //
  // `skipped` routes the exit: an abandoned tour goes to onSkipAll (which may
  // still owe the user the Privacy chapter), a finished one to onComplete.
  const finish = useCallback(async (skipped: boolean) => {
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
    if (skipped && onSkipAll) onSkipAll()
    else onComplete()
  }, [persistProfile, onComplete, onSkipAll])

  // Bound wrappers so a DOM handler (`onClick={skipAll}`) can't pass its event
  // object in as `skipped` — a MouseEvent is truthy, which would silently turn
  // every completion into a skip.
  const skipAll = useCallback(() => { void finish(true) }, [finish])
  const completeFlow = useCallback(() => { void finish(false) }, [finish])

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

  // Escape on the ANCHORED popover steps (3-5). Those steps are non-modal, so
  // they are exempt from the Tab trap below — but Escape still has to mean
  // "skip all", because the rule is global: one keystroke abandons the rest of
  // first run from any point. Split out rather than folded into the trap effect
  // so the popovers gain the key binding without gaining a focus trap.
  useEffect(() => {
    if (!open || step <= 2) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      // Same freeze as the modal steps: `finish` awaits a profile PATCH, so a
      // second Escape mid-flight would re-enter it and report the skip twice.
      if (!savingProfile) skipAll()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, step, skipAll, savingProfile])

  // Modal-step a11y (website/AGENTS.md): move focus into the dialog, trap Tab,
  // and dismiss on Escape. Applies to the centered Customize modals (steps 1-2);
  // the anchored popovers (steps 3-5) are non-modal and exempt.
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
    // Focus the first control on ENTERING the dialog step — not on every
    // re-run. This effect re-runs whenever `finish` / `savingProfile` change
    // identity, which now includes every keystroke in the "Other" role field
    // (finish → persistProfile → roleOther). Re-focusing there would yank the
    // caret to "Skip all" after the first character typed, so initial focus is
    // keyed to the step and the re-runs only reinstall the key handler.
    //
    // `savingProfile` IS part of the key: every step-2 control is disabled
    // during the save, so `getFocusable()` is empty and the browser drops focus
    // to <body>. Without re-seating focus when the freeze lifts, the failed-save
    // path (modal stays open with an error) would leave the Tab trap's
    // first/last comparisons unmatched and Tab would escape the dialog.
    const focusKey = `${step}:${shellHost?.sectionSlot ? 1 : 0}:${savingProfile ? 1 : 0}`
    if (initialFocusKeyRef.current !== focusKey) {
      initialFocusKeyRef.current = focusKey
      getFocusable()[0]?.focus()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        // Dismissal is frozen while a save is in flight (same reason the
        // Skip button is disabled): the PATCH payload is already snapshotted.
        if (!savingProfile) skipAll()
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
  }, [open, step, skipAll, savingProfile, dialogRef, shellHost?.sectionSlot])

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
    else completeFlow()
  }

  // ── Step 1: Pick your look (Customize chapter — import-setup layout) ──────
  if (step === 1) {
    return (
      <OnboardingChapterShell
        eyebrow={i18nT('components.onboardingFlow.customize_step', { n: 1, total: 2 })}
        ariaLabel={i18nT('components.onboardingFlow.customize_kirocrew')}
        panelHeadline={i18nT('components.onboardingFlow.make_it_yours')}
        panelBody={i18nT('components.onboardingFlow.set_your_look_and_tell_kiro_about_you_so_respons')}
        panelFootnote={i18nT('components.onboardingFlow.change_anything_later_in_settings')}
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
        onSkipAll={skipAll}
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
        eyebrow={i18nT('components.onboardingFlow.customize_step', { n: 2, total: 2 })}
        ariaLabel={i18nT('components.onboardingFlow.customize_kirocrew')}
        panelHeadline={i18nT('components.onboardingFlow.make_it_yours')}
        panelBody={i18nT('components.onboardingFlow.set_your_look_and_tell_kiro_about_you_so_respons')}
        panelFootnote={i18nT('components.onboardingFlow.change_anything_later_in_settings')}
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
        onSkipAll={skipAll}
        skipDisabled={savingProfile}
        dialogRef={dialogRef}
        footer={
          <>
            <Btn type="button" className="h-9 rounded-lg px-4" disabled={savingProfile} onClick={() => setStep(1)}>
              {i18nT('components.onboardingFlow.back')}
            </Btn>
            <SendBtn type="button" disabled={savingProfile} onClick={next}>
              {savingProfile ? i18nT('components.onboardingFlow.saving') : i18nT('components.onboardingFlow.continue')}
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
              {i18nT(ROLE_LABEL_KEY[o.value])}
            </button>
          ))}
        </div>

        {/* "Other" is the only answer the six chips cannot express, so it
            reveals a free-text field instead of being a dead end. Optional:
            leaving it blank persists 'other' with no description, exactly as
            before this input existed. */}
        {role === 'other' && (
          <input
            ref={roleOtherRef}
            type="text"
            value={roleOther}
            disabled={savingProfile}
            onChange={e => {
              profileTouched.current = true
              // Cap here rather than with `maxLength`: the HTML attribute counts
              // UTF-16 code units, so a paste ending in an astral character
              // truncates mid-surrogate-pair.
              setRoleOther(capRoleOther(e.target.value))
            }}
            onKeyDown={e => {
              // Enter in a single-field reveal should advance, not submit the
              // dialog's first button.
              if (e.key === 'Enter') {
                e.preventDefault()
                if (!savingProfile) void next()
              }
            }}
            placeholder={i18nT('components.onboardingFlow.e_g_solutions_architect_sre_founder')}
            aria-label={i18nT('components.onboardingFlow.describe_your_role')}
            className="mt-2 w-full rounded-lg border border-border bg-transparent px-3 py-2 text-[13px] text-text placeholder:text-muted focus:border-accent focus:outline-none disabled:opacity-60"
          />
        )}

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
              {i18nT(TECH_LABEL_KEY[o.value])}
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
  const isLastPop = step === LAST_STEP

  return createPortal(
    <div className="fixed z-[120] w-[288px] animate-rise" style={{ left: coords.left, top: coords.top }}>
      <div
        className="relative bg-card border border-accent p-5"
        style={{ boxShadow: RING_SHADOW, borderRadius: '0px 24px 24px 24px' }}
      >
        <div className="absolute" style={{ bottom: 'calc(100% + 6px)', left: -4 }}>
          <GhostWithArm />
        </div>
        <h3 className="text-[18px] font-semibold text-text-strong leading-tight">{i18nT(POP_TITLE_KEY[pop.navId])}</h3>
        <p className="text-[13px] text-muted mt-2.5 leading-relaxed">{i18nT(POP_BODY_KEY[pop.navId])}</p>
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
            {/* Skip is hidden on the last popover: it finishes the flow itself,
                so a "Skip" beside "Done" would be a second way to do what the
                primary already does. Escape is not wired for the popovers (they
                are non-modal), which is why the earlier ones keep a Skip. */}
            {!isLastPop && (
              <button
                onClick={skipAll}
                className="text-[13px] text-muted hover:text-text-strong cursor-pointer bg-transparent border-none"
              >
                {i18nT('components.onboardingFlow.skip')}
              </button>
            )}
            <button
              onClick={next}
              aria-label={isLastPop
                ? i18nT('components.onboardingFlow.done')
                : i18nT('components.onboardingFlow.next')}
              className="flex items-center gap-1.5 rounded-[10px] bg-accent text-accent-fg text-[13px] font-semibold px-3 py-2 cursor-pointer border-none hover:opacity-90 transition-opacity"
            >
              {isLastPop
                ? i18nT('components.onboardingFlow.done')
                : i18nT('components.onboardingFlow.next')}
              {/* No arrow on the last popover — a forward arrow next to "Done"
                  advertises more tour than there is. */}
              {!isLastPop && <ArrowRight size={15} />}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
