import React from 'react'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import RunInTerminalBtn from '../components/RunInTerminalBtn'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import { parseOptions } from '../app-sdk/protocol'
import { appendFollowUpOption } from '../lib/followUpToggle'
import { SlotProvider } from '../providers/SlotContext'
import { TerminalHostContext, terminalCommand } from '../hooks/useTerminalCommand'
import { KILLING_ESCAPE_MS } from '../hooks/useStopEscapeHatch'
import { parseDirTokens } from '../utils/fileTokens'
import { formatToken } from '../utils/pasteTokens'
import { renderWithProviders as renderWithAppProviders } from './helpers'
import { changeLanguage, registerCatalogs } from '../i18n'
import ru from '../i18n/locales/ru.json'
import bn from '../i18n/locales/bn.json'

registerCatalogs({ ru: { translation: ru }, bn: { translation: bn } })

const terminal = vi.hoisted(() => ({
  enabled: true,
  addTab: vi.fn<(cwd?: string) => string | null>(),
  send: vi.fn(),
  bringBack: vi.fn(),
  unsubscribe: vi.fn(),
  ready: null as (() => void) | null,
  failure: null as (() => void) | null,
  callbacks: new Map<string, () => void>(),
}))
vi.mock('../hooks/useBottomTerminal', () => ({
  addTab: (cwd?: string) => terminal.addTab(cwd),
}))
vi.mock('../utils/terminalPopout', () => ({
  bringBack: () => terminal.bringBack(),
}))
vi.mock('../utils/terminalRegistry', () => ({
  isTerminalEnabled: () => terminal.enabled,
  useTerminalEnabled: () => terminal.enabled,
  onTerminalReady: (id: string, callback: () => void, onFailure?: () => void) => {
    terminal.ready = callback
    terminal.failure = onFailure ?? null
    terminal.callbacks.set(id, callback)
    return terminal.unsubscribe
  },
  sendToTerminalSession: (...args: unknown[]) => terminal.send(...args),
}))

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  terminal.enabled = true
  terminal.addTab.mockReset().mockReturnValue('terminal-1')
  terminal.send.mockReturnValue(true)
  terminal.ready = null
  terminal.failure = null
  terminal.callbacks.clear()
})
afterEach(async () => {
  vi.useRealTimers()
  await changeLanguage('en')
})

function renderWithProviders(ui: React.ReactElement) {
  const view = renderWithAppProviders(<TerminalHostContext.Provider value="docked">{ui}</TerminalHostContext.Provider>)
  return {
    ...view,
    rerender: (next: React.ReactElement) => view.rerender(
      <TerminalHostContext.Provider value="docked">{next}</TerminalHostContext.Provider>,
    ),
  }
}

function setup(overrides: Partial<React.ComponentProps<typeof ChatInput>> = {}) {
  const props = {
    value: '! pwd',
    onChange: vi.fn(),
    onSend: vi.fn(),
    onSteer: vi.fn(),
    terminalCommands: 'local' as const,
    project: '/work/selected project',
    ...overrides,
  }
  const view = renderWithProviders(<ChatInput {...props} />)
  return { ...view, props }
}

function confirmRun() {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^Run(?: anyway)?$/ }))
}

