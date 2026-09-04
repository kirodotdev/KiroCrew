import { readFileSync } from 'node:fs'
import path from 'node:path'

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { contributedCommands } from './contributedCommands'

/**
 * The frontend half of the contributed-command conformance suite.
 *
 * Reads the SAME fixture as `test/test_app_contribution_conformance.py`. Not extra
 * coverage — both sides have their own unit tests — but a build-breaking guarantee that
 * the two hand-written rulebooks cannot disagree about a verdict. They drifted twice
 * while this contract was being written (the title cap and the per-app command cap each
 * landed on one side only), and both times the manifest accepted an app whose rows the
 * launcher then silently dropped. A comment cannot fail a build; this can.
 *
 * The two sides report differently on purpose — the manifest returns errors an app
 * author sees at install, the frontend skips the row and warns on the console — so each
 * harness asserts in its own idiom. What the fixture pins is the VERDICT.
 */
const FIXTURE = path.resolve(
  __dirname,
  '../../../../test/fixtures/contributed_commands_conformance.json',
)

type Case = {
  name: string
  accept: boolean
  commands: number
  contributes?: { commands: unknown[] }
  generate?: {
    commands?: number
    titleLength?: number
    promptLength?: number
    hosts?: number
    keywords?: number
    keywordLength?: number
  }
  asymmetric?: { why: string[]; manifest: string; frontend: string }
}

const cases: Case[] = JSON.parse(readFileSync(FIXTURE, 'utf8')).cases

/** Expand the `generate` shorthand, kept identical to the Python harness's `_contributes`. */
function contributesFor(c: Case): { commands: unknown[] } {
  const gen = c.generate
  if (!gen) return c.contributes as { commands: unknown[] }
  if (gen.commands !== undefined) {
    return {
      commands: Array.from({ length: gen.commands }, (_, n) => ({
        id: `cmd-${n}`,
        title: `Command ${n}`,
        prompt: 'Do the thing.',
      })),
    }
  }
  const cmd: Record<string, unknown> = { id: 'do-it', title: 'Do it', prompt: 'Do the thing.' }
  if (gen.keywords !== undefined) {
    cmd.keywords = Array.from({ length: gen.keywords }, (_, n) => `kw${n}`)
  }
  if (gen.keywordLength !== undefined) cmd.keywords = ['k'.repeat(gen.keywordLength)]
  if (gen.titleLength !== undefined) cmd.title = 'x'.repeat(gen.titleLength)
  if (gen.promptLength !== undefined) cmd.prompt = 'x'.repeat(gen.promptLength)
  if (gen.hosts !== undefined) {
    cmd.prompt = 'Do it to {argument}'
    cmd.argument = {
      kind: 'url',
      hosts: Array.from({ length: gen.hosts }, (_, n) => `h${n}.test`),
    }
  }
  return { commands: [cmd] }
}

describe('contributed commands — conformance with the shared fixture', () => {
  beforeEach(() => {
    // A refused contribution warns; the fixture is full of refusals on purpose.
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  it('has cases to run', () => {
    expect(cases.length).toBeGreaterThan(15)
  })

  it.each(cases.map(c => [c.name, c] as const))('%s', (_name, c) => {
    const rows = contributedCommands([
      {
        name: 'conformance',
        displayName: 'Conformance',
        enabled: true,
        origin: 'registry',
        manifest: { contributes: contributesFor(c) },
      },
    ])
    // `commands` is how many SURVIVE, so a case can pin partial acceptance: one bad
    // entry dropped while its siblings load.
    expect(rows).toHaveLength(c.commands)
    if (c.asymmetric) {
      // A case the fixture marks as a deliberate per-side difference. The verdicts may
      // differ, but only in the ONE way the fixture spells out, so a future change cannot
      // quietly widen the gap -- and an unrecognised behaviour string fails rather than
      // passing by default.
      if (c.asymmetric.frontend === 'accept-with-autosend-clamped') {
        expect(rows[0].autoSend).toBe(false)
      } else if (c.asymmetric.frontend === 'accept-with-keywords-trimmed') {
        // Covers both keyword asymmetries: the count case and the per-keyword length
        // case. Asserting BOTH bounds regardless of which case this is keeps one
        // behaviour value honest for both, and fails if a future change trims only one.
        expect(rows[0].keywords.length).toBeLessThanOrEqual(30)
        for (const kw of rows[0].keywords) expect(kw.length).toBeLessThanOrEqual(60)
      } else {
        throw new Error(`unknown asymmetric.frontend: ${c.asymmetric.frontend}`)
      }
      return
    }
    if (c.accept) expect(rows.length).toBeGreaterThan(0)
  })
})
