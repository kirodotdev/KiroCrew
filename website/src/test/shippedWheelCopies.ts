/**
 * The apps whose wheel copy still ships, read from the list the backend census reads
 * (`test/shipped_wheel_copies.txt`, one app per line).
 *
 * A test that walks the shipped manifests or the builtin registry needs a floor under
 * its walk, or a walk that finds nothing passes every case. A count floor erodes with
 * each wheel-copy deletion and turns red on an unrelated one; comparing the walk with
 * this list by name does not, because each deletion removes its own line. The list
 * only shrinks: Kiro Crew accepts no new built-in app.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

export const SHIPPED_WHEEL_COPIES_LIST = resolve(__dirname, '..', '..', '..', 'test', 'shipped_wheel_copies.txt')

/** The listed app names, sorted: every line that is neither blank nor a `#` comment. */
export function shippedWheelCopies(): string[] {
  return readFileSync(SHIPPED_WHEEL_COPIES_LIST, 'utf8')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '' && !line.startsWith('#'))
    .sort()
}

/**
 * What a walk found that the list does not name (`added`) and what the list names
 * that the walk did not find (`dropped`). Both empty means the walk saw every shipped
 * copy and nothing else. A deletion removes its app's line from the list; a dropped
 * copy that still ships means the walk stopped reading it.
 */
export function wheelCopyDrift(found: Iterable<string>): { added: string[]; dropped: string[] } {
  const seen = new Set(found)
  const listed = shippedWheelCopies()
  return {
    added: [...seen].filter((name) => !listed.includes(name)).sort(),
    dropped: listed.filter((name) => !seen.has(name)),
  }
}
