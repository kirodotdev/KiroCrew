/**
 * The notification center: list, delete, clear, ack/unack/ack-all, and the
 * per-channel notification settings.
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
    updateNotificationChannelSettings: (channel: string, settings: { muted?: boolean; priority?: string | null }) =>
      put('/api/notifications/channels/settings', { channel, ...settings }).then(j),
  }

  return { inbox }
}
