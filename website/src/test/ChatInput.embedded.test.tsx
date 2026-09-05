/**
 * The `embedded` flag is FAIL-CLOSED: every capability that defaults on for a
 * first-class composer is off under it, an explicit prop still wins, and the
 * defaults are resolved in ONE place -- so a capability added later has exactly
 * one line to touch, and cannot light up inside an app embed by convention.
 *
 * The source-shape assertion is deliberate: the invariant this protects is
 * "no capability reads its raw prop with `= true`", which a render test cannot
 * see for a capability that does not exist yet.
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const mockApi = vi.hoisted(() => ({ skills: vi.fn(), skillTrust: vi.fn(), grantSkillTrust: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

const src = readFileSync(join(__dirname, '../components/ChatInput.tsx'), 'utf8')

describe('ChatInput embedded flag', () => {
  it('resolves every defaulted capability through one embed-aware default', () => {
    expect(src).toContain('const capabilityDefault = !embedded')
    for (const cap of ['typedCommandMenus', 'slotApprovalChrome', 'promptOptimizer']) {
      expect(src).toContain(`const ${cap} = ${cap}Prop ?? capabilityDefault`)
    }
  })

  it('forbids ANY prop defaulting to true in the destructure, so a future capability cannot light up in an embed by convention', () => {
    // The invariant is general, so the assertion must be too: scan the whole
    // props destructure for `<name> = true,`. The only permitted one is
    // `connected`, which is a liveness flag (offline disables sending), not a
    // capability -- forcing it off for an embed would be wrong. A new
    // capability must default through `capabilityDefault` (or default off).
    const start = src.indexOf('function ChatInput({')
    const end = src.indexOf('}: ChatInputProps)', start)
    expect(start).toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    const destructure = src.slice(start, end)
    const trueDefaults = [...destructure.matchAll(/\n\s+(\w+) = true,/g)].map(m => m[1])
    expect(trueDefaults).toEqual(['connected'])
  })

  it('turns the typed command menus off when embedded (no skills prefetch on focus)', async () => {
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<ChatInput embedded value="" onChange={vi.fn()} onSend={vi.fn()} />)
    fireEvent.focus(screen.getByLabelText('Message input'))
    await new Promise(r => setTimeout(r, 30))
    expect(mockApi.skills).not.toHaveBeenCalled()
  })

  it('lets an explicit prop win over the flag', async () => {
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<ChatInput embedded typedCommandMenus value="" onChange={vi.fn()} onSend={vi.fn()} />)
    fireEvent.focus(screen.getByLabelText('Message input'))
    await new Promise(r => setTimeout(r, 30))
    expect(mockApi.skills).toHaveBeenCalled()
  })

  it('keeps the first-class default when not embedded', async () => {
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    fireEvent.focus(screen.getByLabelText('Message input'))
    await new Promise(r => setTimeout(r, 30))
    expect(mockApi.skills).toHaveBeenCalled()
  })
})
