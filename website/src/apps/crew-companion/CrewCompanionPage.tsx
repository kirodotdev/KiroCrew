/**
 * Crew Companion — KiroCrew builtin dashboard page.
 *
 * The pet lives on the desktop as a separate macOS app running its own HTTP server
 * on 127.0.0.1:7778. A browser page can't read that server directly, so every
 * request goes through the gateway reverse proxy at `/apps/crew-companion/api/<path>`
 * (same-origin, no CORS). This page is where you configure the three things the pet
 * can't easily surface from the desktop: how it nudges you (Settings), what it will
 * remind you about (Reminders), and its record of your time together (Memories).
 *
 * The desktop app being closed is the ORDINARY case, not an error — every control
 * still renders (disabled, with a line explaining why) rather than vanishing.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Ghost } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { apiGet, apiPost } from './api'
import { REMINDER_PATHS, STATS_PATHS, POLL_MS } from './constants'
import { CC_CSS } from './styles'
import SettingsSection from './SettingsSection'
import RemindersSection from './RemindersSection'
import MemoriesSection from './MemoriesSection'
import type { ReminderConfigPatch, RemindersPayload, StatsPayload } from './types'


export default function CrewCompanionPage() {
  const [rem, setRem] = useState<RemindersPayload | null>(null)
  /** 'offline' sentinel — the desktop app could not be reached. */
  const [remError, setRemError] = useState<string | null>(null)
  /** Which proxy path worked, so writes go the same way reads came. */
  const remPathRef = useRef<string | null>(null)

  const [mem, setMem] = useState<StatsPayload | null>(null)
  const [memOffline, setMemOffline] = useState(false)
  const memPathRef = useRef<string | null>(null)

  /** Draft for the custom interval — `null` = not editing, `''` = cleared. */
  const [customMins, setCustomMins] = useState<string | null>(null)

  /** Transient message for a failed write, announced politely to assistive tech. */
  const [notice, setNotice] = useState<string | null>(null)
  /**
   * Clear the failure notice on the next success. Without this the message was
   * permanent: a user who retried and succeeded still read that it had failed.
   */
  const clearNotice = () => setNotice(null)

  const loadReminders = useCallback(async () => {
    const paths = remPathRef.current ? [remPathRef.current] : REMINDER_PATHS
    for (const path of paths) {
      try {
        const data = await apiGet<RemindersPayload>(path)
        if (data && Array.isArray(data.reminders)) {
          remPathRef.current = path
          setRem(data)
          setRemError(null)
          return
        }
      } catch { /* try the next candidate */ }
    }
    setRemError('offline')
  }, [])

  const loadMemories = useCallback(async () => {
    const paths = memPathRef.current ? [memPathRef.current] : STATS_PATHS
    for (const path of paths) {
      try {
        const data = await apiGet<StatsPayload>(path)
        if (data && data.stats) {
          memPathRef.current = path
          setMem(data)
          setMemOffline(false)
          return
        }
      } catch { /* try the next candidate */ }
    }
    setMemOffline(true)
  }, [])

  // A browser client with no IPC to the desktop app, so it polls.
  useEffect(() => {
    void loadReminders()
    void loadMemories()
    const t = setInterval(() => { void loadReminders(); void loadMemories() }, POLL_MS)
    return () => clearInterval(t)
  }, [loadReminders, loadMemories])

  const writeBase = () => remPathRef.current || REMINDER_PATHS[0]

  const setReminderCfg = useCallback((patch: ReminderConfigPatch) => {
    // Optimistic: the poll is up to POLL_MS away and the switch should move now.
    setRem((r) => (r ? { ...r, ...patch } : r))
    apiPost(`${writeBase()}/config`, patch).then(clearNotice).catch((e: unknown) => {
      setNotice(i18nT('apps.crewCompanion.reminders.couldnt_save', { error: errText(e) }))
      void loadReminders()
    })
  }, [loadReminders])

  /**
   * Resolves TRUE only when the reminder actually reached the desktop app, so the
   * add box knows whether it may clear what the user typed. Returning void here
   * meant a failed write still emptied the field and the text was gone.
   */
  const addReminder = useCallback(async (
    text: string, fireAt: string, everyMinutes?: number,
  ): Promise<boolean> => {
    try {
      await apiPost(`${writeBase()}/add`, { text, fireAt, everyMinutes })
      clearNotice()
      await loadReminders()
      return true
    } catch (e: unknown) {
      setNotice(i18nT('apps.crewCompanion.reminders.couldnt_add', { error: errText(e) }))
      return false
    }
  }, [loadReminders])

  const skipReminder = useCallback((id: string) => {
    apiPost(`${writeBase()}/skip`, { id })
      .then(() => { clearNotice(); return loadReminders() })
      .catch((e: unknown) => setNotice(i18nT('apps.crewCompanion.reminders.couldnt_skip', { error: errText(e) })))
  }, [loadReminders])

  const removeReminder = useCallback((id: string) => {
    // Optimistic removal — the row should go now, not on the next poll.
    setRem((r) => (r ? { ...r, reminders: r.reminders.filter((x) => x.id !== id) } : r))
    apiPost(`${writeBase()}/remove`, { id }).then(clearNotice).catch((e: unknown) => {
      setNotice(i18nT('apps.crewCompanion.reminders.couldnt_remove', { error: errText(e) }))
      void loadReminders()
    })
  }, [loadReminders])

  return (
    <div className="cc-page">
      <style>{CC_CSS}</style>

      <div>
        <div className="cc-head-top">
          <Ghost size={22} style={{ color: 'var(--accent)' }} aria-hidden />
          <h1 className="cc-h1">{i18nT('apps.crewCompanion.header.title')}</h1>
        </div>
        <p className="cc-sub">{i18nT('apps.crewCompanion.header.subtitle')}</p>
      </div>

      <SettingsSection
        rem={rem}
        remError={remError}
        onCfg={setReminderCfg}
        customMins={customMins}
        setCustomMins={setCustomMins}
      />

      <RemindersSection
        rem={rem}
        remError={remError}
        onAdd={addReminder}
        onSkip={skipReminder}
        onRemove={removeReminder}
      />

      <MemoriesSection mem={mem} offline={memOffline} />

      {/* Politely announce a failed write without stealing focus. */}
      <div aria-live="polite" className="cc-muted" style={{ marginTop: 12 }}>{notice}</div>
    </div>
  )
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
