/**
 * Settings search registry id of the Kiro sign-in card (`settingsManual.ts`), so
 * "sign out" and "switch account" find it. Search opens `?highlight=<this id>`
 * rather than `key:<anchor>`, so the Agent Harness tab accepts both to open the
 * KAS row. A leaf module so the search core can read it without pulling the
 * highlight hook and the settings components into its chunk.
 */
export const KIRO_SIGN_IN_SETTING_ID = 'agent.kiro-sign-in'
