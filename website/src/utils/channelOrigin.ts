/**
 * Channel-origin helpers for chat slots.
 *
 * When the backend surfaces a conversation that started on Slack/Discord/Teams
 * (etc.), it mints the slot key FROM that channel's session key
 * (`slack:1785370133.085469` -> `slack_1785370133.085469`), deterministically.
 * The key is therefore the record of where the conversation started, and it
 * survives every restore path for free because it is the slot's identity — no
 * extra payload field is needed.
 *
 * Mirrors `CHANNEL_SESSION_NAMESPACES` in `src/kiro_crew/messaging/link.py`.
 * Keep the two lists in sync.
 */

/** Display label per channel namespace. */
const CHANNEL_LABELS: Record<string, string> = {
  slack: 'Slack',
  discord: 'Discord',
  telegram: 'Telegram',
  whatsapp: 'WhatsApp',
  webex: 'Webex',
  wecom: 'WeCom',
  teams: 'Teams',
  weixin: 'Weixin',
  unified: 'Direct message',
}

/** Mirrors messaging.link.is_legacy_slack_key for pre-namespace history rows. */
export function isLegacySlackSlotKey(slotKey?: string): boolean {
  return Boolean(slotKey && /^\d+\.\d+$/.test(slotKey))
}

/**
 * Return the channel namespace a slot originated from, or `''` for an ordinary
 * dashboard session.
 *
 * Callers that need to vary a SENTENCE by channel want this rather than the
 * label: `unified` has no proper noun to interpolate, and an English fragment
 * injected into a translated string cannot be fixed by the translation.
 */
export function slotChannelNamespace(slotKey?: string): string {
  if (!slotKey) return ''
  if (isLegacySlackSlotKey(slotKey)) return 'slack'
  for (const ns of Object.keys(CHANNEL_LABELS)) {
    if (slotKey.startsWith(`${ns}:`) || slotKey.startsWith(`${ns}_`)) {
      return ns
    }
  }
  return ''
}

/**
 * Return the display label of the channel a slot originated from, or `''` for an
 * ordinary dashboard session.
 *
 * Match is case-sensitive on purpose: the backend always mints these keys
 * lowercase, so a user-titled session like `Slack_thread_triage` (capital S,
 * from the title-derived slot name) is correctly NOT labelled as channel-origin.
 *
 * Accepts both separators — a live session key uses `slack:<ts>` while a slot
 * key and the persisted session index use `slack_<ts>` (the history layer folds
 * `:` to `_`).
 */
export function slotChannelLabel(slotKey?: string): string {
  const ns = slotChannelNamespace(slotKey)
  return ns ? CHANNEL_LABELS[ns] : ''
}
