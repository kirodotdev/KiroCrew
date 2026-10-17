import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createRef, useEffect, useRef, useState, type RefObject } from 'react'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import type { ComposerControl } from '../components/composerControl'

/* ── Trigger detection is one rule for both editor engines. The textarea's own
 *    change handler and the opt-in Lexical editor's change callback must open
 *    the same picker for the same text, and both publish the caret the
 *    dictation splice reads. The Lexical editor is replaced by a plain input
 *    that drives the SAME callback contract (`onChange`, `controlRef`,
 *    `onReady`), because happy-dom cannot drive a contenteditable. ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
  pathComplete: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

const lexicalCaret = vi.hoisted(() => ({ at: 0 }))
vi.mock('../components/LexicalComposerInput', () => {
  function StubLexicalComposerInput({ value, onChange, controlRef, onReady, ariaLabel }: {
    value: string
    onChange: (next: string) => void
    controlRef: RefObject<ComposerControl | null>
    onReady?: () => void
    ariaLabel: string
  }) {
    const ref = useRef<HTMLInputElement>(null)
    useEffect(() => {
      controlRef.current = {
        focus: () => ref.current?.focus(),
        getRootElement: () => ref.current,
        getSelection: () => ({ start: lexicalCaret.at, end: lexicalCaret.at }),
        setSelection: () => {},
      }
      onReady?.()
    }, [controlRef, onReady])
    return (
      <input
        ref={ref}
        data-stub-lexical=""
        aria-label={ariaLabel}
        value={value}
        onChange={e => { lexicalCaret.at = e.target.value.length; onChange(e.target.value) }}
      />
    )
  }
  return { default: StubLexicalComposerInput }
})

import ChatInput from '../components/ChatInput'

const ROOT = '/work/proj'
const SKILLS = [
  { key: 'peer-review-walk', name: 'peer-review-walk', description: 'Walk peer CRs', source: 'kirocrew' },
]

type Engine = 'textarea' | 'lexical'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  lexicalCaret.at = 0
  mockApi.skills.mockResolvedValue(SKILLS)
  mockApi.skillTrust.mockResolvedValue({ project: ROOT, project_key: ROOT })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  mockApi.fileSearch.mockResolvedValue({ results: [] })
  mockApi.pathComplete.mockResolvedValue({
    results: [{ path: `${ROOT}/src`, name: 'src', kind: 'dir' as const, size: 0, mtime: 1750000000 }],
    root: ROOT,
  })
})

function Host({ engine, caretRef }: { engine: Engine; caretRef?: RefObject<{ start: number; end: number } | null> }) {
  const [val, setVal] = useState('')
  const input = (
    <ChatInput
      value={val}
      onChange={setVal}
      onSend={vi.fn()}
      project={ROOT}
      onFileSelect={vi.fn()}
      lexicalComposer={engine === 'lexical'}
    />
  )
  return (
    <SlotProvider slotId="chat-1">
      {caretRef
        ? <ComposerVoiceSliceOverride inputProps={{ voiceCaretRef: caretRef }}>{input}</ComposerVoiceSliceOverride>
        : input}
    </SlotProvider>
  )
}

async function editor(engine: Engine): Promise<HTMLElement> {
  if (engine === 'textarea') return screen.getByLabelText('Message input')
  // The lazy chunk's Suspense fallback carries the same accessible name, so
  // wait for the editor itself rather than for the name.
  return waitFor(() => {
    const el = document.querySelector<HTMLElement>('[data-stub-lexical]')
    if (!el) throw new Error('lexical editor not mounted yet')
    return el
  })
}

async function typeInto(engine: Engine, value: string) {
  fireEvent.change(await editor(engine), { target: { value } })
}

describe.each<Engine>(['textarea', 'lexical'])('ChatInput trigger detection (%s engine)', engine => {
  it('opens the skill picker for a $ token at the caret', async () => {
    renderWithProviders(<Host engine={engine} />)
    await typeInto(engine, 'run $peer')
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByText('$peer-review-walk')).toBeInTheDocument())
  })

  it('opens the path picker for a ./ token, resolved against the project', async () => {
    renderWithProviders(<Host engine={engine} />)
    await typeInto(engine, 'read ./')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './', '', expect.anything()))
  })

  it('opens the slash menu only for a leading slash', async () => {
    renderWithProviders(<Host engine={engine} />)
    await typeInto(engine, 'not /a command')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    await typeInto(engine, '/')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
  })

  it('opens the file picker for an @ token and closes it once the token ends', async () => {
    renderWithProviders(<Host engine={engine} />)
    await typeInto(engine, 'see @re')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await typeInto(engine, 'see @re done')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('publishes the caret the dictation splice reads', async () => {
    const caretRef = createRef<{ start: number; end: number } | null>()
    renderWithProviders(<Host engine={engine} caretRef={caretRef} />)
    await typeInto(engine, 'hello')
    await waitFor(() => expect(caretRef.current).toEqual({ start: 5, end: 5 }))
  })
})
