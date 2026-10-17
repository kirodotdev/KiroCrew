/**
 * The files the Crewmates page is made of: the `MembersPage.tsx` facade and the
 * owners beside it (`MembersPage.owners.test.tsx` pins how they compose). This
 * is the one place the page's file rule lives: the owners test and every source
 * pin whose negative half must hold over the whole page read it through here, so
 * a new owner is read without editing each pin, and a rule that stops matching
 * fails here instead of leaving those pins nothing to scan.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

export const MEMBERS_PAGE_DIR = resolve(__dirname, '..', 'pages', 'members')
export const MEMBERS_PAGE_FACADE = 'MembersPage.tsx'

/** The page's files by name: the facade, `MembersPage<Part>.tsx`, `membersPage*.ts`
 *  and `useMembersPage*.ts`. A test file never matches. */
const MEMBERS_PAGE_FILE_RE = /^(MembersPage(?:[A-Z]\w*)?\.tsx|membersPage\w*\.ts|useMembersPage\w*\.ts)$/

/** Every file of the page, the facade first. Throws when the rule no longer finds
 *  the facade and the roster column, which every pin over the page reads. */
export function membersPageFiles(): string[] {
  const files = readdirSync(MEMBERS_PAGE_DIR).filter((f) => MEMBERS_PAGE_FILE_RE.test(f)).sort()
  for (const required of [MEMBERS_PAGE_FACADE, 'MembersPageRoster.tsx']) {
    if (!files.includes(required)) throw new Error(`membersPageFiles() no longer finds ${required} in ${MEMBERS_PAGE_DIR}`)
  }
  return [MEMBERS_PAGE_FACADE, ...files.filter((f) => f !== MEMBERS_PAGE_FACADE)]
}

/** Every file of the page as an absolute path, the facade first. */
export const membersPagePaths = (): string[] => membersPageFiles().map((f) => join(MEMBERS_PAGE_DIR, f))

/** The page's owners: every file of the page but the facade. */
export const membersPageOwners = (): string[] => membersPageFiles().slice(1)

/** The text of every file of the page, for a negative pin that must hold over all of it. */
export const readMembersPage = (): string =>
  membersPageFiles().map((f) => readFileSync(join(MEMBERS_PAGE_DIR, f), 'utf8')).join('\n')
