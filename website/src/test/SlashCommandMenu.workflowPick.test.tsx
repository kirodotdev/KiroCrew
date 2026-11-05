import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn(), workflowDefinitions: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'
import { useComposerPickers } from '../chat-core/composer/pickers'
import { ComposerPickerMenus } from '../chat-core/composer/PickerMenus'
import { dashboardPickerSource } from '../components/dashboardPickerSource'

const CMDS = [
  { name: '/compact', description: 'Compact the conversation' },
  { name: '/workflow', description: 'Run a saved workflow' },
]

const WORKFLOWS = [
  { slug: 'nightly-report', name: 'Nightly report', description: 'Summarise the day' },
  { slug: 'triage', name: 'Triage', description: '' },
]

function Harness({ input, onSelect = vi.fn(), onClose = vi.fn(), hidden }: {
  input: string; onSelect?: (c: string) => void; onClose?: () => void; hidden?: readonly string[]
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [qc] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  return (
    <QueryClientProvider client={qc}>
      <div ref={ref} data-testid="anchor">anchor</div>
      <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={onClose} hidden={hidden} />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.slashCommands.mockReset()
  mockApi.workflowDefinitions.mockReset()
  mockApi.slashCommands.mockResolvedValue(CMDS)
  mockApi.workflowDefinitions.mockResolvedValue({ definitions: WORKFLOWS })
})

describe('SlashCommandMenu: /workflow lists the saved workflows to pick from', () => {
  it('lists every saved workflow with its description once "/workflow " is typed', async () => {
    render(<Harness input="/workflow " />)
    expect(await screen.findByText('nightly-report')).toBeInTheDocument()
    expect(screen.getByText('Summarise the day')).toBeInTheDocument()
    // No description: the name stands in.
    expect(screen.getByText('triage')).toBeInTheDocument()
    expect(screen.getByText('Triage')).toBeInTheDocument()
    expect(screen.queryByText('/compact')).not.toBeInTheDocument()
  })

  it('Enter on an exactly typed slug picks that workflow, not a newer longer one', async () => {
    // The library lists newest first, so the longer slug arrives first.
    mockApi.workflowDefinitions.mockResolvedValue({ definitions: [
      { slug: 'deploy-prod', name: 'Deploy prod', description: '' },
      { slug: 'deploy', name: 'Deploy', description: '' },
    ] })
    const onSelect = vi.fn()
    render(<Harness input="/workflow deploy" onSelect={onSelect} />)
    await screen.findByText('deploy-prod')
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/workflow deploy ')
  })

  it('filters by the slug typed so far', async () => {
    render(<Harness input="/workflow tr" />)
    expect(await screen.findByText('triage')).toBeInTheDocument()
    expect(screen.queryByText('nightly-report')).not.toBeInTheDocument()
  })

  it('Enter writes "/workflow <slug> " so the run input can follow', async () => {
    const onSelect = vi.fn()
    render(<Harness input="/workflow night" onSelect={onSelect} />)
    await screen.findByText('nightly-report')
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/workflow nightly-report ')
  })

  it('a click picks the workflow too', async () => {
    const onSelect = vi.fn()
    render(<Harness input="/workflow " onSelect={onSelect} />)
    fireEvent.mouseDown(await screen.findByText('triage'))
    expect(onSelect).toHaveBeenCalledWith('/workflow triage ')
  })

  it('with no matching workflow it closes and leaves Enter to send as typed', async () => {
    const onClose = vi.fn()
    const onSelect = vi.fn()
    render(<Harness input="/workflow zz" onClose={onClose} onSelect={onSelect} />)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(mockApi.workflowDefinitions).toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('a failed workflow read is named, with a Retry that re-reads the list', async () => {
    mockApi.workflowDefinitions.mockRejectedValueOnce(new Error('offline'))
    const onClose = vi.fn()
    const onSelect = vi.fn()
    render(<Harness input="/workflow " onClose={onClose} onSelect={onSelect} />)
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't load saved workflows — Enter sends the message")
    expect(onClose).not.toHaveBeenCalled()
    // A failed read releases Enter, so the draft can still be sent as typed.
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('nightly-report')).toBeInTheDocument()
    expect(mockApi.workflowDefinitions).toHaveBeenCalledTimes(2)
  })

  it('a failed refresh is named above the cached rows, which stay pickable', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const onSelect = vi.fn()
    function Menu() {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <>
          <div ref={ref}>anchor</div>
          <SlashCommandMenu input="/workflow " anchorRef={ref} onSelect={onSelect} onClose={vi.fn()} />
        </>
      )
    }
    render(<QueryClientProvider client={qc}><Menu /></QueryClientProvider>)
    expect(await screen.findByText('nightly-report')).toBeInTheDocument()
    mockApi.workflowDefinitions.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { await qc.refetchQueries({ queryKey: ['workflow-definitions'] }) })
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't load saved workflows")
    expect(screen.getByText('nightly-report')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/workflow nightly-report ')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('holds Enter and says so while the list is still loading', async () => {
    let finish!: (v: { definitions: typeof WORKFLOWS }) => void
    mockApi.workflowDefinitions.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const onClose = vi.fn()
    const onSelect = vi.fn()
    render(<Harness input="/workflow " onClose={onClose} onSelect={onSelect} />)
    expect(await screen.findByRole('status')).toHaveTextContent('Loading saved workflows…')
    expect(onClose).not.toHaveBeenCalled()
    finish({ definitions: WORKFLOWS })
    expect(await screen.findByText('nightly-report')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/workflow nightly-report ')
  })

  it('reads the workflows only once "/workflow " is typed', async () => {
    render(<Harness input="/" />)
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    expect(mockApi.workflowDefinitions).not.toHaveBeenCalled()
  })

  it('lists nothing when the host refuses /workflow', async () => {
    render(<Harness input="/workflow " hidden={['/workflow']} />)
    // The command menu itself still resolves, so wait on its read first.
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalled())
    expect(mockApi.workflowDefinitions).not.toHaveBeenCalled()
    expect(screen.queryByText('nightly-report')).not.toBeInTheDocument()
  })

  it('stops listing once a slug and a space are typed', async () => {
    render(<Harness input="/workflow triage " />)
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalled())
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(mockApi.workflowDefinitions).not.toHaveBeenCalled()
  })
})

