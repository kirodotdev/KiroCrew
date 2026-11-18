/**
 * Settings search registry id of the Kiro sign-in card (`settingsManual.ts`), so
 * "sign out" and "switch account" find it. Search opens `?highlight=<this id>`
 * rather than `key:<anchor>`; the card carries both. A leaf module so the search
 * core can read it without pulling the highlight hook and the settings
 * components into its chunk.
 */
export const KIRO_SIGN_IN_SETTING_ID = 'kiro-account.kiro-sign-in'
/** The id the entry had while the card lived on the Agent Harness tab. Old
 *  links and palette history still carry it; Settings forwards them. */
export const LEGACY_KIRO_SIGN_IN_SETTING_ID = 'agent.kiro-sign-in'
