/**
 * CopyCommandButton — unit tests for copy success and failure states.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { ConfigSetCommandRow, CopyCommandButton } from './CopyCommandButton'
import type { ConfigSetHintsV1 } from '../../api/client'

// Mock the clipboard utility. `copyCode` resolves a BOOLEAN — true only once
// the text actually reached the clipboard — and never rejects, so a failed copy
// is a resolved `false` here, not a rejection. A bare `vi.fn()` resolving
// `undefined` would read as a failure.
vi.mock('../../utils/clipboard', () => ({
  copyCode: vi.fn(),
}))

import { copyCode } from '../../utils/clipboard'
const mockedCopyCode = vi.mocked(copyCode)

afterEach(() => {
  vi.clearAllMocks()
})

describe('CopyCommandButton', () => {
  it('renders with copy icon initially', () => {
    render(<CopyCommandButton text="test command" />)
    const button = screen.getByRole('button')
    expect(button).not.toBeNull()
    expect(button.getAttribute('aria-label')).toBe('Copy command')
  })

  it('shows check icon after successful copy', async () => {
    mockedCopyCode.mockResolvedValue(true)
    render(<CopyCommandButton text="kirocrew config set x true" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
    })

    await waitFor(() => {
      expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copied!')
    })
  })

  it('shows X icon after failed copy', async () => {
    mockedCopyCode.mockResolvedValue(false)
    render(<CopyCommandButton text="test command" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
    })

    await waitFor(() => {
      const button = screen.getByRole('button')
      expect(button.getAttribute('aria-label')).toBe('Copy failed')
      expect(button.getAttribute('title')).toBe('Copy failed')
    })
  })

  it('resets to idle state after timeout on failure', async () => {
    vi.useFakeTimers()
    mockedCopyCode.mockResolvedValue(false)
    render(<CopyCommandButton text="test" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
      // Let the resolved promise settle
      await Promise.resolve()
    })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copy failed')

    // Advance past the 1500ms reset timeout
    act(() => { vi.advanceTimersByTime(1600) })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copy command')
    vi.useRealTimers()
  })

  it('resets to idle state after timeout on success', async () => {
    vi.useFakeTimers()
    mockedCopyCode.mockResolvedValue(true)
    render(<CopyCommandButton text="test" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
      // Let the resolved promise settle
      await Promise.resolve()
    })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copied!')

    act(() => { vi.advanceTimersByTime(1600) })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copy command')
    vi.useRealTimers()
  })

  it('has aria-live="polite" for assistive technology', () => {
    render(<CopyCommandButton text="test" />)
    const button = screen.getByRole('button')
    expect(button.getAttribute('aria-live')).toBe('polite')
  })
})

const WIN_LAUNCHER = 'C:\\Kiro Crew\\resources\\bin\\kirocrew.cmd'
const HINTS: ConfigSetHintsV1 = {
  version: 1,
  encoding: 'utf-8-base64',
  shells: [
    { id: 'powershell', prefix: `& '${WIN_LAUNCHER}'` },
    { id: 'cmd', prefix: `"${WIN_LAUNCHER}"` },
  ],
}

/** The UTF-8 text a displayed `--value-base64=<token>` command carries. */
function decodedToken(command: string): string {
  const token = command.split('--value-base64=')[1]
  const binary = atob(token)
  return new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)))
}

const displayedCommands = (container: HTMLElement): string[] =>
  [...container.querySelectorAll('code')].map((c) => c.textContent ?? '')

