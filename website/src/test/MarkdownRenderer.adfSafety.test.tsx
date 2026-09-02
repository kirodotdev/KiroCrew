// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import MarkdownRenderer from '../components/MarkdownRenderer'

// The backend's Jira ADF -> markdown converter escapes literal text so it cannot
// become markup, and emits external media as a LINK so opening an issue cannot
// auto-fetch a provider-controlled address. Both claims are about what THIS
// renderer does with the converter's output -- which plugins are in the stack
// decides which characters have to be escaped -- so they are asserted here,
// against the real plugin stack, rather than only in a Python comment.
//
// The fixture is shared: test/test_source_providers.py pins that the converter
// produces each `markdown` from each `adf`, so a case cannot drift from the
// converter, and this file pins what the renderer then does with it. A failure
// here means the escape set in source_providers.py needs re-deriving, not that
// the fixture needs editing.

type SafetyCase = {
  name: string
  markdown: string
  expectText?: string
  forbidSelectors?: string[]
  requireSelector?: string
  forbidText?: string
}

const fixturePath = resolve(__dirname, '../../../test/fixtures/adf_markdown_safety.json')
const cases: SafetyCase[] = JSON.parse(readFileSync(fixturePath, 'utf8')).cases

describe('ADF converter output through the real renderer', () => {
  it('has cases to check', () => {
    expect(cases.length).toBeGreaterThanOrEqual(6)
  })

  for (const testCase of cases) {
    it(testCase.name, () => {
      const { container } = render(<MarkdownRenderer content={testCase.markdown} />)

      for (const selector of testCase.forbidSelectors ?? []) {
        expect(container.querySelector(selector), `${selector} rendered`).toBeNull()
      }
      if (testCase.requireSelector) {
        expect(container.querySelector(testCase.requireSelector)).not.toBeNull()
      }
      if (testCase.expectText) {
        // The escaping backslashes must not survive into what the reader sees.
        expect(container.textContent).toContain(testCase.expectText)
      }
      if (testCase.forbidText) {
        expect(container.textContent).not.toContain(testCase.forbidText)
      }
    })
  }
})
