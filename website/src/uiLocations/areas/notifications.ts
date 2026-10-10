/**
 * Registered UI locations for the notification feed and detail panel, drawn
 * both in the top bar's bell popover and on the Notifications page. One area of
 * `UI_LOCATIONS`; see `../descriptors.ts` for the two-step contract. Created
 * empty ahead of its area batch, so filling it never edits the aggregator.
 */
import type { UiLocationArea } from '../types'

// NotificationFeed draws its controls differently per host: the bell popover
// uses the `mac` variant (icon buttons in the controls card), the
// Notifications page the `panel` variant (text buttons beside the search box).
// Those are separate render sites, so each gets its own id. The detail panel is
// one component both hosts render, so its controls get one id, two placements.
export const LOCATIONS = {
  'notifications.mark-all-read': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['mark everything read', 'read all', 'clear unread', 'dismiss unread badge'],
      'zh-CN': ['全部已读', '一键已读', '清除未读'],
    },
    placements: [{
      surface: 'shell', parent: 'shell.notifications', entry: 'menu',
      requires: [{ kind: 'condition', id: 'has_unread_notifications' }],
    }],
  },
  'notifications.clear-all': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['delete all notifications', 'remove all notifications', 'empty the inbox'],
      'zh-CN': ['删除所有通知', '清空通知', '清空收件箱'],
    },
    placements: [{
      surface: 'shell', parent: 'shell.notifications', entry: 'menu',
      requires: [{ kind: 'condition', id: 'has_notifications' }],
    }],
  },
  // Visible text "All" (with a check icon): the page's mark-all-read.
  'notifications.page-mark-all-read': {
    kind: 'button',
    aliasKeys: ['components.notifications.notificationFeed.mark_all_as_read'],
    terms: {
      en: ['mark everything read', 'read all', 'clear unread'],
      'zh-CN': ['全部已读', '一键已读', '清除未读'],
    },
    placements: [{
      surface: 'notifications', parent: 'page.notifications', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'has_unread_notifications' }],
    }],
  },
  // Visible text "Clear"; it asks to confirm before clearing every notification.
  'notifications.page-clear-all': {
    kind: 'button',
    aliasKeys: ['components.notifications.notificationFeed.clear_all_notifications'],
    terms: {
      en: ['delete all notifications', 'remove all notifications', 'empty the inbox'],
      'zh-CN': ['删除所有通知', '清空通知', '清空收件箱'],
    },
    placements: [{
      surface: 'notifications', parent: 'page.notifications', entry: 'toolbar',
      requires: [{ kind: 'condition', id: 'has_notifications' }],
    }],
  },
  // Drawn only for a read notification (NotificationDetailPanel: `n.acked`);
  // its unread twin, "Mark read", is the other branch of the same ternary.
  // Opening one usually marks it read, but not always, so it is a condition.
  'notifications.detail.mark-unread': {
    kind: 'button',
    terms: {
      en: ['mark as unread', 'keep unread', 'mark it new again'],
      'zh-CN': ['设为未读', '保留未读'],
    },
    placements: [
      {
        surface: 'shell', parent: 'shell.notifications', entry: 'content',
        requires: [{ kind: 'condition', id: 'notification_selected' }, { kind: 'condition', id: 'notification_read' }],
      },
      {
        surface: 'notifications', parent: 'page.notifications', entry: 'content',
        requires: [{ kind: 'condition', id: 'notification_selected' }, { kind: 'condition', id: 'notification_read' }],
      },
    ],
  },
  // The keep-or-mute prompt under the first notification from a new channel.
  // One body (`promptBody`) that both hosts draw, so one id, two placements.
  'notifications.mute-channel': {
    kind: 'button',
    terms: {
      en: ['stop notifications from an app', 'silence a channel', 'mute an app', 'stop these notifications', 'block a notification source'],
      'zh-CN': ['屏蔽渠道', '静音渠道', '不再接收这些通知', '屏蔽应用通知'],
    },
    placements: [
      {
        surface: 'shell', parent: 'shell.notifications', entry: 'content',
        requires: [{ kind: 'condition', id: 'new_channel_prompt' }],
      },
      {
        surface: 'notifications', parent: 'page.notifications', entry: 'content',
        requires: [{ kind: 'condition', id: 'new_channel_prompt' }],
      },
    ],
  },
} as const satisfies UiLocationArea