describe('ConfigSetCommandRow with launcher hints', () => {
  it('builds each shell command from its server prefix and ONE attached token', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />,
    )

    // A blank list editor sets the empty list: `[]`, whose standard token is `W10=`.
    expect(displayedCommands(container)).toEqual([
      `& '${WIN_LAUNCHER}' config set skills.extra_paths --value-base64=W10=`,
      `"${WIN_LAUNCHER}" config set skills.extra_paths --value-base64=W10=`,
    ])
  })

  it('labels each shell row', () => {
    render(<ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />)

    expect(screen.getByText('Windows PowerShell')).not.toBeNull()
    expect(screen.getByText('Windows Command Prompt')).not.toBeNull()
  })

  it('encodes Unicode as exact, padded standard Base64', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />,
    )

    fireEvent.change(screen.getByLabelText('Values for the command, one per line'), {
      target: { value: "C:/my skills/a&b's/é" },
    })

    const [powershell, cmd] = displayedCommands(container)
    expect(powershell).toBe(
      `& '${WIN_LAUNCHER}' config set skills.extra_paths --value-base64=WyJDOi9teSBza2lsbHMvYSZiJ3Mvw6kiXQ==`,
    )
    expect(cmd.endsWith('--value-base64=WyJDOi9teSBza2lsbHMvYSZiJ3Mvw6kiXQ==')).toBe(true)
    expect(decodedToken(powershell)).toBe('["C:/my skills/a&b\'s/é"]')
  })

  it('sends empty text as an attached empty token, never a separate argument', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="dashboard.url" schemaType="string" initialValue="" hints={HINTS} />,
    )

    for (const command of displayedCommands(container)) {
      expect(command.endsWith(' --value-base64=')).toBe(true)
      expect(command).not.toMatch(/--value-base64\s/)
    }
  })

  it('turns a list into lines by the shared rule: CRLF, trim, drop blanks, keep order and duplicates', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />,
    )

    fireEvent.change(screen.getByLabelText('Values for the command, one per line'), {
      target: { value: '  /b  \r\n\r\n   \n/a\n/b\n' },
    })

    const [powershell] = displayedCommands(container)
    expect(decodedToken(powershell)).toBe('["/b","/a","/b"]')
    // ...and the readout shows what the token carries.
    expect(container.querySelector('pre')?.textContent).toBe('["/b","/a","/b"]')
  })

  it('sends a non-list value verbatim', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="dashboard.url" schemaType="string" hints={HINTS} />,
    )

    fireEvent.change(screen.getByLabelText('Value for the command'), { target: { value: ' http://x/?a=1&b=2 ' } })

    expect(decodedToken(displayedCommands(container)[0])).toBe(' http://x/?a=1&b=2 ')
  })

  it.each([
    ['array', 'Values for the command, one per line', 'TEXTAREA', null, ''],
    ['integer', 'Value for the command', 'INPUT', 'number', '7'],
    ['number', 'Value for the command', 'INPUT', 'number', '7'],
    ['boolean', 'Value for the command', 'INPUT', 'text', 'true'],
    ['string', 'Value for the command', 'INPUT', 'text', 'value'],
    ['unknown', 'Value for the command', 'INPUT', 'text', 'value'],
  ])('picks the control from the schema type %s', (schemaType, label, tag, inputType, sample) => {
    render(<ConfigSetCommandRow configKey="some.key" schemaType={schemaType} hints={HINTS} />)

    const control = screen.getByLabelText(label)
    expect(control.tagName).toBe(tag)
    if (inputType) expect(control.getAttribute('type')).toBe(inputType)
    expect((control as HTMLInputElement).value).toBe(sample)
  })

  it('starts from the caller sample when it has a meaningful one', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="session.archive_retention_days" schemaType="integer" initialValue="7" hints={HINTS} />,
    )

    expect((screen.getByLabelText('Value for the command') as HTMLInputElement).value).toBe('7')
    expect(decodedToken(displayedCommands(container)[0])).toBe('7')
  })

  it('shows a list value as stored in a non-live <pre>, not a status <output>', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />,
    )
    fireEvent.change(screen.getByLabelText('Values for the command, one per line'), {
      target: { value: ' /x \n/y' },
    })

    const readout = container.querySelector('pre')
    expect(readout).not.toBeNull()
    expect(readout?.getAttribute('aria-live')).toBe('off')
    expect(readout?.textContent).toBe('["/x","/y"]')
    expect(container.querySelector('output')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it.each(['string', 'integer', 'number', 'boolean', 'unknown', undefined])(
    'shows no readout for the single-value type %s, which would repeat the field',
    (schemaType) => {
      const { container } = render(
        <ConfigSetCommandRow configKey="some.key" schemaType={schemaType} hints={HINTS} />,
      )

      expect(container.querySelector('pre')).toBeNull()
      expect(container.querySelector('[role="group"]')).toBeNull()
      expect(screen.queryByText('This command sets')).toBeNull()
      // The commands still render from the field's value.
      expect(displayedCommands(container)).toHaveLength(2)
    },
  )

  it('names the editor, its explanation and the readout', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />,
    )

    const editor = screen.getByLabelText('Values for the command, one per line')
    const description = document.getElementById(editor.getAttribute('aria-describedby') ?? '')
    expect(description?.textContent).toMatch(/one entry per line/i)
    const group = container.querySelector('[role="group"]')
    expect(group?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(screen.getByText('This command sets')).not.toBeNull()
  })

  it('copies exactly the command it displays', async () => {
    mockedCopyCode.mockResolvedValue(true)
    const { container } = render(
      <ConfigSetCommandRow configKey="skills.extra_paths" schemaType="array" hints={HINTS} />,
    )
    fireEvent.change(screen.getByLabelText('Values for the command, one per line'), { target: { value: '/x\n/y' } })

    const shown = displayedCommands(container)
    const buttons = screen.getAllByRole('button', { name: 'Copy command' })
    expect(buttons).toHaveLength(2)
    await act(async () => {
      fireEvent.click(buttons[1])
    })

    expect(mockedCopyCode).toHaveBeenCalledWith(shown[1])
  })
})

describe('ConfigSetCommandRow without launcher hints', () => {
  it('shows the positional command and a neutral shell note, and never the encoded flag', () => {
    const { container } = render(
      <ConfigSetCommandRow configKey="instances.enabled" schemaType="boolean" />,
    )

    expect(displayedCommands(container)).toEqual(['kirocrew config set instances.enabled true'])
    expect(screen.getByText('This command may need quoting changes in some shells.')).not.toBeNull()
    // No hint is also what a non-owner or a tree without the cached status sees on a
    // current gateway, so the note never tells anyone to update.
    expect(container.textContent).not.toMatch(/update|upgrade/i)
    expect(container.textContent).not.toContain('--value-base64')
    expect(container.querySelector('textarea, input, pre')).toBeNull()
  })

  it('keeps an explicit placeholder as the positional value', () => {
    const { container } = render(
      <ConfigSetCommandRow
        configKey="session.archive_retention_days"
        schemaType="integer"
        valuePlaceholder="7"
      />,
    )

    expect(displayedCommands(container)).toEqual(['kirocrew config set session.archive_retention_days 7'])
  })
})
