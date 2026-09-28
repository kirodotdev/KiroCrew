/**
 * Request paths for the chat-slot API, owned in ONE place.
 *
 * `client.ts` builds the request from these; a caller that must NAME the request
 * without making it — `switchSlotFailureReport` journaling a REJECTED slot-detail
 * fetch, for which no Response exists — reads the same helper, so a route change
 * cannot leave the journal naming a path that was never requested.
 *
 * Deliberately not exported from `client.ts`: ~600 test files replace that module
 * wholesale with `vi.mock('../api/client', () => ({ api: {...} }))`, and vitest
 * throws on any export the factory omits. A named export there would turn every
 * such test's failure path into a throw inside the very catch that records the
 * failure (see `utils/agentSwitchFeedback.ts` for the precedent).
 */

/** Path of `GET /api/chat/slots/{slot}` (the slot-detail request), with the key
 *  encoded exactly as the request sends it. Query string excluded — the journal
 *  stores paths only. */
export const chatSlotDetailPath = (slot: string): string => '/api/chat/slots/' + encodeURIComponent(slot)
