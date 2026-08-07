/**
 * panel.tsx — the panel, in its own window.
 *
 * This entry exists so the panel is an OS window rather than DOM inside the
 * companion's overlay, which is what makes it draggable independently: the heading
 * carries `-webkit-app-region: drag`, and the operating system moves the window.
 *
 * It owns the data the card renders (reminders, break cadence) and reports two things
 * back to the main process, because only this side knows them: whether the breathing
 * exercise is running, and whether a destination view is open. Both suppress
 * close-on-blur — losing a 48-second exercise or a half-read settings screen to a
 * stray click elsewhere would be hostile.
 */
import { StrictMode, useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { adoptDashboardTheme, watchThemeChanges } from './dashboardTheme'
import { initI18n } from '../../i18n'
import BreathingOverlay from './BreathingOverlay'
import { PanelCard } from './PanelCard'
import type { PanelView } from './PanelViews'
import {
  ADD_PATH,
  BREATHING_DONE_PATH,
  REMINDERS_PATH,
  REMOVE_PATH,
  SKIP_PATH,
} from './constants'
import { labelFor, sortedReminders } from './reminders'
import { petBridge } from './petBridge'
import type { Reminder } from './types'

/** How often the list refreshes while the panel is open. */
const REFRESH_MS = 30_000

interface PanelBridge {
  panelClose?(): void
  panelBreathing?(active: boolean): void
  panelHold?(hold: boolean): void
  /** Which side of the companion the panel opened on — aims the spring's origin. */
  onPanelOpened?(cb: (side: 'left' | 'right') => void): (() => void) | void
}

function bridge(): PanelBridge | undefined {
  return (window as unknown as { crewCompanion?: PanelBridge }).crewCompanion
}

function Panel() {
  const [reminders, setReminders] = useState<Reminder[]>([])
  const [breakMins, setBreakMins] = useState(45)
  const [sessionOn, setSessionOn] = useState(true)
  const [view, setView] = useState<PanelView>('main')
  const [breathing, setBreathing] = useState(false)
  /**
   * Which side of the companion the panel opened on. Aims the spring's transform
   * origin (see PanelCard + panel.css) so the card grows out of the companion rather
   * than away from it. The main process resolves the side from placement and sends it
   * over `crew-companion:panel-opened`.
   */
  const [openSide, setOpenSide] = useState<'left' | 'right'>('right')

  const load = useCallback(async () => {
    try {
      const r = await fetch(REMINDERS_PATH, { credentials: 'same-origin' })
      if (!r.ok) return
      const d = (await r.json()) as {
        reminders?: Reminder[]
        breakReminderMins?: number
        sessionNotificationsEnabled?: boolean
      }
      setReminders(Array.isArray(d.reminders) ? d.reminders : [])
      if (typeof d.breakReminderMins === 'number') setBreakMins(d.breakReminderMins)
      // Read alongside breakReminderMins, from the same flat snapshot. Omitting it
      // here is what made the footer claim "task alerts on" permanently: the value
      // was in the payload all along and simply never left this function.
      if (typeof d.sessionNotificationsEnabled === 'boolean') {
        setSessionOn(d.sessionNotificationsEnabled)
      }
    } catch {
      /* the panel shows what it last had rather than emptying */
    }
  }, [])

  useEffect(() => {
    void load()
    const t = window.setInterval(() => void load(), REFRESH_MS)
    return () => window.clearInterval(t)
  }, [load])

  /*
    Re-read the moment a setting changes, instead of waiting out the poll.

    The settings view lives in THIS window but writes through the bridge, so it
    cannot hand the new value back up here directly. `config:updated` is the
    existing broadcast for exactly that, and the settings view already listens to
    it; the footer beside it did not, so for up to REFRESH_MS the two halves of one
    card disagreed about the same setting.
  */
  useEffect(() => petBridge.onConfigUpdated?.(() => void load()), [load])

  /**
   * Aim the spring from the side the panel opened on, and start every open at the
   * glance. The window is destroyed on close, so a fresh open remounts the card and
   * replays the entrance — this just points it the right way and re-reads the list,
   * whose relative labels go stale while the window is gone.
   */
  useEffect(() => {
    const off = bridge()?.onPanelOpened?.((side) => {
      setOpenSide(side === 'left' ? 'left' : 'right')
      setView('main')
      void load()
    })
    return () => { off?.() }
  }, [load])

  /**
   * Tell the main process when the panel must not close on blur.
   *
   * Reported from here because only this side knows: the exercise is a commitment,
   * and a secondary view is a destination rather than a glance.
   */
  useEffect(() => {
    bridge()?.panelBreathing?.(breathing)
  }, [breathing])

  useEffect(() => {
    bridge()?.panelHold?.(view !== 'main')
  }, [view])

  /** Escape closes the panel — but not while the exercise is running. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (breathing) return
      bridge()?.panelClose?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [breathing])

  const addReminder = useCallback(
    async (text: string, fireAtIso: string, everyMinutes?: number) => {
      try {
        const r = await fetch(ADD_PATH, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, fireAt: fireAtIso, everyMinutes }),
        })
        if (!r.ok) return false
        await load()
        return true
      } catch {
        return false
      }
    },
    [load],
  )

  const mutate = useCallback(
    async (path: string, id: string) => {
      try {
        await fetch(path, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id }),
        })
        await load()
      } catch {
        /* the list simply does not change */
      }
    },
    [load],
  )

  /** A completed exercise is counted; one abandoned halfway is not. */
  const finishBreathing = useCallback(async () => {
    try {
      await fetch(BREATHING_DONE_PATH, { method: 'POST', credentials: 'same-origin' })
    } catch {
      /* the exercise still happened; only the tally missed it */
    }
    setBreathing(false)
  }, [])

  const now = new Date()
  // Up to three, strictly chronological — the spec's "one truthful window".
  const upNext = sortedReminders(reminders)
    .slice(0, 3)
    .map((r) => {
      const l = labelFor(r.fireAt, now)
      return {
        id: r.id,
        text: r.text,
        absLabel: l.absLabel,
        relLabel: l.relLabel,
        recurring: Boolean(r.recurrence),
      }
    })

  return (
    // Containing block for the exercise's inset:0, so it fills the card.
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <PanelCard
        upNext={upNext}
        breakMins={breakMins}
        sessionOn={sessionOn}
        view={view}
        openSide={openSide}
        onAdd={addReminder}
        onBreathe={() => setBreathing(true)}
        onSeeAll={() => setView('all')}
        onSettings={() => setView('settings')}
        onBack={() => setView('main')}
        onRemove={(id) => void mutate(REMOVE_PATH, id)}
        onSkip={(id) => void mutate(SKIP_PATH, id)}
        onClose={() => bridge()?.panelClose?.()}
      />
      {breathing ? (
        <BreathingOverlay onDone={finishBreathing} onEnd={() => setBreathing(false)} />
      ) : null}
    </div>
  )
}

const host = document.getElementById('root')
if (host) {
  initI18n()
  // Await the theme before the first paint: the card is styled from Kiro Crew's
  // variables, and rendering ahead of them shows fallback colours and then snaps.
  void adoptDashboardTheme().then(() => {
    watchThemeChanges()
    createRoot(host).render(
      <StrictMode>
        <Panel />
      </StrictMode>,
    )
  })
}
