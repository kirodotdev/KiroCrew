/**
 * The "Send follow-ups with one click" setting (Settings, Chat, Composer).
 *
 * On, a follow-up card's Start sends the agent-written prompt as the new
 * session's first message; off (the default), Start puts it in the new
 * session's message box and nothing runs until the person presses send
 * (rfc-followup-start-sends.md).
 *
 * It is its own browser-local key, read and written with `usePersistedBool`
 * (only the stored string '1' is on), and deliberately NOT a `ChatConfig`
 * field and NOT in `DURABLE_PREF_KEYS`: the uiPrefs host backup lives in the
 * agent-writable data home (`~/.kiro/crew/ui-prefs.json`), so a restorable
 * value would let an agent switch on one-click running of its own prompts for
 * the person's next fresh origin. The rule is the one `mc-yolo-ack` follows: a
 * value that gates a human confirmation must not be restorable from a file the
 * agent can write. No agent change card can reach it either.
 */
export const FOLLOWUP_START_SENDS_KEY = 'mc-followup-start-sends'
