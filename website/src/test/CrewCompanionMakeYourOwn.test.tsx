/**
 * "Make your own" must be able to create a pack.
 *
 * The bug: saving a brand-new pack failed with "That pack needs a name" (a misleading
 * message -- it guards the internal id, not the human name). Two-part cause:
 *   1. `useSaveWithDialog` sends a FIRST save through `doSave(false)` -- there is no
 *      existing pack to prompt an overwrite dialog against, so `asNew` is false.
 *   2. `PackEditor` only minted an id when `asNew` was true, so a new pack carried `''`
 *      and the backend refused it.
 * The fix mints an id whenever there is no `existingPack`. These pin BOTH halves so the
 * regression cannot come back through either one.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import { useSaveWithDialog } from '../apps/crew-companion/editorHooks'

describe('make-your-own save routing', () => {
  it('sends a first-time save (no existing pack) straight through doSave(false)', () => {
    const { result } = renderHook(() => useSaveWithDialog(undefined, false))
    const doSave = vi.fn()
    act(() => result.current.triggerSave(doSave))
    // No overwrite dialog for a pack that does not exist yet.
    expect(result.current.showSaveDialog).toBe(false)
    expect(doSave).toHaveBeenCalledWith(false)
  })

  it('mints an id on the doSave(false) path when there is no existing pack', () => {
    // The other half lives in PackEditor's id expression. Assert its shape directly:
    // the mint must trigger on `!existingPack`, not on `asNew` alone (the old bug).
    const src = readFileSync('src/apps/crew-companion/PackEditor.tsx', 'utf8')
    expect(src).toMatch(/asNew\s*\|\|\s*!existingPack\s*\?\s*crypto\.randomUUID\(\)/)
    // And the old broken form -- mint only on asNew -- must be gone.
    expect(src).not.toMatch(/id:\s*asNew\s*\?\s*crypto\.randomUUID\(\)\s*:/)
  })

  it('draws the current avatar in the breathing exercise, not a hardcoded ghost', () => {
    // The breathing overlay renders in panel.tsx (a window layer), so it can and should
    // follow the picked appearance. It used to import the built-in kiro_idle.svg
    // directly, which showed the default ghost even under a custom pack.
    const src = readFileSync('src/apps/crew-companion/BreathingOverlay.tsx', 'utf8')
    expect(src).toMatch(/import\s*\{\s*PetAvatar\s*\}/)
    // No import of, or reference to, the hardcoded built-in asset. Scoped to real
    // statements (import / src=) so the historical note in a comment does not match.
    expect(src).not.toMatch(/import\s+\w+\s+from\s+['"][^'"]*kiro_idle/)
    expect(src).not.toMatch(/\bghostIdleUrl\b(?!`)/)
  })
})
