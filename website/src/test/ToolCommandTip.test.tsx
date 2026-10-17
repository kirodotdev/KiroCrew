import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { formatShellForTip } from '../utils/shellTipFormat'
import { ToolCommandTip, TIP_CHAR_CAP, TIP_LINE_CAP, PILL_TIP_OPEN_CLASS } from '../pages/chat/ToolCommandTip'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

const CUT_HINT = i18nT('pages.chat.toolCallLine.tip_cut_hint')

/** Each line as `<op>|<text>`, plus the kinds that carry colour. */
const lineText = (cmd: string) => formatShellForTip(cmd)?.map(l => `${l.op}|${l.pieces.map(p => p.text).join('')}`)
const kinds = (cmd: string, kind: string) => formatShellForTip(cmd)?.flatMap(l => l.pieces.filter(p => p.kind === kind).map(p => p.text))

describe('formatShellForTip', () => {
  it('puts each pipeline stage on its own line, led by its operator', () => {
    const cmd = '.venv/bin/python -m pytest -q -o addopts="" test/a.py 2>&1 | grep -E "^(FAILED|E )|passed" | head -20'
    expect(lineText(cmd)).toEqual([
      '|.venv/bin/python -m pytest -q -o addopts="" test/a.py 2>&1',
      '||grep -E "^(FAILED|E )|passed"',
      '||head -20',
    ])
    expect(kinds(cmd, 'program')).toEqual(['.venv/bin/python', 'grep', 'head'])
    expect(kinds(cmd, 'flag')).toEqual(['-m', '-q', '-o', '-E', '-20'])
    expect(kinds(cmd, 'string')).toEqual(['""', '"^(FAILED|E )|passed"'])
    expect(kinds(cmd, 'redirect')).toEqual(['2>&1'])
  })

  it('splits on && || ; but never inside quotes or substitutions', () => {
    expect(lineText("cd a && echo 'x && y' || n=$(ls | wc -l); echo `a|b`")).toEqual([
      '|cd a',
      '&&|echo \'x && y\'',
      '|||n=$(ls | wc -l)',
      ';|echo `a|b`',
    ])
  })

  it('treats a leading env assignment as not the program', () => {
    expect(kinds('FOO=1 BAR=2 node x.js', 'program')).toEqual(['node'])
  })

  it('drops the empty line a trailing ; would leave', () => {
    expect(lineText('ls;')).toEqual(['|ls'])
  })

  it('refuses a multi-line command so the caller shows it verbatim', () => {
    expect(formatShellForTip("cat > f <<'EOF'\nbody\nEOF")).toBeNull()
  })

  it('reads >| as one clobbering redirect, not a pipe into the target', () => {
    expect(lineText('echo x >| out.txt')).toEqual(['|echo x >| out.txt'])
    expect(kinds('echo x 2>| out.txt', 'redirect')).toEqual(['2>|'])
    expect(kinds('echo x >| out.txt', 'program')).toEqual(['echo'])
  })

  it('keeps every character of a single-line command', () => {
    const cmd = 'a  "b c"\\ d >> out.txt & e'
    const out = formatShellForTip(cmd)!
    expect(out.map(l => l.op + l.pieces.map(p => p.text).join('')).join('')).toBe(cmd)
  })
})

describe('ToolCommandTip', () => {
  it('carries the verbatim command for assistive tech and hides the reflow', () => {
    const cmd = 'ls -la | grep x'
    const { container } = render(<ToolCommandTip text={cmd} shell />)
    expect(container.querySelector('.sr-only')?.textContent).toBe(cmd)
    expect(container.querySelector('[aria-hidden="true"]')?.textContent).toContain('grep x')
  })

  it('resolves the cut hint from the catalog rather than echoing its key', () => {
    expect(CUT_HINT).toBe('Open the step to see the rest')
  })

  it('bounds a many-stage command and points at the opened step for the rest', () => {
    const cmd = Array.from({ length: TIP_LINE_CAP + 5 }, (_, i) => `echo ${i}`).join(' && ')
    const { container } = render(<ToolCommandTip text={cmd} shell />)
    const grid = container.querySelector('[aria-hidden="true"]')!
    expect(grid.textContent).toContain(`echo ${TIP_LINE_CAP - 1}`)
    expect(grid.textContent).not.toContain(`echo ${TIP_LINE_CAP}`)
    const cut = grid.querySelector('[data-testid="tool-command-tip-cut"]')!
    expect(cut.textContent).toBe(`… ${CUT_HINT}`)
    expect(cut).toHaveClass('text-muted')
    expect(grid.lastElementChild).toBe(cut)
    // The verbatim command for assistive tech is whole, so it carries no hint.
    expect(container.querySelector('.sr-only')?.textContent).toBe(cmd)
  })

  it('marks a command cut by the character cap the same way', () => {
    const cmd = `echo ${'x'.repeat(TIP_CHAR_CAP + 50)}`
    const { container } = render(<ToolCommandTip text={cmd} shell />)
    expect(container.querySelector('[data-testid="tool-command-tip-cut"]')?.textContent).toBe(`… ${CUT_HINT}`)
  })

  it('shows no cut marker when the whole command fits', () => {
    const { container } = render(<ToolCommandTip text="ls | wc -l" shell />)
    expect(container.querySelector('[data-testid="tool-command-tip-cut"]')).toBeNull()
  })

  it('paints a pipe apart from the sequencing operators in the gutter', () => {
    const { container } = render(<ToolCommandTip text="a | b && c || d; e |& f" shell />)
    const gutter = Array.from(container.querySelectorAll('[data-op-kind]'), el => [el.textContent, el.getAttribute('data-op-kind'), el.className])
    expect(gutter.map(([op, kind]) => `${op}:${kind}`)).toEqual(['|:pipe', '&&:sequence', '||:sequence', ';:sequence', '|&:pipe'])
    for (const [, kind, cls] of gutter) {
      expect(cls).toContain(kind === 'pipe' ? 'text-accent' : 'text-muted')
      expect(cls).not.toContain(kind === 'pipe' ? 'text-muted' : 'text-accent')
    }
  })

  it('renders a prose title as plain text within the viewport width cap', () => {
    render(<ToolCommandTip text="Read /a/b.ts" shell={false} />)
    expect(screen.getByText('Read /a/b.ts')).toHaveClass('max-w-[min(26rem,calc(100vw-2rem))]')
    expect(screen.queryByTestId('tool-command-tip')).toBeNull()
    expect(screen.queryByTestId('tool-command-tip-cut')).toBeNull()
  })

  it('marks a cut prose title with the same pointer to the opened step', () => {
    const title = 'Read '.repeat(TIP_CHAR_CAP)
    render(<ToolCommandTip text={title} shell={false} />)
    const cut = screen.getByTestId('tool-command-tip-cut')
    expect(cut.textContent).toBe(`… ${CUT_HINT}`)
    expect(cut).toHaveClass('text-muted')
  })
})

