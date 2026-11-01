/**
 * The diff-scoped i18n gates measure a MOVED file against its base-side source.
 *
 * `[vs-base]` compares a touched file's count with the same file at the base; a file
 * that only moved has no base copy at its new path, so without the rename map it reads
 * as wholly new and every frozen finding in it fails as if this branch wrote it.
 */
import { describe, expect, it } from 'vitest'

// @ts-expect-error -- a plain .mjs gate script with no type declarations
import { renameSources } from '../../scripts/check-i18n-strings.mjs'

describe('renameSources', () => {
  it('maps each renamed path to its base path and ignores every other status', () => {
    const listing = [
      'R100\twebsite/src/pages/DevFleetPage.tsx\twebsite/src/apps/dev-fleet/DevFleetPage.tsx',
      'R087\twebsite/src/test/a.test.ts\twebsite/src/apps/x/a.test.ts',
      'M\twebsite/src/apps/builtinRegistry.ts',
      'A\twebsite/src/new.ts',
      '',
    ].join('\n')
    expect([...renameSources(listing)]).toEqual([
      ['website/src/apps/dev-fleet/DevFleetPage.tsx', 'website/src/pages/DevFleetPage.tsx'],
      ['website/src/apps/x/a.test.ts', 'website/src/test/a.test.ts'],
    ])
  })

  it('is empty for a diff with no renames', () => {
    expect(renameSources('M\twebsite/src/a.ts\n').size).toBe(0)
  })
})
