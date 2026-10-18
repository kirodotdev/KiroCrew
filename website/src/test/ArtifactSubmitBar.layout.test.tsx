import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { SubmitBar } from '../components/ArtifactSubmitBar'

// The bar was written for the ~480px chat side panel, where label, "Add
// instruction" and Submit share one line. In the standalone page's 340px
// comments sidebar that line cut the label off before it said where the
// comments go, so that host stacks the label on its own line.
const label = () => screen.getByText(/to send to this chat/)
const submit = () => screen.getByRole('button', { name: /submit/i })

describe('SubmitBar layout', () => {
  it('stacked: the label sits on its own line and is never truncated', () => {
    render(<SubmitBar count={2} submitting={false} onSubmit={vi.fn()} stacked />)
    expect(label().className).not.toMatch(/\btruncate\b/)
    expect(label().className).toMatch(/\bblock\b/)
    // Not inside the row that holds the controls, so the controls cannot squeeze it.
    expect(label().parentElement?.contains(submit())).toBe(true)
    expect(label().nextElementSibling?.contains(submit())).toBe(true)
  })

  it('default (side panel): one line, label truncates, as before', () => {
    render(<SubmitBar count={2} submitting={false} onSubmit={vi.fn()} />)
    expect(label().className).toMatch(/\btruncate\b/)
    const row = label().parentElement?.parentElement
    expect(row?.contains(submit())).toBe(true)
  })

  it('the 340px sidebar host asks for the stacked layout', async () => {
    const body = await readFile(join(__dirname, '..', 'pages/ArtifactDetailPageBody.tsx'), 'utf8')
    const bar = body.slice(body.indexOf('<SubmitBar'), body.indexOf('/>', body.indexOf('<SubmitBar')))
    expect(bar).toMatch(/\n\s*stacked\s*\n/)
  })
})
