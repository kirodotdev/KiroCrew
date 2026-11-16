// WIRE VALUES ONLY for the `[GRILL:]` discussion card (grillMarker.ts): the
// marker opener the conductor writes, and the key-line prefix the card adds to
// what it sends. Both are matched by value, so they are the same in every
// locale; translating them would stop the card from drawing, or from locking
// after a reload. The rest of the texts the card sends back follow the reader's
// language and live in the catalog.

/** Opens a marker line. */
export const GRILL_OPEN = '[GRILL:'

/** Opens the line naming which card a Start / No thanks answers. */
export const GRILL_KEY_PREFIX = 'key: '
