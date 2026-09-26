/**
 * CustomAcpSetup — the inline setup form for the `custom` ACP backend.
 *
 * Two kinds of test live here:
 *   - The pure argv helpers, pinned directly because they carry the rules that
 *     make a saved command line safe: one argument per line, no shell parsing,
 *     an empty textarea is `[]` not `['']`, and an argv with an embedded newline
 *     cannot round-trip so the editor refuses it.
 *   - The component, driven through the DOM to prove Save writes the EXACT argv,
 *     that it writes `agent.custom_acp` and never `agent.acp_backend` (save is
 *     not activate), that a validation failure keeps the draft on screen, and
 *     that a rejected save keeps it too.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: { patchConfig: patchConfigMock },
}))

import {
  CustomAcpSetup,
  argsFromText,
  textFromArgs,
  hasUneditableArg,
  validateDraft,
} from '../pages/developer/CustomAcpSetup'

function wrap(saved?: { command: string; args: string[] }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <CustomAcpSetup saved={saved} />
    </QueryClientProvider>,
  )
  return { qc }
}

const command = () => screen.getByLabelText('Executable') as HTMLInputElement
const args = () => screen.getByLabelText('Arguments') as HTMLTextAreaElement
const saveBtn = () => screen.getByRole('button', { name: 'Save configuration' })

beforeEach(() => {
  patchConfigMock.mockClear()
  patchConfigMock.mockResolvedValue({})
})

describe('argsFromText', () => {
  it('treats an empty textarea as no arguments, never one empty argument', () => {
    // `['']` is a real, different (and wrong) command: a harness invoked with one
    // empty argument is not the same as one invoked with none.
    expect(argsFromText('')).toEqual([])
  })

  it('splits one argument per line with no shell parsing', () => {
    // A line IS an argument. No word-splitting, no quote handling, no globbing.
    expect(argsFromText('--serve\n--port\n8080')).toEqual(['--serve', '--port', '8080'])
  })

  it('preserves whitespace inside and around an argument', () => {
    // A path with spaces needs no quoting; leading/trailing spaces are the arg's.
    expect(argsFromText('  --flag with spaces  ')).toEqual(['  --flag with spaces  '])
  })

  it('drops only the final trailing newline, not an intentional empty line', () => {
    // The trailing newline is the editor's, not an argument. A blank line in the
    // MIDDLE was typed on purpose and is kept.
    expect(argsFromText('a\n')).toEqual(['a'])
    expect(argsFromText('a\n\nb')).toEqual(['a', '', 'b'])
  })
})

describe('textFromArgs / hasUneditableArg', () => {
  it('round-trips every argv the editor can produce', () => {
    for (const argv of [[], ['a'], ['a', 'b'], ['a', '', 'b'], ['  spaced  ']]) {
      expect(argsFromText(textFromArgs(argv))).toEqual(argv)
    }
  })

  it('round-trips an argv whose LAST argument is empty (regression)', () => {
    // The bug: `textFromArgs` joined with '\n' and `argsFromText` popped the
    // final newline, so a trailing empty argument was lost on the way through the
    // textarea. `['a','']` and `['']` are representable argv and must survive.
    for (const argv of [[''], ['a', ''], ['a', '', ''], ['', 'b', '']]) {
      expect(argsFromText(textFromArgs(argv))).toEqual(argv)
    }
    // The encoding difference is only in the trailing-empty case; a normal argv
    // still displays plainly, one per line with no trailing blank.
    expect(textFromArgs(['--a', '--b'])).toBe('--a\n--b')
    expect(textFromArgs(['a', ''])).toBe('a\n\n')
    expect(textFromArgs([''])).toBe('\n')
  })

  it('keeps a single-argument textarea reading as one argument', () => {
    // The other side of the inverse: `'a\n'` is the editor's own trailing newline
    // after one argument, not `['a','']`. Literal whitespace semantics unchanged.
    expect(argsFromText('a\n')).toEqual(['a'])
    expect(argsFromText('  --flag with spaces  ')).toEqual(['  --flag with spaces  '])
  })

  it('flags an argv with an embedded newline as uneditable', () => {
    // `['a\nb']` renders as two lines and would read back as `['a', 'b']`, so the
    // editor refuses to touch it rather than silently rewrite it.
    expect(hasUneditableArg(['a\nb'])).toBe(true)
    expect(hasUneditableArg(['a', 'b'])).toBe(false)
  })

  it('flags a carriage return as uneditable too (textarea normalizes it)', () => {
    // A textarea rewrites `\r` and `\r\n` to `\n`, so an argument carrying a CR
    // cannot survive one-per-line editing any more than one carrying a newline.
    expect(hasUneditableArg(['a\rb'])).toBe(true)
    expect(hasUneditableArg(['a\r\nb'])).toBe(true)
    expect(hasUneditableArg(['plain'])).toBe(false)
  })
})

describe('validateDraft', () => {
  it('requires a non-blank executable', () => {
    expect(validateDraft('', '')).toHaveProperty('error')
    expect(validateDraft('   ', '')).toHaveProperty('error')
  })

  it('trims the command and returns the parsed argv', () => {
    expect(validateDraft('  /bin/agent  ', '--x\n--y')).toEqual({
      config: { command: '/bin/agent', args: ['--x', '--y'] },
    })
  })

  it('rejects an over-long command', () => {
    expect(validateDraft('x'.repeat(4097), '')).toHaveProperty('error')
    expect(validateDraft('x'.repeat(4096), '')).toHaveProperty('config')
  })

  it('rejects too many arguments', () => {
    const many = Array.from({ length: 129 }, (_, i) => `a${i}`).join('\n')
    expect(validateDraft('cmd', many)).toHaveProperty('error')
  })

  it('rejects a single over-long argument', () => {
    expect(validateDraft('cmd', 'x'.repeat(8193))).toHaveProperty('error')
    expect(validateDraft('cmd', 'x'.repeat(8192))).toHaveProperty('config')
  })
})

describe('CustomAcpSetup form', () => {
  it('leads with the security warnings, not behind a disclosure', async () => {
    // Warning-led per the user's direction: the risk is stated first and is not
    // collapsible. No attestation checkbox, no confirm dialog.
    wrap()
    expect(
      screen.getByText(/runs a harness you supply, outside/),
    ).toBeInTheDocument()
    // No arbitrary-code fact is hidden behind a <details>.
    expect(
      screen.getByText(/without Kiro Crew's approval prompts/).closest('details'),
    ).toBeNull()
    // And there is no attestation gate in front of the inputs.
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('warns plainly that the harness runs without Crew approval or hook checks', async () => {
    wrap()
    expect(
      screen.getByText(/read, write, or delete files without Kiro Crew's approval/),
    ).toBeInTheDocument()
  })

  it('states the sandbox is a floor, not a disposable project or a network allowlist', async () => {
    wrap()
    expect(
      screen.getByText(/coarse floor, not a disposable or read-only project and not a network allowlist/),
    ).toBeInTheDocument()
  })

  it('claims no native isolation or model control it does not have', async () => {
    // No false privileges: the feature caveats say model is set in the harness,
    // there is no subagent continuation, and native Windows will not start it.
    wrap()
    expect(screen.getByText(/model is chosen in the harness/i)).toBeInTheDocument()
    expect(screen.getByText(/no shared process or subagent continuation/i)).toBeInTheDocument()
    expect(screen.getByText(/On native Windows, where there is no isolation, it will not start/)).toBeInTheDocument()
  })

  it('saves the EXACT argv to agent.custom_acp and never touches agent.acp_backend', async () => {
    // Save is not Use. Whitespace and blank lines are preserved verbatim; nothing
    // here writes the active backend.
    wrap()
    fireEvent.change(command(), { target: { value: '/usr/local/bin/my-agent' } })
    fireEvent.change(args(), { target: { value: '--serve\n--port\n8080' } })
    fireEvent.click(saveBtn())
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.custom_acp', {
        command: '/usr/local/bin/my-agent',
        args: ['--serve', '--port', '8080'],
      }),
    )
    // Exactly one write, and it is NOT the backend switch.
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
    expect(patchConfigMock).not.toHaveBeenCalledWith('agent.acp_backend', expect.anything())
  })

  it('saves an empty argv as [] when the textarea is blank', async () => {
    wrap()
    fireEvent.change(command(), { target: { value: 'agent' } })
    fireEvent.click(saveBtn())
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.custom_acp', {
        command: 'agent',
        args: [],
      }),
    )
  })

  it('seeds the form from the saved config', async () => {
    wrap({ command: '/bin/x', args: ['--a', '--b'] })
    expect(command().value).toBe('/bin/x')
    expect(args().value).toBe('--a\n--b')
  })

  it('keeps the draft and does not save when the executable is blank', async () => {
    // A validation failure highlights the problem and retains what the user typed;
    // the Save button is also disabled with a blank command, so this asserts both
    // the disabled guard and that no write escaped.
    wrap()
    fireEvent.change(args(), { target: { value: '--only-args' } })
    expect(saveBtn()).toBeDisabled()
    expect(args().value).toBe('--only-args')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('keeps the draft on a rejected save and shows the error through ErrorNotice', async () => {
    // No optimistic save: a rejection needs no revert, and the draft the user typed
    // stays exactly as it was so they can retry.
    patchConfigMock.mockRejectedValue(new Error('nope'))
    wrap()
    fireEvent.change(command(), { target: { value: '/bin/agent' } })
    fireEvent.change(args(), { target: { value: '--x' } })
    fireEvent.click(saveBtn())
    await waitFor(() =>
      expect(screen.getByText('Could not save the custom ACP configuration.')).toBeInTheDocument(),
    )
    // The draft is retained.
    expect(command().value).toBe('/bin/agent')
    expect(args().value).toBe('--x')
    // The error renders through ErrorNotice (role="alert"), WITHOUT an agent
    // hand-off — navigating away would discard the unsaved draft.
    const notice = screen
      .getByText('Could not save the custom ACP configuration.')
      .closest('[role="alert"]')
    expect(notice).not.toBeNull()
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).toBeNull()
  })

  it('refuses to edit a saved argv that carries an embedded newline', async () => {
    // A one-per-line editor cannot represent `['a\nb']` without rewriting it, so
    // the form is read-only with an explanation rather than corrupting the command.
    wrap({ command: '/bin/x', args: ['a\nb'] })
    expect(screen.getByText(/contain a line break inside one argument/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Executable')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save configuration' })).toBeNull()
  })

  it('preserves a trailing empty argument through save (round-trip regression)', async () => {
    // The editor encodes a trailing empty argument as a trailing newline, so the
    // exact argv reaches the save rather than dropping the empty tail.
    wrap()
    fireEvent.change(command(), { target: { value: '/bin/agent' } })
    fireEvent.change(args(), { target: { value: '--flag\n\n' } })
    fireEvent.click(saveBtn())
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.custom_acp', {
        command: '/bin/agent',
        args: ['--flag', ''],
      }),
    )
  })
})

/**
 * The draft state the form reports up, and the read-only paths.
 *
 * `onDraftState` is how the parent's Use gate learns a command line has been
 * edited but not saved, or that a save is in flight, so it can refuse to activate
 * anything other than the saved value. `disabled` is the governed (policy-denied)
 * path. These render the form directly with the extra props rather than through
 * the plain `wrap` helper.
 */
