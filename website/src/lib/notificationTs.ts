/** Unique `ts` for a client-side notification that the feed can still PARSE.
 *  `addNotification` dedupes on `ts`, so two entries in the same millisecond would
 *  see the second silently dropped — which for a payload-carrying entry discards
 *  the user's message. The disambiguator goes in FRACTIONAL digits because
 *  `parseTs` only accepts `\d+(\.\d+)?`; a `<ms>-<n>` form falls through to
 *  `new Date(string)`, which is Invalid Date in V8 → "Invalid Date" headers and
 *  "NaNd ago" in the bell feed. */
let notificationTsSeq = 0
export const uniqueNotificationTs = (): string => `${Date.now()}.${notificationTsSeq++}`
