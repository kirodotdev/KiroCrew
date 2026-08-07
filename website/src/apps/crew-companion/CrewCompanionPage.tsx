/**
 * Crew Companion — KiroCrew builtin dashboard page.
 *
 * The companion lives on the desktop as a separate macOS app running its own HTTP
 * server on 127.0.0.1:7778. A browser page can't read that server directly, so every
 * request goes through the gateway reverse proxy at `/apps/crew-companion/api/<path>`
 * (same-origin, no CORS). This page is where you configure the things the companion
 * can't easily surface from the desktop: how it nudges you (Settings), what it will
 * remind you about (Reminders), and its record of your time together (Memories).
 *
 * When the companion is not running, both of its endpoints are unreachable; instead
 * of rendering dead disabled controls, the page shows a distinct "not running" state
 * with an Open action, and keeps Memories visible from a local cache.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Ghost, ExternalLink } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { isElectron } from '../../lib/electron'
import { apiGet, apiPost } from './api'
import { REMINDERS_PATH, STATS_PATH, POLL_MS } from './constants'
import { CC_CSS } from './styles'
import SettingsSection from './SettingsSection'
import RemindersSection from './RemindersSection'
import MemoriesSection from './MemoriesSection'
import type { ReminderConfigPatch, RemindersPayload, StatsPayload } from './types'


export default function CrewCompanionPage() {
  const [rem, setRem] = useState<RemindersPayload | null>(null)
  /** 'offline' sentinel — the desktop app could not be reached. */
  const [remError, setRemError] = useState<string | null>(null)

  const [mem, setMem] = useState<StatsPayload | null>(null)
  const [memOffline, setMemOffline] = useState(false)

  /** Draft for the custom interval — `null` = not editing, `''` = cleared. */
  const [customMins, setCustomMins] = useState<string | null>(null)

  /** Transient message for a failed write, announced politely to assistive tech. */
  const [notice, setNotice] = useState<string | null>(null)
  /**
   * Clear the failure notice on the next success — otherwise a user who retries
   * and succeeds still reads that it had failed.
   */
  const clearNotice = () => setNotice(null)

  const loadReminders = useCallback(async () => {
    try {
      const data = await apiGet<RemindersPayload>(REMINDERS_PATH)
      if (data && Array.isArray(data.reminders)) {
        setRem(data)
        setRemError(null)
        return
      }
    } catch { /* fall through to the offline state below */ }
    setRemError('offline')
  }, [])

  const loadMemories = useCallback(async () => {
    try {
      const data = await apiGet<StatsPayload>(STATS_PATH)
      if (data && data.stats) {
        setMem(data)
        setMemOffline(false)
        return
      }
    } catch { /* fall through to the offline state below */ }
    setMemOffline(true)
  }, [])

  // A browser client with no IPC to the desktop app, so it polls.
  useEffect(() => {
    void loadReminders()
    void loadMemories()
    const t = setInterval(() => { void loadReminders(); void loadMemories() }, POLL_MS)
    return () => clearInterval(t)
  }, [loadReminders, loadMemories])

  /** Writes go where reads come from — a single path now that this is a builtin. */
  const writeBase = () => REMINDERS_PATH

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
   * add box knows whether it may clear what the user typed.
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

  /**
   * Relaunch the desktop pet. The user can Quit it from the avatar menu, after
   * which there is no other way back — this button hits the gateway's app-open
   * endpoint (allowed in the manifest). On a headless/remote gateway the open
   * is not possible locally, so surface the command instead of failing silently.
   */
  /**
   * Bring the companion back.
   *
   * The original page POSTed to `/open`, which launched the separate desktop app. As a
   * builtin there is no external app to launch: the companion is an overlay window the
   * desktop app owns. So this records the request and the overlay opens its panel on
   * the next poll — the dashboard page has no bridge to open a window itself.
   *
   * If the app is switched off entirely, it is enabled first AND THEN the open request
   * is re-sent. Enabling alone used to be the end of it, which meant the first click
   * after switching the companion off turned it back on and opened nothing: the intent
   * the user actually expressed was dropped, silently, and only a second click worked.
   *
   * The failure notice uses its own key with an `{{error}}` slot, like every other
   * write on this page. It used to reuse `offline.body` — a piece of guidance prose
   * with no placeholder — so a failure showed the user "Open it to change break
   * nudges…" and threw the real reason away.
   */
  const openPet = useCallback(() => {
    const open = () => apiPost('/api/apps/crew-companion/window', { target: 'panel' })
    open()
      .then(clearNotice)
      .catch(() =>
        apiPost('/api/apps/crew-companion/enable', {})
          .then(open)               // the request that was asked for in the first place
          .then(clearNotice)
          .catch((e: unknown) => {
            setNotice(i18nT('apps.crewCompanion.offline.couldnt_open', { error: errText(e) }))
          }),
      )
  }, [clearNotice])

  // Memories is a look-back, not a live control — keep it visible even when the
  // pet is off by caching the last good stats and showing them (labelled) offline.
  useEffect(() => {
    if (mem) {
      try { localStorage.setItem('cc:lastStats', JSON.stringify(mem)) } catch { /* quota / private mode */ }
    }
  }, [mem])
  const cachedMem = useMemo<StatsPayload | null>(() => {
    if (mem) return mem
    try {
      const raw = localStorage.getItem('cc:lastStats')
      return raw ? (JSON.parse(raw) as StatsPayload) : null
    } catch { return null }
  }, [mem])

  /**
   * The desktop companion is unreachable only when BOTH of its endpoints are
   * down. Keying off reminders alone would render "isn't running" over a live
   * companion if only the reminders path drifted (version/path skew).
   */
  const offline = !!remError && memOffline

  return (
    <div className="cc-page">
      <style>{CC_CSS}</style>

      <div>
        <div className="cc-head-top">
          <Ghost size={22} style={{ color: 'var(--accent)' }} aria-hidden />
          <h1 className="cc-h1">{i18nT('apps.crewCompanion.header.title')}</h1>
        </div>
        <p className="cc-sub">{i18nT('apps.crewCompanion.header.subtitle')}</p>
        {/* Mochi-parity honesty (MochiPage.tsx does the same): in a browser there
          * is no desktop overlay to click, so the quit-tip's "click the companion
          * on your desktop" instruction is unfollowable and reads as a bug. Swap
          * it for a note that names where the companion actually lives. */}
        {!offline && isElectron ? <p className="cc-quit-tip">{i18nT('apps.crewCompanion.offline.quit_tip')}</p> : null}
        {!offline && !isElectron ? (
          <p role="note" className="cc-quit-tip">{i18nT('apps.crewCompanion.header.browser_note')}</p>
        ) : null}
      </div>

      {offline ? (
        <>
          <section className="cc-offline">
            <Ghost className="cc-offline-ghost" aria-hidden />
            <div className="cc-offline-title">{i18nT('apps.crewCompanion.offline.title')}</div>
            <div className="cc-offline-body">{i18nT('apps.crewCompanion.offline.body')}</div>
            <button type="button" className="cc-cta" onClick={openPet}>
              <ExternalLink size={15} aria-hidden /> {i18nT('apps.crewCompanion.offline.open')}
            </button>
          </section>

          {/* Memories persists from cache — a keepsake, not a live control. */}
          {cachedMem ? <MemoriesSection mem={cachedMem} offline={false} stale /> : null}
        </>
      ) : (
        <>
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
        </>
      )}

      {/* Politely announce a failed write without stealing focus. */}
      <div aria-live="polite" className="cc-muted" style={{ marginTop: 12 }}>{notice}</div>
    </div>
  )
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
