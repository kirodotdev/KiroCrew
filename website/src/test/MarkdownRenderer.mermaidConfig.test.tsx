import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * The COMPLETE configuration every diagram draw hands `mermaid.initialize`.
 *
 * `securityLevel: 'strict'` is what keeps a prompt-injected diagram from
 * running script, and nothing else pins it for this renderer: the CI grep only
 * rejects an added `'loose'`, so a dropped or retyped key would pass it. The
 * whole object is compared, per theme, so no key can go missing on one branch.
 */

const FENCE = '```mermaid\ngraph TD;A-->B\n```'

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => {
  cleanup()
  document.documentElement.removeAttribute('data-theme')
})

async function initializedWith() {
  render(<MarkdownRenderer content={FENCE} />)
  await vi.waitFor(() => expect(mermaid.initialize).toHaveBeenCalled())
  return vi.mocked(mermaid.initialize).mock.calls[0][0]
}

describe('MarkdownRenderer mermaid initialize config', () => {
  it('draws a light-theme diagram under the strict security level', async () => {
    document.documentElement.setAttribute('data-theme', 'light')
    expect(await initializedWith()).toEqual({
      startOnLoad: false,
      theme: 'default',
      themeVariables: {
        primaryColor: '#f59e32',
        primaryTextColor: '#1a1a1a',
        primaryBorderColor: '#ccc',
        lineColor: '#666',
        secondaryColor: '#fff3e0',
        tertiaryColor: '#f5f5f5',
      },
      securityLevel: 'strict',
      fontFamily: 'inherit',
      suppressErrorRendering: true,
    })
  })

  it('draws a dark-theme diagram under the strict security level', async () => {
    document.documentElement.setAttribute('data-theme', 'midnight-dark')
    expect(await initializedWith()).toEqual({
      startOnLoad: false,
      theme: 'dark',
      themeVariables: {
        primaryColor: '#f59e32',
        primaryTextColor: '#e8e6e3',
        primaryBorderColor: '#3a3a3a',
        lineColor: '#888',
        secondaryColor: '#2a2a2a',
        tertiaryColor: '#1a1a1a',
      },
      securityLevel: 'strict',
      fontFamily: 'inherit',
      suppressErrorRendering: true,
    })
  })

  it('is set in the file the security spec names', () => {
    // docs/system-specs/modules/security.md states where the strict level is
    // set; the file it names must be the one that sets it.
    const repo = join(__dirname, '..', '..', '..')
    const spec = readFileSync(join(repo, 'docs', 'system-specs', 'modules', 'security.md'), 'utf8')
    const named = /Mermaid `securityLevel` is set to `'strict'` in `([^`]+)`/.exec(spec)
    expect(named, 'security.md names the file that sets the Mermaid security level').not.toBeNull()
    const file = join(repo, 'website', 'src', named![1])
    expect(existsSync(file), file).toBe(true)
    expect(readFileSync(file, 'utf8')).toMatch(/securityLevel: 'strict'/)
  })

  it('re-initializes before every draw so a theme switch between diagrams applies', async () => {
    render(<MarkdownRenderer content={`${FENCE}\n\ntext\n\n\`\`\`mermaid\ngraph LR;C-->D\n\`\`\``} />)
    await vi.waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(2))
    expect(mermaid.initialize).toHaveBeenCalledTimes(2)
  })
})
