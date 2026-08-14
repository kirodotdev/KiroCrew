/**
 * OS notification surface. Third consumer of MC_NOTIFICATION_EVENT (after
 * useNotificationSound and ThemeExperienceLayer): mirrors in-app notifications
 * to the operating system's notification center via the web Notification API.
 *
 * One consumer on the shared event means one banner per event by construction —
 * this replaces the two historical ad-hoc paths (the unread-count effect in
 * useNativeNotification and the inline `new Notification` in useWebSocket's
 * approval case) that could both fire for a single approval.
 *
 * Settings persist in localStorage under 'mc-notification-os'. Banners only
 * fire while the tab is hidden — when the user is looking at the app, the
 * toast/badge/sound surfaces already cover the event.
 */
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../store'
import type { AppStore } from '../store'
import { switchSlot } from '../store/chatSlice'
import {
  MC_NOTIFICATION_EVENT, MC_OS_SETTINGS_CHANGED_EVENT,
  TURN_DONE_KIND, APPROVAL_KIND, type McNotificationDetail,
} from './notificationEvent'
import { safeInternalUrl } from '../components/notifications/notifMeta'
import { SOUND_CATEGORIES, type SoundCategory } from './useNotificationSound'
import { safeSetItem } from '../utils/safeStorage'
import { i18nT } from '../i18n/t'

export interface OSNotificationSettings {
  enabled: boolean
  /** Per-category opt-out. Absent = on; the master switch gates everything. */
  perCategory: Partial<Record<SoundCategory, boolean>>
}

const STORAGE_KEY = 'mc-notification-os'

const DEFAULTS: OSNotificationSettings = {
  enabled: true,
  perCategory: {},
}

const VALID_CATEGORIES = new Set<string>(SOUND_CATEGORIES)

export function loadOSSettings(): OSNotificationSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULTS, perCategory: { ...DEFAULTS.perCategory } }
    const parsed = JSON.parse(raw) as Partial<OSNotificationSettings>
    const perCategory: Partial<Record<SoundCategory, boolean>> = {}
    for (const [k, v] of Object.entries(parsed.perCategory || {})) {
      if (VALID_CATEGORIES.has(k) && typeof v === 'boolean') {
        perCategory[k as SoundCategory] = v
      }
    }
    return {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : DEFAULTS.enabled,
      perCategory,
    }
  } catch {
    return { ...DEFAULTS, perCategory: { ...DEFAULTS.perCategory } }
  }
}

export function saveOSSettings(s: OSNotificationSettings): void {
  safeSetItem(STORAGE_KEY, JSON.stringify(s))
  window.dispatchEvent(new CustomEvent(MC_OS_SETTINGS_CHANGED_EVENT))
}

/** Whether a banner is wanted for this kind under the given settings.
 *  Unknown kinds (app-published channels) are gated by the master switch only. */
export function osBannerWantedForKind(kind: string | undefined, settings: OSNotificationSettings): boolean {
  if (!settings.enabled) return false
  const cat = kind && VALID_CATEGORIES.has(kind) ? (kind as SoundCategory) : undefined
  if (cat && settings.perCategory[cat] === false) return false
  return true
}

/** Resolve the owning session's display title from the store, if any. */
function sessionTitleForSlot(store: AppStore, slot: string | undefined): string | undefined {
  if (!slot) return undefined
  try {
    const slots = store.getState().dashboard.slots
    const match = slots.find((s) => s.key === slot)
    return match?.title || undefined
  } catch {
    return undefined
  }
}

/** Compose banner title/body from the event detail. Pure — the owning
 *  session's display title is injected by the hook. Exported for tests. */
export function composeBanner(
  detail: McNotificationDetail,
  botName: string,
  session?: string,
): { title: string; body: string } {
  let title = detail.title || ''
  let body = detail.body || ''
  if (!title) {
    if (detail.kind === TURN_DONE_KIND) {
      title = session || botName
      body = body || i18nT('hooks.useOSNotification.turn_complete')
    } else if (detail.kind === APPROVAL_KIND) {
      title = i18nT('hooks.useOSNotification.approval_required')
      body = body || i18nT('hooks.useOSNotification.a_task_needs_your_decision')
    } else {
      title = botName
      body = body || i18nT('hooks.useOSNotification.new_notification')
    }
  }
  // Name the session in the title when it is not already the title, so a
  // multi-session user knows which session is talking without opening the app.
  if (session && title !== session) title = `${session} — ${title}`
  return { title, body }
}

/**
 * Installs the window listener that mirrors notification events to the OS.
 * Mount once at app shell level (App.tsx), inside the Router.
 */
export function useOSNotification(botName: string, avatar: string): void {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const appStore = useAppStore()

  useEffect(() => {
    let current = loadOSSettings()
    const onSettingsChanged = () => { current = loadOSSettings() }

    const onNotification = (e: Event) => {
      const detail = (e as CustomEvent<McNotificationDetail>).detail || {}
      if (!osBannerWantedForKind(detail.kind, current)) return
      // Banner only when the app is not being looked at: the in-app surfaces
      // (toast, badge, sound) already cover the visible case.
      if (typeof document !== 'undefined' && !document.hidden) return
      if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return
      const { title, body } = composeBanner(detail, botName, sessionTitleForSlot(appStore, detail.slot))
      try {
        const n = new Notification(title, {
          body,
          icon: avatar,
          tag: detail.tag || 'kirocrew-notif',
        })
        n.onclick = () => {
          try { window.focus() } catch { /* best-effort */ }
          const safe = safeInternalUrl(detail.url)
          if (safe) {
            navigate(safe)
          } else if (detail.slot) {
            dispatch(switchSlot(detail.slot))
            navigate('/chat')
          }
          n.close()
        }
      } catch { /* Notification constructor can throw in odd environments — never break the app for a banner */ }
    }

    window.addEventListener(MC_OS_SETTINGS_CHANGED_EVENT, onSettingsChanged)
    window.addEventListener(MC_NOTIFICATION_EVENT, onNotification as EventListener)
    return () => {
      window.removeEventListener(MC_OS_SETTINGS_CHANGED_EVENT, onSettingsChanged)
      window.removeEventListener(MC_NOTIFICATION_EVENT, onNotification as EventListener)
    }
  }, [botName, avatar, dispatch, navigate, appStore])
}
