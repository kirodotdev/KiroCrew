/**
 * The notification center: list, delete, clear, ack/unack/ack-all, the
 * per-channel notification settings and the gateway's Do Not Disturb.
 */

import type { ClientTransport } from './transport'

export function createNotificationsEndpoints({ post, put, del, j, jfetch: fetch }: ClientTransport) {
  const inbox = {
    // Notifications
    notifications: () => fetch('/api/notifications').then(j),
    deleteNotification: (ts: string) => del('/api/notifications', { ts }).then(j),
    clearNotifications: () => post('/api/notifications/clear').then(j),
    ackNotification: (ts: string) => post('/api/notifications/ack', { ts }).then(j),
    unackNotification: (ts: string) => post('/api/notifications/unack', { ts }).then(j),
    ackAllNotifications: () => post('/api/notifications/ack-all').then(j),
    notificationChannels: () => fetch('/api/notifications/channels').then(j),
    updateNotificationChannelSettings: (channel: string, settings: { muted?: boolean; priority?: string | null; deliver_to?: string[]; deliver_min_priority?: string }) =>
      put('/api/notifications/channels/settings', { channel, ...settings }).then(j),
    // The gateway's Do Not Disturb: `until` is epoch seconds, null when off.
    notificationDnd: () => fetch('/api/notifications/dnd').then(j) as Promise<{ until: number | null }>,
    setNotificationDnd: (until: number | null) =>
      put('/api/notifications/dnd', { until }).then(j) as Promise<{ until: number | null }>,
    // Web Push (browser subscription lifecycle)
    vapidPublicKey: () => fetch('/api/notifications/push/vapid-public-key').then(j) as Promise<{ publicKey: string }>,
    subscribePush: (subscription: PushSubscriptionJSON) =>
      post('/api/notifications/push/subscribe', subscription as unknown as object).then(j),
    unsubscribePush: (endpoint: string) =>
      post('/api/notifications/push/unsubscribe', { endpoint }).then(j),
  }

  return { inbox }
}
