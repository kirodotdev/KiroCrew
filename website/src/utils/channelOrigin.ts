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

import { i18nT } from '../i18n/t'

/**
 * Display label per channel namespace whose name is a PROPER NOUN — the product
 * that hosts the conversation. Deliberately NOT translated and deliberately not
 * in the catalog: "Slack" is "Slack" in every locale, and a catalog entry would
 * only invite a translator to render it in a local script.
 */
const CHANNEL_BRAND: Record<string, string> = {
  slack: 'Slack',
  discord: 'Discord',
  telegram: 'Telegram',
  whatsapp: 'WhatsApp',
  webex: 'Webex',
  wecom: 'WeCom',
  teams: 'Teams',
  weixin: 'Weixin',
}

/**
 * Catalog KEY for the namespaces whose label is real English COPY rather than a
 * brand. Only `unified` qualifies: it is the "no external channel" case, so
 * there is no product name to show and the label is a phrase that must be
 * translated.
 *
 * A key and not an `i18nT()` call: this table is evaluated at module load, so a
 * call here would freeze the boot language. The lookup happens in
 * `slotChannelLabel()`, which callers invoke during render. Shaped as a flat
 * `Record` of full literal keys and indexed inline at the `i18nT()` call, which
 * is the form `scripts/check-i18n-keys.mjs` can resolve statically.
 */
const CHANNEL_LABEL_KEY: Record<string, string> = {
  unified: 'utils.channelOrigin.direct_message',
}

/**
 * Every recognised namespace, in match order. DERIVED from the two tables above
 * rather than written out a third time, so a channel added to either one is
 * matched by `slotChannelNamespace` automatically and the lists cannot drift.
 */
const CHANNEL_NAMESPACES = [...Object.keys(CHANNEL_BRAND), ...Object.keys(CHANNEL_LABEL_KEY)]

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
  for (const ns of CHANNEL_NAMESPACES) {
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
 *
 * This is the render-time resolver for `CHANNEL_LABEL_KEY`: every caller invokes
 * it from a render callback, so the `i18nT()` below re-evaluates on a language
 * switch. Returning `''` for a non-channel slot is load-bearing — callers use
 * the empty string as "not channel-origin" (asserted in `channelOrigin.test.ts`
 * against `slotChannelNamespace`), so a namespace that matches must always
 * produce a non-empty label.
 */
/**
 * The brand label for a CHANNEL TYPE (`"slack"`, `"discord"`), independent of any
 * session key. `slotChannelLabel` answers the same question for a slot; this is
 * for callers that already know the channel and need a label that does NOT vary
 * with connection state — a row whose label changed between "connected" and
 * "disconnected" would stop reading as one row with two states. Returns `''` for
 * an unrecognised type so the caller can fall back to whatever the wire sent.
 */
export function channelBrandLabel(channelType?: string): string {
  if (!channelType) return ''
  return Object.prototype.hasOwnProperty.call(CHANNEL_BRAND, channelType)
    ? CHANNEL_BRAND[channelType]
    : ''
}

export function slotChannelLabel(slotKey?: string): string {  const ns = slotChannelNamespace(slotKey)
  if (!ns) return ''
  // `hasOwnProperty`, not `in`: an inherited Object.prototype member such as
  // `toString` would otherwise resolve to a function and be handed to i18next.
  return Object.prototype.hasOwnProperty.call(CHANNEL_LABEL_KEY, ns)
    ? i18nT(CHANNEL_LABEL_KEY[ns])
    : CHANNEL_BRAND[ns]
}