function renderWithProps(props: {
  saved?: { command: string; args: string[] }
  disabled?: boolean
  onDraftState?: (s: { dirty: boolean; saving: boolean }) => void
}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <CustomAcpSetup {...props} />
    </QueryClientProvider>,
  )
  return { qc, view }
}

describe('CustomAcpSetup draft state and read-only paths', () => {
  it('reports clean on mount and dirty once the command is edited', async () => {
    const onDraftState = vi.fn()
    renderWithProps({ saved: { command: '/bin/x', args: [] }, onDraftState })
    // Mount reports the freshly seeded, clean state.
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: false, saving: false }))
    fireEvent.change(command(), { target: { value: '/bin/y' } })
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: true, saving: false }))
    // Typing the saved value back makes it clean again — dirty is a comparison,
    // not a "was ever touched" latch.
    fireEvent.change(command(), { target: { value: '/bin/x' } })
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: false, saving: false }))
  })

  it('reports dirty when only the arguments change', async () => {
    const onDraftState = vi.fn()
    renderWithProps({ saved: { command: '/bin/x', args: ['--a'] }, onDraftState })
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: false, saving: false }))
    fireEvent.change(args(), { target: { value: '--a\n--b' } })
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: true, saving: false }))
  })

  it('reports saving while a save is in flight, then clean once it resolves', async () => {
    // The gate must stay disabled across the whole in-flight window, not just at
    // the edit — a pending PATCH has not changed what is on disk yet.
    let resolveSave: (v: unknown) => void = () => {}
    patchConfigMock.mockReturnValue(new Promise(r => (resolveSave = r)))
    const onDraftState = vi.fn()
    renderWithProps({ onDraftState })
    fireEvent.change(command(), { target: { value: '/bin/agent' } })
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: true, saving: false }))
    fireEvent.click(saveBtn())
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: true, saving: true }))
    resolveSave({})
    // The save resolved but the config query has not refetched in this isolated
    // render, so the seed is unchanged and the just-saved draft still differs from
    // it — the gate stays closed until a fresh `saved` prop arrives, which is the
    // parent's config-refresh path.
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: true, saving: false }))
  })

  it('returns to clean when the saved prop catches up to the draft (config refresh)', async () => {
    // Simulates the parent re-seeding the form after its config query refetches:
    // once `saved` matches what was typed, the draft is no longer a mismatch.
    const onDraftState = vi.fn()
    const { view } = renderWithProps({
      saved: { command: '/bin/x', args: [] },
      onDraftState,
    })
    fireEvent.change(command(), { target: { value: '/bin/y' } })
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: true, saving: false }))
    // A remount with the new saved value is exactly what the parent does — the
    // detail pane is keyed on the shown row, so a fresh config re-seeds the form.
    view.rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CustomAcpSetup key="reseed" saved={{ command: '/bin/y', args: [] }} onDraftState={onDraftState} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: false, saving: false }))
  })

  it('does not erase a draft typed while the config is being re-seeded', async () => {
    // A form reset on refetch would wipe what the user is typing. The draft seeds
    // once on mount and edits win; a later prop change does not clobber the field.
    const { view } = renderWithProps({ saved: { command: '/bin/x', args: [] } })
    fireEvent.change(command(), { target: { value: '/bin/in-progress' } })
    // The parent's config query resolves with a DIFFERENT value mid-edit.
    view.rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CustomAcpSetup saved={{ command: '/bin/refetched', args: ['--x'] }} />
      </QueryClientProvider>,
    )
    // The in-progress edit survives — the seed initialiser runs once, so a refetch
    // that lands while the user is typing does not reset the field under them.
    expect(command().value).toBe('/bin/in-progress')
  })

  it('renders read-only controls for a governed (policy-denied) selection', async () => {
    // A denied custom row still shows its form (it is the saved backend), but every
    // control is disabled so the reader cannot edit a command line the deployment
    // forbids — driven by the governed selection, no evaluator invented here.
    renderWithProps({ saved: { command: '/bin/x', args: ['--a'] }, disabled: true })
    expect(command()).toBeDisabled()
    expect(args()).toBeDisabled()
    expect(saveBtn()).toBeDisabled()
  })

  it('does not save from a governed form even with a valid draft', async () => {
    renderWithProps({ saved: { command: '/bin/x', args: [] }, disabled: true })
    // The Save button is disabled, so a click is inert and nothing is written.
    fireEvent.click(saveBtn())
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})

it('compares the parsed launch pair after a save, not editor-only whitespace', async () => {
  const onDraftState = vi.fn()
  const { qc, view } = renderWithProps({ saved: { command: 'agent', args: [] }, onDraftState })
  fireEvent.change(command(), { target: { value: ' next-agent ' } })
  fireEvent.change(args(), { target: { value: '--acp\n' } })
  fireEvent.click(saveBtn())
  await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.custom_acp', {
    command: 'next-agent', args: ['--acp'],
  }))
  view.rerender(
    <QueryClientProvider client={qc}>
      <CustomAcpSetup saved={{ command: 'next-agent', args: ['--acp'] }} onDraftState={onDraftState} />
    </QueryClientProvider>,
  )
  await waitFor(() => expect(onDraftState).toHaveBeenLastCalledWith({ dirty: false, saving: false }))
  expect(command().value).toBe(' next-agent ')
  expect(args().value).toBe('--acp\n')
})