describe('direct terminal commands', () => {
  it('names the workspace folder in the hint when the project chip is absent or compact', () => {
    const observers = new Map<Element, ResizeObserverCallback>()
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { observers.set(target, this.callback) }
      unobserve(target: Element) { observers.delete(target) }
      disconnect() {}
    })
    const { props, rerender, unmount } = setup({ onProjectClick: vi.fn() })
    const hint = () => within(screen.getByRole('group', { name: 'Review and run in terminal' })).getByRole('status')
    try {
      expect(hint()).toHaveTextContent(/^Terminal panel$/)
      rerender(<ChatInput {...props} onProjectClick={undefined} />)
      // The folder name keeps a phone hint to one line; hover and review carry the path.
      expect(hint()).toHaveTextContent(/^Terminal panel · selected project$/)
      expect(within(hint()).getByTitle('/work/selected project')).toBeInTheDocument()
      rerender(<ChatInput {...props} />)
      const shelf = screen.getByTestId('composer-context-shelf')
      act(() => observers.get(shelf)?.([{
        target: shelf, contentRect: { width: 320, height: 32 },
      } as ResizeObserverEntry], {} as ResizeObserver))
      expect(hint()).toHaveTextContent(/^Terminal panel · selected project$/)
      fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
      expect(screen.getByRole('dialog')).toHaveTextContent('/work/selected project')
    } finally {
      unmount()
      vi.unstubAllGlobals()
    }
  })

  it('reviews a command on the first Enter even when it exceeds the model context limit', () => {
    const command = `printf '%s' '${'x'.repeat(4_400)}'`
    const { props } = setup({ value: `! ${command}`, contextWindowTokens: 1_000 })
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(screen.getByRole('dialog')).toHaveTextContent(command)
    expect(screen.getByTestId('prompt-length-notice')).not.toHaveAttribute('data-held', 'true')
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    expect(terminal.addTab).not.toHaveBeenCalled()
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', command, { preserveTrailingWhitespace: true })
  })

  it('lists the syntax with the other shortcuts only while a local terminal is enabled and docked', () => {
    const { props, rerender } = setup({ value: '' })
    const input = screen.getByLabelText('Message input')
    const advertised = /^Message .+… \(\/command · @file · \$skill · ! shell command\)$/
    expect(input).toHaveAttribute('placeholder', expect.stringMatching(advertised))

    for (const target of ['remote', 'pending', undefined] as const) {
      rerender(<ChatInput {...props} terminalCommands={target} />)
      expect(input).not.toHaveAttribute('placeholder', expect.stringMatching(advertised))
    }
    for (const host of ['unavailable', 'detached'] as const) {
      rerender(<TerminalHostContext.Provider value={host}><ChatInput {...props} /></TerminalHostContext.Provider>)
      expect(screen.getByLabelText('Message input')).not.toHaveAttribute('placeholder', expect.stringMatching(advertised))
    }

    terminal.enabled = false
    rerender(<ChatInput {...props} />)
    expect(screen.getByLabelText('Message input')).not.toHaveAttribute('placeholder', expect.stringMatching(advertised))
    expect(terminal.addTab).not.toHaveBeenCalled()
  })

  it.each([false, true])('runs a terminal draft during compaction (agent running: %s)', isRunning => {
    const onStop = vi.fn()
    const { props } = setup({ compacting: true, isRunning, stopDeclined: isRunning, canSteer: true, onStop })
    expect(screen.queryByTestId('compacting-indicator')).not.toBeInTheDocument()
    if (isRunning) expect(screen.getByTestId('stop-declined-hint')).toHaveTextContent('Click again to force stop')
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    expect(terminal.addTab).not.toHaveBeenCalled()
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.addTab).toHaveBeenCalledExactlyOnceWith('/work/selected project')
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
    if (isRunning) {
      fireEvent.click(screen.getByTestId('stop-button-armed'))
      expect(onStop).toHaveBeenCalledOnce()
    }
  })

  it('runs a fresh composer terminal while native code blocks offer Copy under the reuse setting', async () => {
    const { queryClient, rerender } = renderWithProviders(<></>)
    queryClient.setQueryData(['kirocrewConfig'], { dashboard: { terminal: { reuse_current: true } } })
    const props = {
      value: '! pwd', onChange: vi.fn(), onSend: vi.fn(), onSteer: vi.fn(),
      terminalCommands: 'local' as const, project: '/work/selected project',
    }
    rerender(<><ChatInput {...props} /><RunInTerminalBtn code="echo copied" /></>)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    const copyDialog = screen.getByRole('dialog')
    expect(within(copyDialog).getByRole('button', { name: 'Copy', exact: true })).toBeInTheDocument()
    expect(copyDialog).toHaveTextContent('paste into the terminal')
    await waitFor(() => expect(within(copyDialog).getByRole('button', { name: 'Cancel' })).toHaveFocus())
    expect(terminal.addTab).not.toHaveBeenCalled()
    fireEvent.click(within(copyDialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    const runDialog = screen.getByRole('dialog')
    expect(within(runDialog).queryByRole('button', { name: 'Copy', exact: true })).not.toBeInTheDocument()
    expect(runDialog).toHaveTextContent('/work/selected project')
    await waitFor(() => expect(within(runDialog).getByRole('button', { name: 'Cancel' })).toHaveFocus())
    confirmRun()
    expect(terminal.send).not.toHaveBeenCalled()
    act(() => terminal.ready?.())
    expect(terminal.addTab).toHaveBeenCalledExactlyOnceWith('/work/selected project')
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(screen.getByLabelText('Message input')).toHaveValue('! pwd')
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it.each([
    [{ placeholder: 'Ask about this selection' }, /Ask about this selection/],
    [{ connected: false }, /offline/i],
    [{ disabled: true }, /stopping/i],
    [{ continuable: true, continueIsRecovery: true, onContinue: vi.fn() }, /interrupted/i],
  ] satisfies [Partial<React.ComponentProps<typeof ChatInput>>, RegExp][])('keeps an existing placeholder ahead of terminal discovery: %j', (overrides, expected) => {
    setup({ value: '', ...overrides })
    expect(screen.getByLabelText('Message input')).toHaveAttribute('placeholder', expect.stringMatching(expected))
  })

  it('keeps recording and transcription guidance ahead of terminal discovery', () => {
    const props = { value: '', onChange: vi.fn(), onSend: vi.fn(), terminalCommands: 'local' as const }
    const { rerender } = renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ voiceRecording: true }}><ChatInput {...props} /></ComposerVoiceSliceOverride>,
    )
    expect(screen.getByLabelText('Message input')).toHaveAttribute('placeholder', expect.stringMatching(/recording/i))
    rerender(
      <ComposerVoiceSliceOverride inputProps={{ voiceTranscribing: true }}><ChatInput {...props} /></ComposerVoiceSliceOverride>,
    )
    expect(screen.getByLabelText('Message input')).toHaveAttribute('placeholder', expect.stringMatching(/transcribing/i))
  })

  it.each([
    ['! pwd', 'pwd'],
    ['  ! printf "$HOME\\n" | cat', 'printf "$HOME\\n" | cat'],
    ['!\npwd\nls', 'pwd\nls'],
    ['! echo tail\\ ', 'echo tail\\ '],
    ['! pwd \t\n', 'pwd \t\n'],
    ['!', ''],
    ['! \t\n ', ''],
    ['!!', null],
    ['!important', null],
    ['![image](photo.png)', null],
    ['say ! pwd', null],
  ])('parses %j as %j', (input, expected) => {
    expect(terminalCommand(input)).toBe(expected)
  })

  it('preserves trailing whitespace in a typed command through review and delivery', () => {
    const command = 'echo tail\\ '
    const { props } = setup({ value: `! ${command}` })
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    expect(screen.getByRole('dialog').querySelector('pre')?.textContent).toBe(command)
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    confirmRun()
    expect(terminal.send).not.toHaveBeenCalled()
    act(() => { terminal.ready?.(); terminal.ready?.() })
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', command, { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each([
    ['ru', 'Проверить команду терминала', 'Проверить и выполнить в терминале'],
    ['bn', 'টার্মিনাল কমান্ড দেখে নাও', 'পর্যালোচনা করে টার্মিনালে চালাও'],
  ] as const)('names the review button by its visible %s label', async (language, label, title) => {
    await changeLanguage(language)
    setup()
    const run = screen.getByRole('button', { name: label, exact: true })
    expect(run).toHaveTextContent(label)
    expect(run).toHaveAccessibleName(label)
    expect(run).toHaveAttribute('title', title)
  })

  it.each(['idle', 'busy', 'stopping', 'killing'] as const)('runs without an agent when %s', state => {
    const onStop = vi.fn()
    const { props } = setup({
      isRunning: state !== 'idle',
      canSteer: true,
      stopState: state === 'stopping' ? 'soft_pending' : state === 'killing' ? 'killing' : 'idle',
      onStop,
    })
    const run = screen.getByRole('button', { name: 'Review terminal command' })
    expect(run).toHaveTextContent(/^Review terminal command$/)
    expect(run).toHaveAttribute('title', 'Review and run in terminal')
    fireEvent.click(run)
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(screen.getByRole('dialog')).toHaveTextContent('/work/selected project')
    expect(screen.getByRole('dialog')).toHaveTextContent("This will run in a new terminal tab, in this chat's working directory.")
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(terminal.ready).toBeNull()
    confirmRun()
    expect(terminal.addTab).toHaveBeenCalledWith('/work/selected project')
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
  })

  it.each([
    ['idle', 'Stop generation'],
    ['soft_pending', 'Force kill session (discards in-progress work and queued messages)'],
    ['killing', 'Force reset session (taking longer than expected)'],
  ] as const)('keeps voice and %s cancellation usable outside terminal review', (stopState, stopLabel) => {
    const onStop = vi.fn()
    const onVoiceToggle = vi.fn()
    const { props, rerender } = setup({ isRunning: true, canSteer: true, onStop })
    if (stopState === 'killing') vi.useFakeTimers()
    rerender(
      <ComposerVoiceSliceOverride inputProps={{ onVoiceToggle }}>
        <ChatInput {...props} isRunning={stopState === 'idle'} stopState={stopState} />
      </ComposerVoiceSliceOverride>,
    )
    const run = screen.getByRole('button', { name: 'Review terminal command' })
    const review = screen.getByRole('group', { name: 'Review and run in terminal' })
    expect(within(review).getAllByRole('button')).toEqual([run])
    expect(within(review).getByRole('status')).toHaveTextContent('Terminal panel · selected project')
    expect(run).toBeEnabled()
    const mic = screen.getByRole('button', { name: 'Voice input' })
    expect(review).not.toContainElement(mic)
    expect(mic).toBeEnabled()
    fireEvent.click(mic)
    expect(onVoiceToggle).toHaveBeenCalledOnce()
    if (stopState === 'killing') {
      expect(screen.getByRole('button', { name: 'Killing session' })).toBeDisabled()
      act(() => { vi.advanceTimersByTime(KILLING_ESCAPE_MS) })
    }

    const stop = screen.getByRole('button', { name: stopLabel })
    expect(review).not.toContainElement(stop)
    expect(stop).toBeEnabled()
    fireEvent.click(stop)
    expect(onStop).toHaveBeenCalledOnce()
    expect(screen.getByLabelText('Message input')).toHaveValue('! pwd')
    expect(props.onChange).not.toHaveBeenCalled()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()

    fireEvent.click(run)
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(onStop).toHaveBeenCalledOnce()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each(['soft_pending', 'killing'] as const)('offers only Run when %s has no stop callback', stopState => {
    setup({ isRunning: true, canSteer: true, stopState })
    expect(screen.getByRole('button', { name: 'Review terminal command' })).toBeEnabled()
    expect(screen.queryByTestId('stop-button-pulsing')).not.toBeInTheDocument()
    expect(screen.queryByTestId('stop-button-killing')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument()
  })

  it('restores review with the command draft when the collapsed composer expands', async () => {
    localStorage.setItem('mc-composer-collapsed', '1')
    const { props } = setup({ collapsible: true })
    expect(screen.queryByRole('button', { name: 'Review terminal command' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('composer-collapsed-bar'))
    const review = await screen.findByRole('button', { name: 'Review terminal command' })
    expect(screen.getByLabelText('Message input')).toHaveValue('! pwd')
    fireEvent.click(review)
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each(['click', 'enter'] as const)('refuses a staged folder in a terminal draft submitted by %s', submit => {
    const onSend = vi.fn()
    const onSteer = vi.fn()
    const onChange = vi.fn()
    function Host() {
      const [value, setValue] = React.useState('@docs/ ')
      return (
        <ChatInput
          value={value}
          onChange={next => { onChange(next); setValue(next) }}
          onSend={onSend}
          onSteer={onSteer}
          pendingDirs={parseDirTokens(value).map(token => token.rel)}
          project="/work/proj"
          terminalCommands="local"
        />
      )
    }
    renderWithProviders(<Host />)
    expect(screen.getByTitle('docs/')).toBeVisible()
    const input = screen.getByLabelText('Message input')
    fireEvent.change(input, { target: { value: '! ls @docs/' } })
    onChange.mockClear()
    if (submit === 'click') fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    else fireEvent.keyDown(input, { key: 'Enter' })

    expect(screen.getByText('Remove folder attachments before running. Removing a folder also removes its @path reference from the command.')).toBeVisible()
    expect(input).toHaveValue('! ls @docs/')
    expect(screen.getByTitle('docs/')).toBeVisible()
    expect(onChange).not.toHaveBeenCalled()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('uses Enter for a command even when the busy default is steer', () => {
    const { props } = setup({ isRunning: true, canSteer: true })
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(terminal.addTab).not.toHaveBeenCalled()
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each([false, true])('requires confirmation for an actual OPTIONS chip selection (Lexical: %s)', async lexicalComposer => {
    const option = '! cat ~/.aws/credentials'
    const { options } = parseOptions(`Suggested next step [OPTIONS: ${option} | Skip]`)
    const onSend = vi.fn()
    const onSteer = vi.fn()
    function Host() {
      const [value, setValue] = React.useState('')
      return (
        <ChatInput
          value={value}
          onChange={setValue}
          onSend={onSend}
          onSteer={onSteer}
          terminalCommands="local"
          lexicalComposer={lexicalComposer}
          followUpOptions={options}
          quickSend={false}
          onFollowUpSelect={text => setValue(previous => appendFollowUpOption(previous, null, text).value)}
        />
      )
    }
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox')
    fireEvent.click(screen.getByRole('button', { name: option }))
    const expectDraft = () => {
      if (lexicalComposer) expect(input).toHaveTextContent(option)
      else expect(input).toHaveValue(option)
    }
    await waitFor(expectDraft)
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'Enter' })
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('cat ~/.aws/credentials')
    expect(dialog).toHaveTextContent('Reads credential files')
    expect(within(dialog).getByRole('button', { name: 'Run anyway' })).toBeInTheDocument()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expectDraft()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
    expect(onSteer).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'cat ~/.aws/credentials', { preserveTrailingWhitespace: true })
    expect(onSend).not.toHaveBeenCalled()
    expect(onSteer).not.toHaveBeenCalled()
  })

  it.each([false, true])('reviews an edited terminal draft sent by a picked follow-up (Lexical: %s)', async lexicalComposer => {
    const onSend = vi.fn()
    const onSteer = vi.fn()
    const onFollowUpSend = vi.fn()
    function Host({ draft }: { draft?: string }) {
      const [value, setValue] = React.useState('')
      const [picked, setPicked] = React.useState<Set<string>>(new Set())
      return (
        <ChatInput
          value={draft ?? value}
          onChange={setValue}
          onSend={onSend}
          onSteer={onSteer}
          onFollowUpSend={onFollowUpSend}
          isRunning={lexicalComposer}
          canSteer
          terminalCommands="local"
          project="/work/selected project"
          lexicalComposer={lexicalComposer}
          followUpOptions={['Explain']}
          followUpPicked={picked}
          followUpSourceKey="assistant-row"
          quickSend={false}
          onFollowUpSelect={text => { setValue(text); setPicked(new Set([text])) }}
        />
      )
    }
    const { rerender } = renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox')
    const expectDraft = (text: string) => {
      if (lexicalComposer) expect(input).toHaveTextContent(text)
      else expect(input).toHaveValue(text)
    }
    fireEvent.click(screen.getByRole('button', { name: 'Explain', exact: true }))
    await waitFor(() => expectDraft('Explain'))
    // The parent keeps the chip picked across ordinary composer edits.
    if (lexicalComposer) rerender(<Host draft="! pwd" />)
    else fireEvent.change(input, { target: { value: '! pwd' } })
    await waitFor(() => expectDraft('! pwd'))

    fireEvent.doubleClick(screen.getByRole('button', { name: 'Explain', exact: true }))
    expect(onFollowUpSend).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(terminal.addTab).not.toHaveBeenCalled()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expectDraft('! pwd')

    fireEvent.click(screen.getByRole('button', { name: 'Send now: Explain' }))
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.addTab).toHaveBeenCalledExactlyOnceWith('/work/selected project')
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(onFollowUpSend).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('reviews a terminal draft when a follow-up supplies the empty-text fallback', () => {
    const onFollowUpSend = vi.fn()
    const { props } = setup({
      followUpOptions: [''], onFollowUpSelect: vi.fn(), onFollowUpSend, quickSend: false,
    })
    fireEvent.click(screen.getByTitle('Send now'))
    expect(onFollowUpSend).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(terminal.addTab).not.toHaveBeenCalled()
  })

  it.each([
    ['! pwd', 'Explain', false, 'Explain'],
    ['! pwd', ' ', false, ' '],
    ['Explain this draft', 'Explain', true, undefined],
    ['Explain this draft', '', false, ''],
  ] as const)('preserves follow-up chat routing for draft %j, option %j, picked %s', (value, option, picked, expected) => {
    const onFollowUpSend = vi.fn()
    const { props } = setup({
      value, pendingFiles: ['/work/image.png'],
      followUpOptions: [option], followUpPicked: new Set(picked ? [option] : []),
      followUpSourceKey: 'assistant-row', onFollowUpSelect: vi.fn(), onFollowUpSend, quickSend: false,
    })
    fireEvent.click(screen.getByTitle('Send now'))
    expect(onFollowUpSend).toHaveBeenCalledExactlyOnceWith(expected, 'assistant-row')
    expect(props.onChange).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Message input')).toHaveValue(value)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each(['{Enter>3/}', '{Enter}{Enter}'])('keeps repeated Enter %s from approving an expanded command', async keys => {
    const user = userEvent.setup()
    const block = { id: 'command', seq: 1, content: 'pwd\nprintf "reviewed\\n"', lines: 2 }
    const value = `! ${formatToken(block)}`
    const onPasteBlocksChange = vi.fn()
    const { props, rerender } = setup({ value, pasteBlocks: [block], onPasteBlocksChange })
    const input = screen.getByLabelText('Message input')
    await user.click(input)
    await user.keyboard('{Enter}')
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('printf "reviewed\\n"')
    expect(within(dialog).queryByRole('button', { name: 'Run anyway' })).not.toBeInTheDocument()
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus())

    // userEvent activates focused buttons; keyDown alone cannot observe an
    // accidental native Enter-to-click confirmation.
    await user.keyboard(keys)
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(onPasteBlocksChange).not.toHaveBeenCalled()
    expect(input).toHaveValue(value)
    // A second Enter may reopen after focus returns to the composer. Close
    // any such review before exercising an intentional keyboard confirmation.
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(input)
    await user.keyboard('{Enter}')
    const reopened = await screen.findByRole('dialog')
    await waitFor(() => expect(within(reopened).getByRole('button', { name: 'Cancel' })).toHaveFocus())
    const replacement = { ...block, content: 'echo replaced' }
    rerender(<ChatInput {...props} pasteBlocks={[replacement]} />)
    expect(reopened).toHaveTextContent('printf "reviewed\\n"')
    expect(reopened).not.toHaveTextContent(replacement.content)
    await user.tab()
    expect(within(reopened).getByRole('button', { name: 'Run', exact: true })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(terminal.addTab).toHaveBeenCalledExactlyOnceWith('/work/selected project')
    expect(terminal.send).not.toHaveBeenCalled()
    act(() => { terminal.ready?.(); terminal.ready?.() })
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', block.content, { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(onPasteBlocksChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each(['!ls', '![photo](photo.png)', '!!', '!important'])('keeps %j on its existing chat send path without a terminal warning', value => {
    const { props } = setup({ value })
    expect(screen.queryByRole('button', { name: 'Review terminal command' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText(/space after !/)).not.toBeInTheDocument()
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(props.onSend).toHaveBeenCalledOnce()
    expect(terminal.addTab).not.toHaveBeenCalled()
  })

  it('closes chat path completion before sending a literal shell path', () => {
    const onSend = vi.fn()
    function Host() {
      const [value, setValue] = React.useState('')
      return <ChatInput value={value} onChange={setValue} onSend={onSend} project="/work/proj" terminalCommands="local" />
    }
    renderWithProviders(<Host />)
    const input = screen.getByLabelText('Message input')
    fireEvent.change(input, { target: { value: 'read ./' } })
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    fireEvent.change(input, { target: { value: '! cat ./' } })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(terminal.addTab).not.toHaveBeenCalled()
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'cat ./', { preserveTrailingWhitespace: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('does not treat the prompt-optimizer shortcut as permission to run a command', () => {
    const { props } = setup({ sendOnEnter: 'ctrl-enter' })
    fireEvent.keyDown(screen.getByLabelText('Message input'), {
      key: 'Enter', ctrlKey: true, shiftKey: true,
    })
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
  })

  it.each([
    [{ terminalCommands: 'remote' as const }, 'Terminal commands are unavailable for sessions on another server. Remove the leading ! to return to chat.'],
    [{ terminalCommands: 'pending' as const }, 'Wait for session details to load, or select another session.'],
    [{ pendingFiles: ['/work/image.png'] }, 'Remove attachments before running a terminal command.'],
    [{ value: '!' }, "Type a command after !. You'll review it before it runs."],
    [{ value: '! \t\n ' }, "Type a command after !. You'll review it before it runs."],
  ] satisfies [Partial<React.ComponentProps<typeof ChatInput>>, string][])('explains and disables a known refusal before submission: %j', (overrides, message) => {
    const { props } = setup(overrides)
    const run = screen.getByRole('button', { name: 'Review terminal command' })
    expect(run).toBeDisabled()
    expect(screen.getByText(message)).toBeVisible()
    fireEvent.click(run)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('keeps an offline command disabled and preserves its draft', () => {
    const { props } = setup({ connected: false })
    expect(screen.getByRole('button', { name: 'Review terminal command' })).toBeDisabled()
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('honors the terminal feature flag', () => {
    terminal.enabled = false
    const { props } = setup()
    expect(screen.getByRole('button', { name: 'Review terminal command' })).toBeDisabled()
    expect(screen.getByText('Terminal is turned off. Enable dashboard.terminal.enabled in the server configuration. Remove the leading ! to return to chat.')).toBeVisible()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
  })

  it.each([
    ['disabled', false],
    ['disabled', true],
    ['remote', false],
    ['remote', true],
  ] as const)('routes a %s draft without its terminal marker to chat (Lexical: %s)', async (refusal, lexicalComposer) => {
    terminal.enabled = refusal !== 'disabled'
    const user = userEvent.setup()
    const onSend = vi.fn()
    function Host({ draft }: { draft?: string }) {
      const [value, setValue] = React.useState('! note')
      const currentValue = draft ?? value
      return (
        <ChatInput
          value={currentValue}
          onChange={setValue}
          onSend={() => onSend(currentValue)}
          terminalCommands={refusal === 'remote' ? 'remote' : 'local'}
          lexicalComposer={lexicalComposer}
        />
      )
    }
    const { rerender } = renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox')
    expect(screen.getByRole('button', { name: 'Review terminal command' })).toBeDisabled()
    expect(screen.getByText(/Remove the leading ! to return to chat\./)).toBeVisible()
    await user.click(input)
    await user.keyboard('{Enter}')
    expect(onSend).not.toHaveBeenCalled()
    expect(terminal.addTab).not.toHaveBeenCalled()
    if (lexicalComposer) expect(input).toHaveTextContent('! note')
    else expect(input).toHaveValue('! note')

    if (lexicalComposer) {
      // Latent Lexical value-prop coverage; native App deletion uses the textarea.
      rerender(<Host draft=" note" />)
    } else {
      await user.pointer({ target: input, offset: 0, keys: '[MouseLeft]' })
      await user.keyboard('{Delete}')
    }
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Review terminal command' })).not.toBeInTheDocument()
      expect(screen.queryByText(/Remove the leading ! to return to chat\./)).not.toBeInTheDocument()
      if (lexicalComposer) expect(input.textContent).toBe(' note')
    })
    expect(onSend).not.toHaveBeenCalled()
    await user.keyboard('{Enter}')
    expect(onSend).toHaveBeenCalledExactlyOnceWith(' note')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
  })

  it('asks for the main window when the host has no terminal dock', () => {
    const onSend = vi.fn()
    const onChange = vi.fn()
    renderWithAppProviders(<ChatInput value="! pwd" onChange={onChange} onSend={onSend} terminalCommands="local" />)
    expect(screen.getByRole('button', { name: 'Review terminal command' })).toBeDisabled()
    expect(screen.getByText('Open this session in the main window to run terminal commands.')).toBeVisible()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('returns a detached terminal before creating the command tab', () => {
    vi.useFakeTimers()
    const props = { value: '! pwd', onChange: vi.fn(), onSend: vi.fn(), terminalCommands: 'local' as const }
    const { rerender } = renderWithAppProviders(
      <TerminalHostContext.Provider value="detached"><ChatInput {...props} /></TerminalHostContext.Provider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(terminal.ready).toBeNull()
    confirmRun()
    expect(terminal.bringBack).toHaveBeenCalledOnce()
    expect(terminal.addTab).not.toHaveBeenCalled()
    rerender(<TerminalHostContext.Provider value="docked"><ChatInput {...props} /></TerminalHostContext.Provider>)
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('does not create a terminal when a timed-out popout returns late', () => {
    vi.useFakeTimers()
    const props = { value: '! pwd', onChange: vi.fn(), onSend: vi.fn(), terminalCommands: 'local' as const }
    const { rerender } = renderWithAppProviders(
      <TerminalHostContext.Provider value="detached"><ChatInput {...props} /></TerminalHostContext.Provider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    act(() => { vi.advanceTimersByTime(6000) })
    expect(screen.getByRole('alert')).toHaveTextContent('Command not sent. Your draft is kept; see the terminal panel.')
    expect(terminal.addTab).not.toHaveBeenCalled()
    rerender(<TerminalHostContext.Provider value="docked"><ChatInput {...props} /></TerminalHostContext.Provider>)
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('keeps the draft and reports failure at the terminal cap', () => {
    terminal.addTab.mockReturnValue(null)
    const { props } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    expect(terminal.addTab).not.toHaveBeenCalled()
    confirmRun()
    expect(screen.getByRole('alert')).toHaveTextContent('Terminal limit reached. Close an unused terminal tab, then try again.')
    expect(screen.getByLabelText('Message input')).toHaveValue('! pwd')
    expect(terminal.addTab).toHaveBeenCalledExactlyOnceWith('/work/selected project')
    expect(terminal.ready).toBeNull()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    const review = screen.getByRole('button', { name: 'Review terminal command' })
    expect(review).toBeEnabled()
    expect(review).toHaveAttribute('title', expect.stringContaining('Run opens the terminal panel'))
    expect(review).toHaveAttribute('title', expect.stringContaining('Nothing is queued'))
    fireEvent.click(review)
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    expect(terminal.addTab).toHaveBeenCalledTimes(1)
    // Closing a tab makes the next deliberate Run succeed.
    terminal.addTab.mockReturnValue('terminal-retry')
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-retry', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each(['invalid_cwd', 'limit', 'failed'] as const)('dismisses %s without changing the draft or retrying the command', failure => {
    if (failure === 'limit') terminal.addTab.mockReturnValueOnce(null)
    if (failure === 'failed') vi.useFakeTimers()
    const onStop = vi.fn()
    const { props } = setup({ onStop })
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    const oldReady = terminal.ready
    if (failure === 'invalid_cwd') act(() => terminal.failure?.())
    if (failure === 'failed') act(() => { vi.advanceTimersByTime(6000) })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(failure === 'limit' ? /Terminal limit reached/
      : 'Command not sent. Your draft is kept; see the terminal panel.')
    fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Message input')).toHaveValue('! pwd')
    expect(props.onChange).not.toHaveBeenCalled()
    act(() => oldReady?.())
    expect(terminal.addTab).toHaveBeenCalledOnce()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()

    // Dismissal does not hide a later failed attempt, or consume a later Run.
    terminal.addTab.mockReturnValueOnce(null)
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    expect(screen.getByRole('alert')).toHaveTextContent(/Terminal limit reached/)
    fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Dismiss' }))
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
  })

  it('reports a rejected workspace immediately and retries only on a new submission', () => {
    const { props, rerender } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    const oldReady = terminal.ready
    act(() => terminal.failure?.())
    expect(screen.getByText('Command not sent. Your draft is kept; see the terminal panel.')).toBeVisible()
    expect(screen.getByLabelText('Message input')).toHaveValue('! pwd')
    expect(props.onChange).not.toHaveBeenCalled()
    act(() => oldReady?.())
    expect(terminal.send).not.toHaveBeenCalled()

    rerender(<ChatInput {...props} project="/work/recovered" />)
    expect(screen.queryByText('Command not sent. Your draft is kept; see the terminal panel.')).not.toBeInTheDocument()
    expect(terminal.addTab).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    act(() => terminal.ready?.())
    expect(terminal.addTab).toHaveBeenLastCalledWith('/work/recovered')
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
  })

  it('never delivers a timed-out command after a late ready event', () => {
    vi.useFakeTimers()
    const { props } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    act(() => { vi.advanceTimersByTime(6000) })
    act(() => terminal.ready?.())
    expect(terminal.unsubscribe).toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(screen.getByText('Command not sent. Your draft is kept; see the terminal panel.')).toBeVisible()
  })

  it.each(['dialog', 'handoff'] as const)('does not clear a draft replaced during the %s', phase => {
    const { props, rerender } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    if (phase === 'handoff') confirmRun()
    rerender(<ChatInput {...props} value="my next question" />)
    if (phase === 'dialog') {
      expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
      expect(screen.getByRole('dialog')).not.toHaveTextContent('my next question')
      confirmRun()
    }
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledWith('terminal-1', 'pwd', { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it.each(['dialog', 'handoff'] as const)('keeps a replacement paste with the same chip label during the %s', phase => {
    const original = { id: 'original', seq: 1, content: 'pwd\nls\nwhoami', lines: 3 }
    const replacement = { ...original, content: 'echo one\necho two\necho three' } // same id: same chip whose content was swapped underneath
    const { props, rerender } = setup({
      value: `! ${formatToken(original)}`,
      pasteBlocks: [original],
      onPasteBlocksChange: vi.fn(),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    if (phase === 'handoff') confirmRun()
    expect(formatToken(replacement)).toBe(formatToken(original))
    rerender(<ChatInput {...props} pasteBlocks={[replacement]} />)
    if (phase === 'dialog') {
      expect(screen.getByRole('dialog')).toHaveTextContent('whoami')
      expect(screen.getByRole('dialog')).not.toHaveTextContent('echo three')
      expect(terminal.addTab).not.toHaveBeenCalled()
      confirmRun()
    }
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', original.content, { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onPasteBlocksChange).not.toHaveBeenCalled()
  })

  it.each(['slot', 'project', 'target', 'pending'] as const)('cancels an open confirmation when its %s changes', async context => {
    const props = { value: '! pwd', onChange: vi.fn(), onSend: vi.fn(), terminalCommands: 'local' as const, project: '/work/a' }
    const { rerender } = renderWithProviders(
      <SlotProvider slotId="first"><ChatInput {...props} /></SlotProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('pwd')
    rerender(
      <SlotProvider slotId={context === 'slot' ? 'second' : 'first'}>
        <ChatInput {...props} project={context === 'project' ? '/work/b' : props.project} terminalCommands={context === 'pending' ? 'pending' : context === 'target' ? 'remote' : 'local'} />
      </SlotProvider>,
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    rerender(<SlotProvider slotId="first"><ChatInput {...props} /></SlotProvider>)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
  })

  it('does not revive a cancelled request from the dialog exit animation', () => {
    const { props } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    const dialog = screen.getByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Run', exact: true })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    // AnimatePresence keeps this mounted briefly after cancellation.
    fireEvent.click(confirm)
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(terminal.bringBack).not.toHaveBeenCalled()
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
  })

  it.each([
    ['dialog', 'flag'], ['dialog', 'attachments'],
    ['handoff', 'flag'], ['handoff', 'attachments'],
  ] as const)('rechecks %s-time %s changes before terminal execution', (phase, changed) => {
    const { props, rerender } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    if (phase === 'handoff') confirmRun()
    if (changed === 'flag') terminal.enabled = false
    else rerender(<ChatInput {...props} pendingDirs={['docs/']} />)
    if (phase === 'dialog') {
      confirmRun()
      expect(terminal.addTab).not.toHaveBeenCalled()
      expect(terminal.bringBack).not.toHaveBeenCalled()
    } else act(() => terminal.ready?.())
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each(['slot', 'pending'] as const)('cancels the handoff when its %s changes, without replay after recovery', async context => {
    const props = { value: '! pwd', onChange: vi.fn(), onSend: vi.fn(), terminalCommands: 'local' as const }
    const { rerender } = renderWithProviders(
      <SlotProvider slotId="first"><ChatInput {...props} /></SlotProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    const ready = terminal.ready
    rerender(<SlotProvider slotId={context === 'slot' ? 'second' : 'first'}><ChatInput {...props} terminalCommands={context === 'pending' ? 'pending' : 'local'} /></SlotProvider>)
    act(() => ready?.())
    expect(terminal.unsubscribe).toHaveBeenCalled()
    rerender(<SlotProvider slotId="first"><ChatInput {...props} /></SlotProvider>)
    act(() => ready?.())
    expect(terminal.addTab).toHaveBeenCalledOnce()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(terminal.send).not.toHaveBeenCalled()
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
  })

  it('keeps pane workspaces and pending commands independent of another pane changing sessions', async () => {
    const clearA = vi.fn()
    const clearB = vi.fn()
    const onSend = vi.fn()
    terminal.addTab.mockReturnValueOnce('terminal-a').mockReturnValueOnce('terminal-b')
    function Panes({ slotA }: { slotA: string }) {
      return (
        <>
          <div data-testid="pane-a">
            <SlotProvider slotId={slotA}>
              <ChatInput value="! pwd" onChange={clearA} onSend={onSend} project="/work/a" terminalCommands="local" />
            </SlotProvider>
          </div>
          <div data-testid="pane-b">
            <SlotProvider slotId="session-b">
              <ChatInput value="! ls" onChange={clearB} onSend={onSend} project="/work/b" terminalCommands="local" />
            </SlotProvider>
          </div>
        </>
      )
    }
    const { rerender } = renderWithProviders(<Panes slotA="session-a" />)
    fireEvent.click(within(screen.getByTestId('pane-a')).getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    fireEvent.click(within(screen.getByTestId('pane-b')).getByRole('button', { name: 'Review terminal command' }))
    confirmRun()
    expect(terminal.addTab.mock.calls).toEqual([['/work/a'], ['/work/b']])

    rerender(<Panes slotA="session-c" />)
    act(() => {
      terminal.callbacks.get('terminal-a')?.()
      terminal.callbacks.get('terminal-b')?.()
    })
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-b', 'ls', { preserveTrailingWhitespace: true })
    expect(clearA).not.toHaveBeenCalled()
    expect(clearB).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
  })

  it('preserves trailing whitespace in expanded pasted shell text without rewriting variables or file sigils', () => {
    const block = { id: 'paste', seq: 1, content: 'echo "$HOME"\ncat @data\necho tail\\ ', lines: 3 }
    const { props } = setup({
      value: `! ${formatToken(block)}`,
      pasteBlocks: [block],
      onPasteBlocksChange: vi.fn(),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    const lines = screen.getByRole('dialog').querySelectorAll('pre > div > span:last-child')
    expect(Array.from(lines, line => line.textContent).join('\n')).toBe(block.content)
    expect(terminal.addTab).not.toHaveBeenCalled()
    expect(props.onPasteBlocksChange).not.toHaveBeenCalled()
    confirmRun()
    expect(terminal.send).not.toHaveBeenCalled()
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', block.content, { preserveTrailingWhitespace: true })
    expect(props.onChange).not.toHaveBeenCalled()
    expect(props.onPasteBlocksChange).not.toHaveBeenCalled()
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onSteer).not.toHaveBeenCalled()
  })

  it.each([
    ['pwd', 'No workspace selected. The command below will run in a new terminal tab, using the terminal’s starting directory.'],
    ['pwd\nls', 'No workspace selected. The 2 lines below will run in a new terminal tab, using the terminal’s starting directory.'],
  ])('reviews the starting directory for %j when a slotless composer has no selected project', async (command, body) => {
    const onChange = vi.fn()
    const onSend = vi.fn()
    renderWithProviders(<SlotProvider slotId={null}><ChatInput value={`! ${command}`} onChange={onChange} onSend={onSend} terminalCommands="local" /></SlotProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).queryByText('Terminal starting directory', { exact: true })).not.toBeInTheDocument()
    expect(dialog).toHaveTextContent(body)
    expect(dialog).not.toHaveTextContent("this chat's working directory")
    await waitFor(() => {
      for (const line of command.split('\n')) {
        expect(within(dialog).getByText(line, { exact: true })).toBeVisible()
      }
    })
    expect(terminal.addTab).not.toHaveBeenCalled()
    confirmRun()
    expect(terminal.addTab).toHaveBeenCalledExactlyOnceWith(undefined)
    act(() => terminal.ready?.())
    expect(terminal.send).toHaveBeenCalledExactlyOnceWith('terminal-1', command, { preserveTrailingWhitespace: true })
    expect(onChange).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
  })
})
