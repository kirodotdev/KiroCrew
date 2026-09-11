import { describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { ErrorCard, isAuthRequired, isModelUnentitled } from '../pages/chat/ErrorCard'

const setupMeta = (member = 'reviewer') => ({
  code: 'memory_unavailable',
  recovery: { kind: 'initialize_member_memory', member },
})

/**
 * The error row used to be an actionless div whose own copy told the reader to
 * retry. These tests pin the two shapes: settled (no action) and resumable
 * (Continue), plus the guard that a press cannot double-fire.
 */
describe('ErrorCard', () => {
  it('treats retained setup metadata as an ordinary retryable error', () => {
    const content = 'memory_unavailable: Owner setup required.'
    render(<ErrorCard content={content} meta={setupMeta()} onContinue={() => {}} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('Owner setup required.')
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('memory_unavailable:')
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByTestId('error-card-continue')).toBeVisible()
  })

  it('does not derive a setup action or strip diagnostic text from untyped prose', () => {
    const content = 'memory_unavailable: Create private memory.'
    render(<ErrorCard content={content} onContinue={() => {}} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent(content)
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByTestId('error-card-continue')).toBeTruthy()
  })

  it('hides the typed code without inventing initialization recovery for other memory errors', () => {
    render(<ErrorCard content="memory_unavailable: Restore the original binding." meta={{ code: 'memory_unavailable' }} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('Restore the original binding.')
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('memory_unavailable:')
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('renders the prose verbatim with no action when the turn is not resumable', () => {
    render(<ErrorCard content="⟳ Connection lost — please retry." />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('⟳ Connection lost — please retry.')
    // Deliberately ABSENT rather than disabled: a permanently greyed button on a
    // red card reads as a broken feature.
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
  })

  it('renders a Continue action when the turn is resumable', () => {
    render(<ErrorCard content="boom" onContinue={() => {}} />)
    expect(screen.getByTestId('error-card-continue')).toBeTruthy()
    expect(screen.getByTestId('error-card')).toHaveAttribute('data-continuable', 'true')
  })

  it('invokes onContinue on press', () => {
    const onContinue = vi.fn()
    render(<ErrorCard content="boom" onContinue={onContinue} />)
    fireEvent.click(screen.getByTestId('error-card-continue'))
    expect(onContinue).toHaveBeenCalledTimes(1)
  })

  it('disables the action while a continue is in flight', () => {
    const onContinue = vi.fn()
    render(<ErrorCard content="boom" onContinue={onContinue} continuing />)
    const btn = screen.getByTestId('error-card-continue') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('keeps the error prose visible in the resumable shape', () => {
    render(<ErrorCard content="⟳ Session busy — please retry." onContinue={() => {}} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('⟳ Session busy — please retry.')
  })
})

/**
 * A model-entitlement rejection is the one error whose fix is not a retry. Its
 * row swaps Continue for the two actions that end it: pick a served model, and
 * change the default the next session would start on.
 */
describe('ErrorCard — model entitlement rejection', () => {
  it('offers pick-model and default-model actions and NO Continue, even when resumable', () => {
    const onContinue = vi.fn()
    const onPickModel = vi.fn()
    const onOpenDefaultModel = vi.fn()
    render(
      <ErrorCard
        content="❌ Your account does not have access to model 'auto'."
        onContinue={onContinue}
        onPickModel={onPickModel}
        onOpenDefaultModel={onOpenDefaultModel}
      />,
    )
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    // Both actions present -> the "do both" dependency is stated on the card,
    // and the hint names each button by its own label so the pair does not
    // read as the same action twice (no positional "the first / the second").
    const bothHint = screen.getByTestId('error-card-both-hint')
    expect(bothHint).toHaveTextContent('Choose a model for this session')
    expect(bothHint).toHaveTextContent('Change default model')
    expect(bothHint).not.toHaveTextContent(/the first/i)
    expect(bothHint).not.toHaveTextContent(/{{/)
    fireEvent.click(screen.getByTestId('error-card-pick-model'))
    fireEvent.click(screen.getByTestId('error-card-default-model'))
    expect(onPickModel).toHaveBeenCalledTimes(1)
    expect(onOpenDefaultModel).toHaveBeenCalledTimes(1)
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('keeps the prose visible alongside the actions', () => {
    render(<ErrorCard content="no access" onPickModel={() => {}} unentitledElsewhere />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('no access')
    expect(screen.queryByTestId('error-card-default-model')).toBeNull()
    // One action only (embed/popout): no "do both" line to point at a missing button,
    // but the reader is told where the missing affordance lives.
    expect(screen.queryByTestId('error-card-both-hint')).toBeNull()
    // Scoped to the affordance this surface lacks: the settings route, not the picker.
    expect(screen.getByTestId('error-card-elsewhere-hint')).toHaveTextContent(/default model/i)
    expect(screen.getByTestId('error-card-elsewhere-hint')).not.toHaveTextContent(/picker/i)
  })

  it('tells a prose-only surface where the picker and settings live, and stays quiet when both actions render', () => {
    render(<ErrorCard content="no access" unentitledElsewhere />)
    expect(screen.getByTestId('error-card-elsewhere-hint')).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    cleanup()
    render(<ErrorCard content="no access" onPickModel={() => {}} onOpenDefaultModel={() => {}} unentitledElsewhere />)
    expect(screen.queryByTestId('error-card-elsewhere-hint')).toBeNull()
    cleanup()
    // An ordinary (non-entitlement) settled error never gets the line.
    render(<ErrorCard content="⟳ Connection lost — please retry." />)
    expect(screen.queryByTestId('error-card-elsewhere-hint')).toBeNull()
  })

  it('isModelUnentitled reads both the live kind and the rebuilt meta.kind carrier', () => {
    expect(isModelUnentitled({ kind: 'model_unentitled' })).toBe(true)
    expect(isModelUnentitled({ meta: { kind: 'model_unentitled' } })).toBe(true)
    expect(isModelUnentitled({ kind: 'transient_retry' })).toBe(false)
    expect(isModelUnentitled({})).toBe(false)
  })
})

/**
 * A signed-out agent process is the other error whose fix is not a retry. Its
 * row swaps Continue for a deep link to the Kiro sign-in card in Settings.
 */
describe('ErrorCard — agent not signed in', () => {
  it('offers Sign in to Kiro and NO Continue, even when resumable', () => {
    const onContinue = vi.fn()
    const onOpenSignIn = vi.fn()
    render(
      <ErrorCard
        content="Your session has expired. Sign in again, then start a new chat."
        onContinue={onContinue}
        onOpenSignIn={onOpenSignIn}
      />,
    )
    const card = screen.getByTestId('error-card')
    expect(card).toHaveAttribute('data-auth-required', 'true')
    expect(card).toHaveTextContent('Your session has expired.')
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    fireEvent.click(screen.getByTestId('error-card-sign-in'))
    expect(onOpenSignIn).toHaveBeenCalledTimes(1)
    expect(onContinue).not.toHaveBeenCalled()
    cleanup()
  })

  it('recognises the auth_required kind on both the live and the rebuilt carrier', () => {
    expect(isAuthRequired({ kind: 'auth_required' })).toBe(true)
    expect(isAuthRequired({ meta: { kind: 'auth_required' } })).toBe(true)
    expect(isAuthRequired({ kind: 'model_unentitled' })).toBe(false)
    expect(isAuthRequired({})).toBe(false)
  })

  it('falls back to plain prose on a surface with no settings route', () => {
    render(<ErrorCard content="not signed in" />)
    expect(screen.queryByTestId('error-card-sign-in')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-auth-required')
  })
})
