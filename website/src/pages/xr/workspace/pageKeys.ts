/**
 * The names of the keys that a page of the workspace takes as keys. They are the DOM's own `KeyboardEvent.key`
 * values, sent to the gateway as they are and matched there by value: names of keys, never words on the screen.
 * This module holds those names and nothing else, and imports nothing.
 */

/** The keys a page takes as keys: the gateway's own list (`browser_harness/panel_hub.py`, `PANEL_KEYS`). A test holds the two together. */
export const PAGE_KEYS: readonly string[] = ['Enter', 'Backspace', 'Delete', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown']

/** The four that move a caret or a selection. */
export const ARROW_KEYS: readonly string[] = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']

export const ENTER_KEY = 'Enter'
export const BACKSPACE_KEY = 'Backspace'
