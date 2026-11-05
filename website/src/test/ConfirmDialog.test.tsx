/**
 * Unit tests for the shared async confirm surface (useConfirm).
 *
 * The contract every converted call site relies on: the promise resolves
 * `true` only from the confirm button; Cancel, Escape, and the X button all
 * resolve `false`; and the native `window.confirm` is never touched — the
 * native dialog is synchronous, freezes the renderer's event loop, and lets a
 * queued Quit event tear the app down before a follow-up request is sent.
 */
import { lazy, memo, startTransition, StrictMode, Suspense, useEffect, useLayoutEffect, useState, type ReactNode } from 'react'
import { describe, it, expect, onTestFinished, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useConfirm, type ConfirmOptions } from '../components/ConfirmDialog'
import { confirmAction } from './confirmAction'
import { deferred } from './helpers'

const exits = vi.hoisted(() => ({
  current: undefined as (() => void) | undefined,
  approve: undefined as (() => void) | undefined,
}))

vi.mock('../components/Modal', async (importOriginal) => {
  const { default: Modal } = await importOriginal<typeof import('../components/Modal')>()
  return {
    default: (props: React.ComponentProps<typeof Modal>) => {
      exits.current = props.onExitComplete
      return <Modal {...props} />
    },
  }
})

vi.mock('../components/ui', async (importOriginal) => {
  const ui = await importOriginal<typeof import('../components/ui')>()
  return {
    ...ui,
    Btn: (props: React.ComponentProps<typeof ui.Btn> & { 'data-guide-confirm'?: string }) => {
      if (props['data-guide-confirm'] !== undefined) exits.approve = props.onClick as () => void
      return <ui.Btn {...props} />
    },
  }
})

function controlledMicrotasks() {
  const tasks: VoidFunction[] = []
  const spy = vi.spyOn(globalThis, 'queueMicrotask').mockImplementation(task => { tasks.push(task) })
  onTestFinished(() => spy.mockRestore())
  return () => { tasks.splice(0).forEach(task => task()) }
}

function ConditionalProbe({ renderDialog, onAnswer }: {
  renderDialog: boolean
  onAnswer: (ok: boolean) => void
}) {
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  return (
    <>
      <button onClick={() => {
        void confirm({ title: 'Proceed?', confirmLabel: 'Proceed' }).then(onAnswer)
      }}>ask conditionally</button>
      <output data-testid="confirm-open">{String(confirmOpen)}</output>
      {renderDialog && confirmDialog}
    </>
  )
}

function SplitRenderer({ dialog }: { dialog: ReactNode }) {
  const [renderDialog, setRenderDialog] = useState(true)
  const [version, setVersion] = useState(0)
  return (
    <>
      <button onClick={() => setRenderDialog(false)}>remove dialog</button>
      <button onClick={() => setVersion(value => value + 1)}>remount dialog</button>
      <section key={version}>{renderDialog && dialog}</section>
    </>
  )
}

function SplitProbe({ onAnswer }: { onAnswer: (ok: boolean) => void }) {
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  return (
    <>
      <button onClick={() => {
        void confirm({ title: 'Proceed?', confirmLabel: 'Proceed' }).then(onAnswer)
      }}>ask separately</button>
      <output data-testid="confirm-open">{String(confirmOpen)}</output>
      <SplitRenderer dialog={confirmDialog} />
    </>
  )
}

function Probe({ onAnswer, opts }: { onAnswer: (ok: boolean) => void; opts?: Partial<ConfirmOptions> }) {
  const { confirm, confirmDialog } = useConfirm()
  return (
    <>
      <button
        onClick={async () =>
          onAnswer(
            await confirm({
              title: 'Discard draft?',
              body: 'The draft is gone for good.',
              confirmLabel: 'Discard draft',
              ...opts,
            }),
          )
        }
      >
        ask
      </button>
      {confirmDialog}
    </>
  )
}

