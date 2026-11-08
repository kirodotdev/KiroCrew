/**
 * Truncation tests for every LLM-derived string WorkflowRunTree caps with a
 * length slice.
 *
 * Each capped field is cut to a fixed number of UTF-16 units. An emoji is two
 * UTF-16 units (a surrogate pair), so a plain `.slice(0, n)` whose cap lands
 * between the pair keeps the lone HIGH surrogate, which renders as U+FFFD (the
 * replacement character) and, when sent back to the agent, is invalid Unicode.
 * These pin that each site cuts on a code-point boundary: a '🎉' that straddles
 * the cap is dropped whole, and a '🎉' that ends exactly at the cap is kept
 * whole.
 *
 * sanitizeLlmOutput runs before each slice; a run of plain ASCII 'a' and a bare
 * '🎉' pass through it unchanged (no credential pattern, no URL), so the
 * expected strings below are the sanitized input sliced.
 */
import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunTree from './WorkflowRunTree'
import type { WfEvent } from './runModel'
import { i18next, initI18n } from '../../i18n/all'
import { renderWithProviders } from '../../test/helpers'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

const EMOJI = '🎉' // U+1F389, two UTF-16 units: \uD83C\uDF89
const LONE_HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

/** `n-1` ASCII chars then the emoji, so a UTF-16 `.slice(0, n)` cuts between the
 *  emoji's surrogate pair — the straddling case. A code-point slice drops the
 *  whole emoji, leaving exactly the `n-1` ASCII chars. */
function straddle(cap: number): { input: string; expected: string } {
  return { input: 'a'.repeat(cap - 1) + EMOJI, expected: 'a'.repeat(cap - 1) }
}

/** The emoji ends exactly at the cap: `n-2` ASCII chars, the emoji at indices
 *  `n-2`/`n-1`, then a tail the cap removes. Both slices keep the emoji whole. */
function endsAtCap(cap: number): { input: string; expected: string } {
  return { input: 'a'.repeat(cap - 2) + EMOJI + 'tail', expected: 'a'.repeat(cap - 2) + EMOJI }
}

function ev(type: string, data: Record<string, unknown>, seq: number): WfEvent {
  return { run_id: 'wf_trunc', seq, ts: '2026-10-02T15:00:00.000Z', type, data }
}

const PHASE_CAP = 80
const LABEL_CAP = 100
const LAST_TOOL_CAP = 60
const LOG_CAP = 200
const ERROR_CAP = 200
const RESULT_CAP = 2000

const phaseHeaderTitle = () => screen.getByRole('button').querySelector('span.truncate.text-left')!
// The ErrorNotice root also holds an "Ask the agent" hand-off button; the
// message text lives in the block variant's own `whitespace-pre-wrap` column, so
// read that to assert on the sliced string alone.
const errorMessage = () => screen.getByTestId('workflow-run-tree-error').querySelector('.whitespace-pre-wrap')!

