/**
 * The app's own rows of three dashboard-wide policies, pinned beside the code they read.
 *
 * Each policy's core test (`src/test/bootRequests.test.ts`,
 * `src/test/hoverVariantPolicy.test.ts`, `src/test/ResizeHandle.parity.test.tsx`) pins
 * core's sites; these are the same checks on this app's files, so the app takes them
 * with it when it leaves the repository.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

const read = (rel: string) => readFileSync(join(__dirname, rel), 'utf8')

describe('Code Review Sage shared-policy rows', () => {
  it('the learning rail opts into focus refetch with a finite staleTime (replace-all PUT of active namespaces)', () => {
    // The rail builds a REPLACE-ALL write from its query cache, so the global
    // Infinity staleTime would let a stale cache overwrite another tab's change.
    expect(read('components/LearningRail.tsx')).toMatch(/staleTime:\s*\d+/)
  })

  it('a run card keeps the room its hover state reserved, so its overlay control covers no content on touch', () => {
    expect(read('components/RunCard.tsx')).toContain("(onDelete ? ' [@media(hover:none)]:pb-7' : '')")
  })

  it('the workspace column splitter mounts the shared ResizeHandle and hand-rolls no vertical separator', () => {
    const src = read('Workspace.tsx')
    expect(src).toMatch(/import ResizeHandle from '(\.\.\/)+components\/ResizeHandle'/)
    expect(src).toContain('<ResizeHandle')
    expect(src).not.toMatch(/aria-orientation="vertical"/)
  })
})
