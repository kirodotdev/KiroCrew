/**
 * Typing in the flat preview: which keys of a real keyboard go to a page, when the phone's own text field shows,
 * and what a change in that field means for the page.
 *
 * The page is a picture, so it has no caret that a keyboard could find. On a desktop the canvas hears the keys and
 * hands them to the page that was pressed last. On a phone no keyboard comes up for a canvas, so the preview shows
 * one text field of its own: the phone's keyboard types into it, and what changes there is sent on to the page
 * (`typed`), by the same `text()` and `key()` of the page's source that a test's script uses.
 *
 * Whether the page takes any of it is the gateway's to say: with the page's focus inside a surface that an agent
 * raised it sends nothing on, and the panel says its line. These are plain functions; the workspace wires them.
 */
import { ARROW_KEYS, PAGE_KEYS } from './pageKeys'
import type { PageFocus } from './source'

export { PAGE_KEYS } from './pageKeys'
const KEYS = new Set(PAGE_KEYS)
const ARROWS = new Set(ARROW_KEYS)

/** What of a key event the rule reads. */
export interface Stroke { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; shiftKey?: boolean; isComposing?: boolean }

/** Where a key of a real keyboard goes: to the page as a key or as text, to the preview's head, or nowhere of ours. */
export type KeyFate = { to: 'key'; key: string } | { to: 'text'; text: string } | { to: 'head' } | { to: 'browser' }

/**
 * `onPage`: the pointer's last press was on a page, and that page takes input. `focus`: where that page's focus is,
 * as the gateway last said.
 *
 * - A key with Ctrl, Alt or the command key is the browser's own (reload, copy), and so is a key of an input method.
 * - Shift with Tab is the browser's own too: it is the way out of the canvas for a person who uses no pointer.
 * - With no page pressed, the arrow keys turn the head and nothing else is ours.
 * - With a page pressed, its keys go to it. The arrow keys go to it only while a field of it has the focus: with
 *   none, they still turn the head.
 * - One character is text for the page.
 */
export function keyFate(stroke: Stroke, onPage: boolean, focus: PageFocus | null): KeyFate {
  if (stroke.ctrlKey || stroke.metaKey || stroke.altKey || stroke.isComposing) return { to: 'browser' }
  if (stroke.key === 'Tab' && stroke.shiftKey) return { to: 'browser' }
  const arrow = ARROWS.has(stroke.key)
  if (!onPage) return arrow ? { to: 'head' } : { to: 'browser' }
  if (arrow) return focus?.editable ? { to: 'key', key: stroke.key } : { to: 'head' }
  if (KEYS.has(stroke.key)) return { to: 'key', key: stroke.key }
  // One character, counted as the person sees it: a letter outside the basic plane is two units and one character.
  if ([...stroke.key].length === 1) return { to: 'text', text: stroke.key }
  return { to: 'browser' }
}

/** The page that takes the keys, and whether a finger's press gave them to it. */
export interface KeysPage { id: string; touch: boolean }

/**
 * Which page the keys are for after something happened. A press on a page gives them to that page. A press on
 * empty space, and a picture that turns away to a page that was opened, take them from every page; the close of
 * the page that has them does too. A page that comes up by itself changes nothing: the person types on where he
 * typed, and the keys go to the new page only after a press on it.
 */
export function keysAfter(now: KeysPage | null, what: { press: KeysPage } | { away: true } | { closed: string } | { cameUp: string }): KeysPage | null {
  if ('press' in what) return what.press
  if ('away' in what) return null
  if ('closed' in what) return now?.id === what.closed ? null : now
  return now
}

/**
 * Whether the phone's field shows: the last press on the page was a finger's, the page's focus is in a field that
 * takes text, that field is not guarded, and the person has not put the field away since that press.
 */
export function wantsField(touch: boolean, focus: PageFocus | null, putAway: boolean): boolean {
  return touch && !putAway && focus !== null && focus.editable && !focus.guarded
}

/**
 * What a change of the phone's field means for the page: take back `back` characters, then add `text`.
 *
 * The field holds what was typed since it opened, and the page's caret is at the end of that. So whatever the
 * phone's keyboard did to the field (a letter, a correction of the last word, a paste) is the end of the old text
 * taken away and a new end put on. Characters are counted as the page's Backspace takes them: a letter outside the
 * basic plane is one. shortcut: a caret that the person moved inside the page, and a sign made of several joined
 * characters, are not followed; upgrade when the page can say where its caret is.
 */
export function typed(before: string, after: string): { back: number; text: string } {
  const a = [...before]
  const b = [...after]
  let same = 0
  while (same < a.length && same < b.length && a[same] === b[same]) same += 1
  return { back: a.length - same, text: b.slice(same).join('') }
}
