/**
 * Web Push subscription hook.
 *
 * Wraps the browser Push API so the settings panel can offer a single
 * gesture-driven toggle. `subscribe()` MUST be called from a user gesture
 * (a click handler) — `Notification.requestPermission()` and
 * `PushManager.subscribe()` both require one, and permission denial is sticky.
 *
 * iOS only exposes the Push API to a HOME-SCREEN-INSTALLED PWA (standalone
 * display mode); in a Safari tab `serviceWorker`/`PushManager` are absent, so
 * `supported` is false and the UI shows an install hint instead of a toggle
 * that cannot work.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

/** Decode a base64url VAPID key into the BufferSource applicationServerKey wants.
 *  Backed by an explicit ArrayBuffer (not the ArrayBufferLike a bare
 *  `new Uint8Array(n)` infers), so it satisfies the DOM `BufferSource` overload
 *  under strict lib types (a plain Uint8Array<ArrayBufferLike> is rejected). */
function urlBase64ToArrayBuffer(base64: string): ArrayBuffer {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(normalized)
  const buffer = new ArrayBuffer(raw.length)
  const view = new Uint8Array(buffer)
  for (let i = 0; i < raw.length; i++) view[i] = raw.charCodeAt(i)
  return buffer
}

/** True when an existing subscription was minted against `wantKey`. A mismatch
 *  means the server's VAPID key rotated under a live subscription, so pushes to
 *  it now fail 401/403 and it must be re-created. Compares the raw
 *  applicationServerKey bytes; if the browser does not expose them
 *  (`options.applicationServerKey` absent), assume a match rather than churn a
 *  working subscription on every toggle. */
function applicationServerKeyMatches(sub: PushSubscription, wantKey: ArrayBuffer): boolean {
  const have = sub.options?.applicationServerKey
  if (!have) return true
  const a = new Uint8Array(have)
  const b = new Uint8Array(wantKey)
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}

function isStandalone(): boolean {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches ||
    // iOS Safari legacy flag
    (navigator as unknown as { standalone?: boolean }).standalone === true
  )
}

export interface PushSubscriptionState {
  /** Push API usable in this context (SW + PushManager present). */
  supported: boolean
  /** iOS in a browser tab: needs home-screen install before push works. */
  needsInstall: boolean
  /** There is a live push subscription for this device. */
  subscribed: boolean
  /** A subscribe/unsubscribe call is in flight. */
  busy: boolean
  /** Last error message, or null. */
  error: string | null
  subscribe: () => Promise<void>
  unsubscribe: () => Promise<void>
}

export function usePushSubscription(): PushSubscriptionState {
  const supported =
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator &&
    typeof PushManager !== 'undefined'
  const needsInstall =
    !supported &&
    typeof navigator !== 'undefined' &&
    /iphone|ipad|ipod/i.test(navigator.userAgent) &&
    !isStandalone()

  const [subscribed, setSubscribed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reflect any existing subscription on mount (no gesture needed to read).
  useEffect(() => {
    if (!supported) return
    let cancelled = false
    navigator.serviceWorker.ready
      .then((reg) => reg.pushManager.getSubscription())
      .then((sub) => {
        if (!cancelled) setSubscribed(!!sub)
      })
      .catch(() => {
        /* reading state must not surface an error */
      })
    return () => {
      cancelled = true
    }
  }, [supported])

  const subscribe = useCallback(async () => {
    if (!supported) return
    setBusy(true)
    setError(null)
    try {
      const permission = await Notification.requestPermission()
      if (permission !== 'granted') {
        setError(
          permission === 'denied'
            ? i18nT('pages.settings.notificationsPanel.push_error_blocked')
            : i18nT('pages.settings.notificationsPanel.push_error_permission'),
        )
        return
      }
      const reg = await navigator.serviceWorker.ready
      const { publicKey } = await api.vapidPublicKey()
      const wantKey = urlBase64ToArrayBuffer(publicKey)
      const existing = await reg.pushManager.getSubscription()
      // A subscription minted against a since-rotated VAPID key (e.g. the
      // keystore file was regenerated) keeps working locally but every push to
      // it fails 401/403 — not 404/410 — so the server never prunes it and the
      // user silently stops receiving notifications. Detect the mismatch here
      // and re-subscribe against the current key so the stored subscription is
      // always bound to the key the server actually signs with.
      let sub = existing
      if (existing && !applicationServerKeyMatches(existing, wantKey)) {
        await existing.unsubscribe().catch(() => {
          /* best-effort: a stale local sub must not block re-subscribing */
        })
        sub = null
      }
      sub =
        sub ||
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: wantKey,
        }))
      await api.subscribePush(sub.toJSON())
      setSubscribed(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('pages.settings.notificationsPanel.push_error_subscribe'))
    } finally {
      setBusy(false)
    }
  }, [supported])

  const unsubscribe = useCallback(async () => {
    if (!supported) return
    setBusy(true)
    setError(null)
    try {
      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.getSubscription()
      if (sub) {
        await api.unsubscribePush(sub.endpoint).catch(() => {
          /* server prune is best-effort; local unsubscribe is what matters */
        })
        await sub.unsubscribe()
      }
      setSubscribed(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('pages.settings.notificationsPanel.push_error_unsubscribe'))
    } finally {
      setBusy(false)
    }
  }, [supported])

  return { supported, needsInstall, subscribed, busy, error, subscribe, unsubscribe }
}
