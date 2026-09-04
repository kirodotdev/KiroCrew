/**
 * Ratchet: the files converted to the shared async confirm surface must never
 * regress to `window.confirm`. The native dialog is synchronous — it freezes
 * the renderer's event loop, so a Quit event queued behind it fires on
 * dismissal and can tear the app down before the follow-up request is sent —
 * and it renders as an unthemeable OS sheet that leaks the origin string.
 *
 * Deliberately scoped to the converted files, not the whole tree: the
 * remaining `window.confirm` sites are undo candidates whose per-operation
 * fate (keep a dialog vs become undo-able) is decided in issue #700, and this
 * ratchet must stay green until that decision lands.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// Call sites only — comments legitimately mention window.confirm by name.
const CALL = /window\.confirm\s*\(/g

const count = (rel: string) =>
  (readFileSync(resolve(SRC, rel), 'utf-8').match(CALL) ?? []).length

describe('window.confirm ratchet (converted files)', () => {
  it.each([
    'components/ConfirmDialog.tsx',
    'components/MarkdownPanel.tsx',
    'pages/ArtifactDeployPage.tsx',
  ])('%s never calls window.confirm', rel => {
    expect(count(rel)).toBe(0)
  })

  // Partially converted files: one site each remains on purpose — it is an
  // undo candidate owned by #700. The ceiling may go DOWN, never up.
  it.each([
    ['pages/ArtifactDetailPage.tsx', 1],
    ['apps/papyrus/PapyrusPage.tsx', 1],
  ])('%s keeps at most %i deferred window.confirm site(s)', (rel, ceiling) => {
    expect(count(rel as string)).toBeLessThanOrEqual(ceiling as number)
  })
})