describe('ToolCallLine pill hover bubble', () => {
  beforeEach(() => { localStorage.clear() })

  function renderRow(command = 'echo hello | wc -l', purpose = 'Count') {
    localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: true }))
    const msg: ChatMessage = { role: 'tool', content: `🔧 Running: ${command}`, cls: '', meta: { tool_call_id: 'tc_h', purpose } }
    const store = createTestStore({
      chat: {
        messages: [msg],
        toolLog: [{ type: 'tool', text: command, purpose, tool_call_id: 'tc_h', output: '1', ts: 1 }],
        slotRunning: false,
      } as unknown as RootState['chat'],
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
  }

  it.each([
    ['search', 'rg error src', 'Find source errors'],
    ['read', 'cat README.md', 'Read the project overview'],
  ])('formats a purpose-labelled shell %s as a two-stage pipeline', (_kind, command, purpose) => {
    const pipeline = `${command} | head -20`
    renderRow(pipeline, purpose)
    expect(screen.getByTestId('tool-pill-label').textContent).toBe(purpose)
    fireEvent.focus(screen.getByRole('button', { name: /Show details/i }))
    const tip = screen.getByRole('tooltip')
    const grid = tip.querySelector('[data-testid="tool-command-tip"] > [aria-hidden="true"]')
    expect(grid).not.toBeNull()
    expect(grid).toHaveClass('grid')
    expect(Array.from(grid!.children, line => line.textContent)).toEqual([`$${command}`, '|head -20'])
    expect(tip.querySelector('.sr-only')?.textContent).toBe(pipeline)
  })

  it('replaces the native title with a styled bubble', () => {
    renderRow()
    const pill = screen.getByRole('button', { name: /Show details/i })
    expect(pill.getAttribute('title')).toBeNull()
    fireEvent.focus(pill)
    const tip = screen.getByRole('tooltip')
    expect(tip.querySelector('[data-testid="tool-command-tip"]')).toBeTruthy()
    expect(tip.querySelector('.sr-only')?.textContent).toBe('echo hello | wc -l')
  })

  it('marks the pill whose bubble is open with a fill and a neutral inset outline, only while open', () => {
    renderRow()
    const pill = screen.getByRole('button', { name: /Show details/i })
    const openClasses = PILL_TIP_OPEN_CLASS.split(' ')
    expect(openClasses).toEqual(expect.arrayContaining(['bg-bg-hover', 'outline-muted', '-outline-offset-1']))
    // Accent is the pill's focus ring; the open-bubble mark must not speak it.
    expect(PILL_TIP_OPEN_CLASS).not.toMatch(/accent/)
    for (const c of openClasses) expect(pill).not.toHaveClass(c)
    expect(pill.hasAttribute('data-tip-open')).toBe(false)
    fireEvent.focus(pill)
    expect(screen.getByRole('tooltip')).toBeTruthy()
    for (const c of openClasses) expect(pill).toHaveClass(c)
    expect(pill.getAttribute('data-tip-open')).toBe('true')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
    for (const c of openClasses) expect(pill).not.toHaveClass(c)
    expect(pill.hasAttribute('data-tip-open')).toBe(false)
  })

  it('shows no bubble once the row is open', () => {
    renderRow()
    const pill = screen.getByRole('button', { name: /Show details/i })
    fireEvent.focus(pill)
    fireEvent.click(pill)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(pill).not.toHaveClass('outline-muted')
    expect(pill.className).not.toContain('bg-bg-hover')
  })
})
