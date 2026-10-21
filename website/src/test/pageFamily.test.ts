import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { isPageOwner, pageFamilyFiles, readPageFamily } from './pageFamily'

let dir = ''
/** A pages directory holding exactly `files`, each containing its own name. */
function pagesDir(files: string[]): string {
  dir = mkdtempSync(join(tmpdir(), 'page-family-'))
  for (const f of files) writeFileSync(join(dir, f), f)
  return dir
}
afterEach(() => {
  if (dir) rmSync(dir, { recursive: true, force: true })
  dir = ''
})

describe('pageFamily', () => {
  it('takes the stem plus a capitalised part, never a test, the facade or another page', () => {
    for (const f of ['ArtifactsPageGallery.tsx', 'artifactsPageModel.ts', 'useArtifactsPageCreate.ts']) {
      expect(isPageOwner('ArtifactsPage', f), f).toBe(true)
    }
    for (const f of [
      'ArtifactsPage.tsx', 'ArtifactsDetailPage.tsx', 'ArtifactDetailPage.tsx', 'ArtifactsPageSubPage.tsx',
      'ArtifactsPage.sort.test.tsx', 'ArtifactsPagegallery.tsx', 'artifactsPage.ts', 'useArtifactsPage.ts',
    ]) {
      expect(isPageOwner('ArtifactsPage', f), f).toBe(false)
    }
  })

  it('lists the facade first, then its owners sorted by name', () => {
    const d = pagesDir(['useXPageB.ts', 'XPageZ.tsx', 'XPage.tsx', 'xPageA.ts', 'XPageC.tsx', 'YPage.tsx'])
    expect(pageFamilyFiles('XPage', d)).toEqual(['XPage.tsx', 'XPageC.tsx', 'XPageZ.tsx', 'useXPageB.ts', 'xPageA.ts'])
    expect(readPageFamily('XPage', d)).toBe('XPage.tsx\nXPageC.tsx\nXPageZ.tsx\nuseXPageB.ts\nxPageA.ts')
  })

  it('throws when the facade is missing', () => {
    const d = pagesDir(['XPageC.tsx'])
    expect(() => pageFamilyFiles('XPage', d)).toThrow(/no facade XPage\.tsx/)
  })

  it('throws when no owner matches, so a mistyped stem cannot make a join read only the facade', () => {
    const d = pagesDir(['XPage.tsx', 'XDetailPage.tsx', 'XPage.view.test.tsx'])
    expect(() => pageFamilyFiles('XPage', d)).toThrow(/no owner of XPage\.tsx/)
  })

  it('finds the split pages it is read for', () => {
    expect(pageFamilyFiles('ArtifactsPage')[0]).toBe('ArtifactsPage.tsx')
    expect(pageFamilyFiles('ArtifactsPage')).toEqual(expect.arrayContaining(['ArtifactsPageGallery.tsx', 'ArtifactsPageFolders.tsx']))
    expect(pageFamilyFiles('KiroCrewAgentsPage')[0]).toBe('KiroCrewAgentsPage.tsx')
    expect(pageFamilyFiles('KiroCrewAgentsPage')).toEqual(expect.arrayContaining(['KiroCrewAgentsPageFields.tsx', 'KiroCrewAgentsPageRoster.tsx']))
  })
})