/** The composer's real picker wiring: the gate in `pickers.ts` and the menus. */
function Composer({ hiddenCommands }: { hiddenCommands?: readonly string[] } = {}) {
  const [value, setValue] = useState('')
  const anchorRef = useRef<HTMLTextAreaElement>(null)
  const queryClient = useQueryClient()
  const pickers = useComposerPickers({
    source: dashboardPickerSource,
    typedCommandMenus: true,
    hiddenCommands,
    value,
    onChange: setValue,
    composerControl: () => null,
    queryClient,
    slotId: null,
  })
  return (
    <>
      <textarea
        ref={anchorRef}
        aria-label="composer"
        value={value}
        onChange={e => { setValue(e.target.value); pickers.openPickersForText(e.target.value, e.target.value) }}
      />
      <span data-testid="pickers" data-slash-open={String(pickers.slashMenuOpen)} />
      <ComposerPickerMenus pickers={pickers} value={value} onChange={setValue} composerAnchorRef={anchorRef} sendOnEnter="enter" hiddenCommands={hiddenCommands} />
    </>
  )
}

describe('the composer: picking /workflow goes straight on to the workflow list', () => {
  it('Enter on /workflow opens the saved workflows, and Enter again picks one', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><Composer /></QueryClientProvider>)
    const box = screen.getByLabelText('composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '/workf' } })
    expect(await screen.findByText('/workflow')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(box.value).toBe('/workflow ')
    expect(await screen.findByText('nightly-report')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(box.value).toBe('/workflow nightly-report ')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('a host that refuses /workflow opens no menu on "/workflow "', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><Composer hiddenCommands={['/workflow']} /></QueryClientProvider>)
    const box = screen.getByLabelText('composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '/workflow ' } })
    // The gate decides as the menu does: no open picker keeps the composer's keys.
    expect(screen.getByTestId('pickers')).toHaveAttribute('data-slash-open', 'false')
    expect(mockApi.workflowDefinitions).not.toHaveBeenCalled()
  })

  it('picking any other command still closes the menu', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><Composer /></QueryClientProvider>)
    const box = screen.getByLabelText('composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '/comp' } })
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(box.value).toBe('/compact ')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })
})