describe('useConfirm', () => {
  it('answers false when the owner omits its dialog', async () => {
    const answer = deferred<boolean>()
    render(<ConditionalProbe renderDialog={false} onAnswer={answer.resolve} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask conditionally' }))

    await act(async () => expect(await answer.promise).toBe(false))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
  })

  it('answers false when an owner branch removes a pending dialog', async () => {
    const answer = deferred<boolean>()
    const view = render(<ConditionalProbe renderDialog onAnswer={answer.resolve} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask conditionally' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })

    view.rerender(<ConditionalProbe renderDialog={false} onAnswer={answer.resolve} />)

    await act(async () => expect(await answer.promise).toBe(false))
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
  })

  it('answers false when a descendant alone removes the dialog', async () => {
    const answer = deferred<boolean>()
    render(<SplitProbe onAnswer={answer.resolve} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask separately' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })

    fireEvent.click(screen.getByRole('button', { name: 'remove dialog' }))

    await act(async () => expect(await answer.promise).toBe(false))
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
  })

  it('keeps its options until exit while releasing the pending signal on answer', async () => {
    const answer = deferred<boolean>()
    render(<ConditionalProbe renderDialog onAnswer={answer.resolve} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask conditionally' }))
    const dialog = await screen.findByRole('dialog', { name: 'Proceed?' })

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(dialog).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Proceed?')
    expect(screen.getByRole('button', { name: 'Proceed' })).toBeInTheDocument()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
    await act(async () => expect(await answer.promise).toBe(false))
    await waitFor(() => expect(dialog).not.toBeInTheDocument())
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
  })

  it('keeps a committed dialog pending under StrictMode', async () => {
    const onAnswer = vi.fn()
    render(<StrictMode><ConditionalProbe renderDialog onAnswer={onAnswer} /></StrictMode>)
    fireEvent.click(screen.getByRole('button', { name: 'ask conditionally' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })
    expect(onAnswer).not.toHaveBeenCalled()

    await confirmAction('Proceed')

    expect(onAnswer).toHaveBeenCalledExactlyOnceWith(true)
  })

  it.each(['pending', 'answered'] as const)('keeps a newer %s question when an older exit finishes', async (state) => {
    const first = deferred<boolean>()
    const second = deferred<boolean>()
    const onAnswer = vi.fn().mockImplementationOnce(first.resolve).mockImplementationOnce(second.resolve)
    render(<ConditionalProbe renderDialog onAnswer={onAnswer} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask conditionally' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })
    const finishPreviousExit = exits.current
    expect(finishPreviousExit).toBeTypeOf('function')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await act(async () => expect(await first.promise).toBe(false))

    fireEvent.click(screen.getByRole('button', { name: 'ask conditionally' }))
    const newerDialog = await screen.findByRole('dialog', { name: 'Proceed?' })
    if (state === 'answered') fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    act(() => finishPreviousExit!())

    if (state === 'pending') {
      expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')
      expect(onAnswer).toHaveBeenCalledTimes(1)
      await confirmAction('Proceed')
      expect(await second.promise).toBe(true)
    } else {
      expect(newerDialog).toBeInTheDocument()
      expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
      await act(async () => expect(await second.promise).toBe(false))
      await waitFor(() => expect(newerDialog).not.toBeInTheDocument())
      expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
    }
  })

  it('refuses an affirmative callback after a descendant removes its surface', async () => {
    const answer = deferred<boolean>()
    render(<SplitProbe onAnswer={answer.resolve} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask separately' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })
    const approve = exits.approve!

    fireEvent.click(screen.getByRole('button', { name: 'remove dialog' }))
    act(() => approve())

    await act(async () => expect(await answer.promise).toBe(false))
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
  })

  it('ignores detached approval after the same question remounts in one commit', async () => {
    const answer = deferred<boolean>()
    const onAnswer = vi.fn(answer.resolve)
    render(<SplitProbe onAnswer={onAnswer} />)
    fireEvent.click(screen.getByRole('button', { name: 'ask separately' }))
    const oldDialog = await screen.findByRole('dialog', { name: 'Proceed?' })
    const oldApprove = exits.approve!

    fireEvent.click(screen.getByRole('button', { name: 'remount dialog' }))
    const currentDialog = screen.getByRole('dialog', { name: 'Proceed?' })
    expect(oldDialog).not.toBeInTheDocument()
    expect(currentDialog).not.toBe(oldDialog)
    act(() => oldApprove())
    await act(async () => {})

    expect(onAnswer).not.toHaveBeenCalled()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')
    await confirmAction('Proceed')
    expect(await answer.promise).toBe(true)
    expect(onAnswer).toHaveBeenCalledExactlyOnceWith(true)
  })

  it('answers an unmounted replacement false while a renderer retains the older question', async () => {
    const first = deferred<boolean>()
    const second = deferred<boolean>()
    const RetainedRenderer = memo(({ dialog }: { dialog: ReactNode }) => <>{dialog}</>, () => true)
    function Owner() {
      const { confirm, confirmDialog, confirmOpen } = useConfirm()
      const [retaining, setRetaining] = useState(false)
      return (
        <>
          <button onClick={() => { void confirm({ title: 'First?', confirmLabel: 'First' }).then(first.resolve) }}>first</button>
          <button onClick={() => setRetaining(true)}>retain</button>
          <button onClick={() => { void confirm({ title: 'Second?', confirmLabel: 'Second' }).then(second.resolve) }}>second</button>
          <output data-testid="confirm-open">{String(confirmOpen)}</output>
          {retaining ? <RetainedRenderer dialog={confirmDialog} /> : confirmDialog}
        </>
      )
    }
    render(<Owner />)
    fireEvent.click(screen.getByRole('button', { name: 'first', exact: true }))
    await screen.findByRole('dialog', { name: 'First?' })
    fireEvent.click(screen.getByRole('button', { name: 'retain' }))
    const retainedDialog = screen.getByRole('dialog', { name: 'First?' })
    fireEvent.click(screen.getByRole('button', { name: 'second', exact: true }))

    await act(async () => {
      expect(await first.promise).toBe(false)
      expect(await second.promise).toBe(false)
    })
    expect(retainedDialog).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Second?' })).toBeNull()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')
  })

  it('keeps an initial descendant passive-effect question until its controls commit', async () => {
    const flushMicrotasks = controlledMicrotasks()
    const onAnswer = vi.fn()
    function Child({ ask }: { ask: (opts: ConfirmOptions) => Promise<boolean> }) {
      useEffect(() => {
        void ask({ title: 'Proceed?', confirmLabel: 'Proceed' }).then(onAnswer)
      }, [ask])
      return null
    }
    function Owner() {
      const { confirm, confirmDialog, confirmOpen } = useConfirm()
      useEffect(flushMicrotasks)
      return <><Child ask={confirm} /><output data-testid="confirm-open">{String(confirmOpen)}</output>{confirmDialog}</>
    }
    render(<Owner />)

    expect(screen.getByRole('dialog', { name: 'Proceed?' })).toBeInTheDocument()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')
    expect(onAnswer).not.toHaveBeenCalled()
    await confirmAction('Proceed')
    expect(onAnswer).toHaveBeenCalledExactlyOnceWith(true)
  })

  it('ignores an omitted question check when a newer question has not committed yet', async () => {
    const flushMicrotasks = controlledMicrotasks()
    const first = deferred<boolean>()
    const onSecondAnswer = vi.fn()
    function Owner() {
      const { confirm, confirmDialog, confirmOpen } = useConfirm()
      const [show, setShow] = useState(false)
      return (
        <>
          <button onClick={() => { void confirm({ title: 'First?', confirmLabel: 'First' }).then(first.resolve) }}>first</button>
          <button onClick={() => {
            setShow(true)
            void confirm({ title: 'Second?', confirmLabel: 'Second' }).then(onSecondAnswer)
            flushMicrotasks()
          }}>second</button>
          <output data-testid="confirm-open">{String(confirmOpen)}</output>
          {show && confirmDialog}
        </>
      )
    }
    render(<Owner />)
    fireEvent.click(screen.getByRole('button', { name: 'first', exact: true }))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'second', exact: true }))
    await act(async () => expect(await first.promise).toBe(false))
    act(flushMicrotasks)

    expect(screen.getByRole('dialog', { name: 'Second?' })).toBeInTheDocument()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')
    expect(onSecondAnswer).not.toHaveBeenCalled()
    await confirmAction('Second')
    expect(onSecondAnswer).toHaveBeenCalledExactlyOnceWith(true)
  })

  it.each([false, true])('accepts an initial descendant layout-effect ask (StrictMode=%s)', async (strict) => {
    const onAnswer = vi.fn()
    function Child({ ask }: { ask: (opts: ConfirmOptions) => Promise<boolean> }) {
      useLayoutEffect(() => {
        void ask({ title: 'Proceed?', confirmLabel: 'Proceed' }).then(onAnswer)
      }, [ask])
      return null
    }
    function Owner() {
      const { confirm, confirmDialog, confirmOpen } = useConfirm()
      return <><Child ask={confirm} /><output data-testid="confirm-open">{String(confirmOpen)}</output>{confirmDialog}</>
    }
    const owner = <Owner />
    render(strict ? <StrictMode>{owner}</StrictMode> : owner)
    await act(async () => {})

    expect(screen.getByRole('dialog', { name: 'Proceed?' })).toBeInTheDocument()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')
    expect(onAnswer.mock.calls).toEqual(strict ? [[false]] : [])
    await confirmAction('Proceed')
    expect(onAnswer.mock.calls).toEqual(strict ? [[false], [true]] : [[true]])
  })

  it.each([
    { transition: false, name: 'answers a suspended body false when its owner disconnects' },
    { transition: true, name: 'keeps a suspended transition ask open and answerable after React commits' },
  ])('$name', async ({ transition }) => {
    const module = deferred<{ default: () => React.JSX.Element }>()
    const load = vi.fn(() => module.promise)
    const Body = lazy(load)
    const onAnswer = vi.fn()
    let ask!: (opts: ConfirmOptions) => Promise<boolean>
    function Owner() {
      const { confirm, confirmDialog, confirmOpen } = useConfirm()
      ask = confirm
      return <><output data-testid="confirm-open">{String(confirmOpen)}</output>{confirmDialog}</>
    }
    render(<Suspense fallback={<p>Outer loading</p>}><Owner /></Suspense>)
    act(() => {
      const request = () => { void ask({ title: 'Proceed?', confirmLabel: 'Proceed', body: <Body /> }).then(onAnswer) }
      if (transition) startTransition(request)
      else request()
    })
    await act(async () => {})

    expect(load).toHaveBeenCalledTimes(1)
    if (transition) expect(onAnswer).not.toHaveBeenCalled()
    else expect(onAnswer).toHaveBeenCalledExactlyOnceWith(false)
    expect(screen.queryByRole('dialog', { hidden: true })).toBeNull()
    expect(screen.queryByText('Outer loading')).toBeNull()
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('false')

    await act(async () => {
      module.resolve({ default: () => <p>Loaded consequence</p> })
      await module.promise
    })

    if (transition) {
      expect(screen.getByRole('dialog', { name: 'Proceed?' })).toHaveTextContent('Loaded consequence')
      expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')
      expect(onAnswer).not.toHaveBeenCalled()
      await confirmAction('Proceed')
      expect(onAnswer).toHaveBeenCalledExactlyOnceWith(true)
    } else {
      expect(screen.queryByRole('dialog', { hidden: true })).toBeNull()
      expect(screen.queryByRole('button', { name: 'Proceed' })).toBeNull()
      expect(screen.queryByText('Loaded consequence')).toBeNull()
      expect(onAnswer).toHaveBeenCalledExactlyOnceWith(false)
    }
  })

  it.each([false, true])('settles asks across hidden-owner deletion (StrictMode=%s)', async (strict) => {
    const ready = deferred<void>()
    const onAnswer = vi.fn()
    let suspend!: () => void
    let ask!: (opts: ConfirmOptions) => Promise<boolean>
    function Owner() {
      const { confirm, confirmDialog } = useConfirm()
      ask = confirm
      return <>{confirmDialog}</>
    }
    function Sibling({ blocked }: { blocked: boolean }) {
      if (blocked) throw ready.promise
      return <p>Sibling ready</p>
    }
    function Tree() {
      const [blocked, setBlocked] = useState(false)
      suspend = () => setBlocked(true)
      return <Suspense fallback={<p>Outer loading</p>}><Owner /><Sibling blocked={blocked} /></Suspense>
    }
    const tree = <Tree />
    const view = render(strict ? <StrictMode>{tree}</StrictMode> : tree)
    act(() => {
      void ask({ title: 'Proceed?', confirmLabel: 'Proceed' }).then(onAnswer)
      suspend()
    })
    expect(screen.getByText('Outer loading')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { hidden: true })).toBeNull()
    await act(async () => {})
    expect(onAnswer).toHaveBeenCalledExactlyOnceWith(false)

    const whileHidden = vi.fn()
    act(() => {
      void ask({ title: 'Hidden?', confirmLabel: 'Proceed' }).then(whileHidden)
      view.unmount()
    })
    await act(async () => {})
    expect(onAnswer).toHaveBeenCalledExactlyOnceWith(false)
    expect(whileHidden).toHaveBeenCalledExactlyOnceWith(false)
    const afterDeletion = vi.fn()
    await act(async () => {
      void ask({ title: 'Deleted?', confirmLabel: 'Delete' }).then(afterDeletion)
    })
    expect(afterDeletion).toHaveBeenCalledExactlyOnceWith(false)
  })

  it.each([false, true])('accepts descendant layout asks after the owner reveals (StrictMode=%s)', async (strict) => {
    const ready = deferred<void>()
    const onAnswer = vi.fn()
    function Child({ ask }: { ask: (opts: ConfirmOptions) => Promise<boolean> }) {
      useLayoutEffect(() => {
        void ask({ title: 'Proceed?', confirmLabel: 'Proceed' }).then(onAnswer)
      }, [ask])
      return null
    }
    function Owner() {
      const { confirm, confirmDialog } = useConfirm()
      return <><Child ask={confirm} />{confirmDialog}</>
    }
    function Sibling({ blocked }: { blocked: boolean }) {
      if (blocked) throw ready.promise
      return null
    }
    function Tree({ blocked }: { blocked: boolean }) {
      const tree = <Suspense fallback={<p>Outer loading</p>}><Owner /><Sibling blocked={blocked} /></Suspense>
      return strict ? <StrictMode>{tree}</StrictMode> : tree
    }
    const view = render(<Tree blocked={false} />)
    await act(async () => {})
    expect(screen.getByRole('dialog', { name: 'Proceed?' })).toBeInTheDocument()
    view.rerender(<Tree blocked />)
    await act(async () => {})
    expect(screen.getByText('Outer loading')).toBeInTheDocument()
    expect(onAnswer.mock.calls.every(([answer]) => answer === false)).toBe(true)
    const cancelled = onAnswer.mock.calls.length
    expect(cancelled).toBe(strict ? 2 : 1)

    view.rerender(<Tree blocked={false} />)
    await act(async () => {})
    expect(screen.getByRole('dialog', { name: 'Proceed?' })).toBeInTheDocument()
    expect(onAnswer).toHaveBeenCalledTimes(cancelled)
    await confirmAction('Proceed')
    expect(onAnswer).toHaveBeenCalledTimes(cancelled + 1)
    expect(onAnswer).toHaveBeenLastCalledWith(true)
  })

  it('answers false when a retained confirm function is called after owner unmount', async () => {
    let ask!: (opts: ConfirmOptions) => Promise<boolean>
    function Owner() {
      const { confirm } = useConfirm()
      ask = confirm
      return null
    }
    const { unmount } = render(<Owner />)
    unmount()

    expect(await ask({ title: 'Proceed?', confirmLabel: 'Proceed' })).toBe(false)
  })

  it('answers false from the backdrop', async () => {
    const answer = deferred<boolean>()
    render(<Probe onAnswer={answer.resolve} />)
    fireEvent.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    const backdrop = document.querySelector('.backdrop-blur-md')
    expect(backdrop).not.toBeNull()

    fireEvent.click(backdrop!)

    await act(async () => expect(await answer.promise).toBe(false))
  })

  it("marks its confirm button, and only that one, as the guide's confirm signal", async () => {
    const user = userEvent.setup()
    render(<Probe onAnswer={vi.fn()} />)
    await user.click(screen.getByText('ask'))
    const dialog = await screen.findByRole('dialog')
    const marked = dialog.querySelectorAll('[data-guide-confirm]')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toBe(screen.getByRole('button', { name: 'Discard draft' }))
  })

  it('renders a themed dialog and never calls window.confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(screen.getByText('Discard draft?')).toBeInTheDocument()
    expect(screen.getByText('The draft is gone for good.')).toBeInTheDocument()
    // The confirm button restates the action — never "OK".
    expect(screen.getByRole('button', { name: 'Discard draft' })).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()
    // Opening the dialog alone answers nothing.
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('paints on the layer above a full-screen overlay when asked to', async () => {
    // The artifact and file previews' full-screen shells are opaque body
    // portals at z-[9999]; a discard prompt they raise on the ordinary modal
    // layer (z-[101]) would be covered and the button that raised it look dead.
    const onAnswer = vi.fn()
    const { unmount } = render(<Probe onAnswer={onAnswer} opts={{ layer: 'top' }} />)
    fireEvent.click(screen.getByText('ask'))
    const dialog = await screen.findByRole('dialog')
    const layer = dialog.closest('.fixed.inset-0.z-\\[10001\\]')
    expect(layer).not.toBeNull()
    expect(document.querySelector('.z-\\[10000\\]')).not.toBeNull()
    unmount()

    render(<Probe onAnswer={onAnswer} />)
    fireEvent.click(screen.getByText('ask'))
    const plain = await screen.findByRole('dialog')
    expect(plain.closest('.fixed.inset-0.z-\\[101\\]')).not.toBeNull()
    expect(document.querySelector('.z-\\[10001\\]')).toBeNull()
  })

  it('resolves true only from the confirm button', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await user.click(await screen.findByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(true))
    expect(onAnswer).toHaveBeenCalledTimes(1)
  })

  it('resolves false from Cancel', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('resolves false from Escape', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('resolves false from the X button', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    await user.click(screen.getByLabelText('Close'))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('a second ask answers the first "no" instead of leaking its promise', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    await user.click(screen.getByText('ask'))
    // First promise settled false; the second is still pending.
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
    expect(onAnswer).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2))
    expect(onAnswer).toHaveBeenLastCalledWith(true)
  })

  it('an unmount while open answers "no" so the awaiting caller is released', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const { unmount } = render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    unmount()
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('cancel() answers "no" so a caller can withdraw an ask whose subject is gone', async () => {
    function CancelProbe({ onAnswer }: { onAnswer: (ok: boolean) => void }) {
      const { confirm, cancel, confirmDialog } = useConfirm()
      return (
        <>
          <button onClick={async () => onAnswer(await confirm({ title: 'Clear?', confirmLabel: 'Clear' }))}>ask</button>
          <button onClick={cancel}>withdraw</button>
          {confirmDialog}
        </>
      )
    }
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<CancelProbe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    await user.click(screen.getByText('withdraw'))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps cancel() the same function across renders, so it is safe in an effect dependency list', async () => {
    const seen: Array<() => void> = []
    function IdentityProbe() {
      const { confirm, cancel, confirmDialog } = useConfirm()
      seen.push(cancel)
      return (
        <>
          <button onClick={() => { void confirm({ title: 'Clear?', confirmLabel: 'Clear' }) }}>ask</button>
          {confirmDialog}
        </>
      )
    }
    const user = userEvent.setup()
    render(<IdentityProbe />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    expect(seen.length).toBeGreaterThan(1)
    expect(new Set(seen).size).toBe(1)
  })
})