describe('WorkflowRunTree truncates LLM strings on a code-point boundary', () => {
  describe('phase title (cap 80)', () => {
    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const { input, expected } = straddle(PHASE_CAP)
      renderWithProviders(<WorkflowRunTree events={[ev('phase_started', { title: input }, 0)]} status="running" />)
      const el = phaseHeaderTitle()
      expect(el.textContent).toBe(expected)
      expect(el.textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      const { input, expected } = endsAtCap(PHASE_CAP)
      renderWithProviders(<WorkflowRunTree events={[ev('phase_started', { title: input }, 0)]} status="running" />)
      expect(phaseHeaderTitle().textContent).toBe(expected)
    })
  })

  describe('agent label (cap 100)', () => {
    const withAgent = (label: string) => [
      ev('phase_started', { title: 'P' }, 0),
      ev('agent_started', { agent_id: 'a1', label, phase: 'P' }, 1),
    ]

    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const { input, expected } = straddle(LABEL_CAP)
      const { container } = renderWithProviders(<WorkflowRunTree events={withAgent(input)} status="running" />)
      const el = container.querySelector('span.font-mono.truncate')!
      expect(el.textContent).toBe(expected)
      expect(el.textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      const { input, expected } = endsAtCap(LABEL_CAP)
      const { container } = renderWithProviders(<WorkflowRunTree events={withAgent(input)} status="running" />)
      expect(container.querySelector('span.font-mono.truncate')!.textContent).toBe(expected)
    })
  })

  describe('agent last tool (cap 60)', () => {
    const withTool = (lastTool: string) => [
      ev('phase_started', { title: 'P' }, 0),
      ev('agent_started', { agent_id: 'a1', label: 'agent one', phase: 'P' }, 1),
      ev('agent_progress', { agent_id: 'a1', last_tool: lastTool }, 2),
    ]

    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const { input, expected } = straddle(LAST_TOOL_CAP)
      const { container } = renderWithProviders(<WorkflowRunTree events={withTool(input)} status="running" />)
      const el = container.querySelector('span.text-muted.truncate')!
      // The last-tool span renders as "· <tool>"; strip the leading marker.
      expect(el.textContent).toBe(`· ${expected}`)
      expect(el.textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      const { input, expected } = endsAtCap(LAST_TOOL_CAP)
      const { container } = renderWithProviders(<WorkflowRunTree events={withTool(input)} status="running" />)
      expect(container.querySelector('span.text-muted.truncate')!.textContent).toBe(`· ${expected}`)
    })
  })

  describe('narrator log message (cap 200)', () => {
    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const { input, expected } = straddle(LOG_CAP)
      const { container } = renderWithProviders(<WorkflowRunTree events={[ev('log', { message: input }, 0)]} status="running" />)
      // The log strip renders each line as "· <message>".
      const line = container.querySelector('.font-mono .truncate') as HTMLElement
      expect(line.textContent).toBe(`· ${expected}`)
      expect(line.textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      const { input, expected } = endsAtCap(LOG_CAP)
      const { container } = renderWithProviders(<WorkflowRunTree events={[ev('log', { message: input }, 0)]} status="running" />)
      const line = container.querySelector('.font-mono .truncate') as HTMLElement
      expect(line.textContent).toBe(`· ${expected}`)
    })
  })

  describe('error message on a non-failed run (cap 200)', () => {
    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const { input, expected } = straddle(ERROR_CAP)
      renderWithProviders(<WorkflowRunTree events={[]} status="running" error={input} />)
      expect(errorMessage().textContent).toBe(expected)
      expect(errorMessage().textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      const { input, expected } = endsAtCap(ERROR_CAP)
      renderWithProviders(<WorkflowRunTree events={[]} status="running" error={input} />)
      expect(errorMessage().textContent).toBe(expected)
    })
  })

  describe('failed-run error, interpolated into the failure template (cap 200)', () => {
    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const { input, expected } = straddle(ERROR_CAP)
      renderWithProviders(<WorkflowRunTree events={[]} status="failed" error={input} />)
      expect(errorMessage().textContent).toBe(`Failed: ${expected}`)
      expect(errorMessage().textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      const { input, expected } = endsAtCap(ERROR_CAP)
      renderWithProviders(<WorkflowRunTree events={[]} status="failed" error={input} />)
      expect(errorMessage().textContent).toBe(`Failed: ${expected}`)
    })
  })

  describe('finished result JSON (cap 2000)', () => {
    // result is a string, so JSON.stringify wraps it in quotes: the rendered
    // text is `"<contents>`. The opening quote is index 0, so a straddling emoji
    // starts one unit before 2000 when the string holds 1998 'a' then the emoji.
    const resultPre = () => screen.getByText(/^"a+/, { selector: 'pre' })

    it('drops an emoji that straddles the cap instead of leaving a lone surrogate', async () => {
      await initI18n()
      const value = 'a'.repeat(RESULT_CAP - 2) + EMOJI // JSON: `"` + 1998 a's + emoji + `"`
      renderWithProviders(<WorkflowRunTree events={[]} status="finished" result={value} />)
      const el = resultPre()
      expect(el.textContent).toBe('"' + 'a'.repeat(RESULT_CAP - 2))
      expect(el.textContent).not.toMatch(LONE_HIGH_SURROGATE)
    })

    it('keeps an emoji that ends exactly at the cap', async () => {
      await initI18n()
      // JSON: `"` + 1997 a's + emoji (ends at index 2000) + `"` + tail removed.
      const value = 'a'.repeat(RESULT_CAP - 3) + EMOJI + 'tail'
      renderWithProviders(<WorkflowRunTree events={[]} status="finished" result={value} />)
      expect(resultPre().textContent).toBe('"' + 'a'.repeat(RESULT_CAP - 3) + EMOJI)
    })
  })
})
