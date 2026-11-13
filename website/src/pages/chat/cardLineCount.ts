/** How many lines a session card's title or preview may wrap onto. Kept out of
 *  ChatSettings so the many tests that mock that module still get the guard. */
export type CardLineCount = 1 | 2 | 3
export const CARD_LINE_OPTIONS = ['1', '2', '3'] as const
export const isCardLineCount = (v: unknown): v is CardLineCount => v === 1 || v === 2 || v === 3
