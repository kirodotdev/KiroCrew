/**
 * The choice he printed on a Bot's words (`[OPTIONS: …]`, TALK slice 2), kept by the answer's own key (`Says.version`:
 * its message's time and place, the key the read marks use): raised again, after a reload too, those words show his
 * print and take no second one. Kept in this browser's storage; a newer answer has a key of its own.
 */
import type { Print } from './boardPicture'

/** What a print chose: the box's label, and where the print stands on it. */
export interface Chosen { label: string; print: Print }

const KEY = 'kirocrew.xr.chosen'
/** shortcut: the newest 200 choices are kept, far more than a crew's raised words; upgrade to the thread's own record when one exists. */
const MOST = 200

const book = (): Record<string, Chosen> => {
  try {
    const raw = window.localStorage.getItem(KEY)
    const got = raw ? (JSON.parse(raw) as unknown) : null
    return got && typeof got === 'object' ? (got as Record<string, Chosen>) : {}
  } catch {
    return {}
  }
}

/** The choice printed on `bot`'s answer `version`, or `null`. */
export function chosenOn(bot: string, version: string): Chosen | null {
  const got = book()[`${bot}|${version}`]
  return got && typeof got.label === 'string' && got.print ? got : null
}

/** He printed `label` on `bot`'s answer `version` (`null`: the print was taken back, its send failed). */
export function keepChosen(bot: string, version: string, chosen: Chosen | null): void {
  const all = book()
  const key = `${bot}|${version}`
  delete all[key]
  if (chosen) all[key] = chosen
  const keys = Object.keys(all)
  for (const old of keys.slice(0, Math.max(0, keys.length - MOST))) delete all[old]
  try { window.localStorage.setItem(KEY, JSON.stringify(all)) } catch { /* no store: kept for this page only by the board */ }
}
