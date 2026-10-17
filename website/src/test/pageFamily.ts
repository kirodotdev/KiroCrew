/**
 * The files one split page is made of: its facade `pages/<Stem>.tsx` and the
 * owners beside it (`<Stem><Part>.tsx`, `<stem><Part>.ts`, `use<Stem><Part>.ts`).
 * A source pin whose negative or every-site half must hold over the whole page
 * reads it through here, so a new owner is read without editing the pin, and a
 * rule that stops matching fails here instead of leaving the pin nothing to scan.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

export const PAGES_DIR = resolve(__dirname, '..', 'pages')

/** Whether `file` is an owner of the page `stem`: the stem, then a capitalised
 *  part. A test, another page's facade (`<...>Page.tsx`) and the facade itself
 *  never match, so `ArtifactsPage` does not take in `ArtifactsDetailPage.tsx`. */
export function isPageOwner(stem: string, file: string): boolean {
  if (file.includes('.test.') || (file.endsWith('Page.tsx') && file !== `${stem}.tsx`)) return false
  const lower = stem.charAt(0).toLowerCase() + stem.slice(1)
  return new RegExp(`^(${stem}[A-Z]\\w*\\.tsx|${lower}[A-Z]\\w*\\.ts|use${stem}[A-Z]\\w*\\.ts)$`).test(file)
}

/** The page's files, facade first, then its owners sorted by name. Throws when
 *  the facade is missing or no owner matches: every caller reads a split page. */
export function pageFamilyFiles(stem: string, dir: string = PAGES_DIR): string[] {
  const files = readdirSync(dir)
  if (!files.includes(`${stem}.tsx`)) throw new Error(`pageFamilyFiles(): no facade ${stem}.tsx in ${dir}`)
  const owners = files.filter((f) => isPageOwner(stem, f)).sort()
  if (owners.length === 0) throw new Error(`pageFamilyFiles(): no owner of ${stem}.tsx in ${dir}`)
  return [`${stem}.tsx`, ...owners]
}

/** The text of every file of the page, facade first, for a pin over all of it. */
export const readPageFamily = (stem: string, dir: string = PAGES_DIR): string =>
  pageFamilyFiles(stem, dir).map((f) => readFileSync(join(dir, f), 'utf8')).join('\n')
