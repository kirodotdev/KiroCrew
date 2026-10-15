import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query'
import type { ReactElement } from 'react'

// The copy control's outcome is decided by the clipboard helper; stub it so
// the tests pin what the card paints for each answer.
const clipboard = vi.hoisted(() => ({ copyCode: vi.fn<(text: string) => Promise<boolean>>() }))
vi.mock('../utils/clipboard', async (importOriginal) => ({ ...(await importOriginal<object>()), ...clipboard }))

import { ApiError } from '../api/apiError'
import { ErrorCard, capabilityRefusal, isAuthRequired, isContextOverflow, isKiroCliMissing, isModelUnentitled, isSessionStartFailed, isTurnTimeout, isUsageLimit, retryProse, sessionStartFailureStreak } from '../pages/chat/ErrorCard'
import type { ChatMessage } from '../types'
import { FEATURE_REQUEST_FORM_URL } from '../prompts/featureRequest'
import { i18nT } from '../i18n/t'

const setupMeta = (member = 'reviewer') => ({
  code: 'memory_unavailable',
  recovery: { kind: 'initialize_member_memory', member },
})

/**
 * The error row used to be an actionless div whose own copy told the reader to
 * retry. These tests pin the two shapes: settled (no action) and resumable
 * (Resume), plus the guard that a press cannot double-fire.
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
    // The wire text stands: with no Resume control there is nothing for a
    // "resume to pick up where it stopped" instruction to point at.
    expect(screen.getByTestId('error-card')).toHaveTextContent('⟳ Connection lost — please retry.')
    // Deliberately ABSENT rather than disabled: a permanently greyed button on a
    // red card reads as a broken feature.
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
  })

  it('renders a Resume action when the turn is resumable', () => {
    render(<ErrorCard content="boom" onContinue={() => {}} />)
    expect(screen.getByTestId('error-card-continue')).toBeTruthy()
    // Interrupted-turn recovery is a Resume action — the visible label must
    // read "Resume", not "Continue" (regression pin for the Resume/Continue
    // naming rule).
    expect(screen.getByTestId('error-card-continue')).toHaveTextContent('Resume')
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
    expect(screen.getByTestId('error-card')).toHaveTextContent('Session busy — resume to pick up where it stopped.')
  })

  /**
   * The gateway's error rows say "please retry"; the button beside them says
   * "Resume". One action, one verb: a known gateway row is swapped for catalog
   * copy that names Resume, and NEVER says "retry" next to that button.
   */
  describe('retryProse — gateway wording is re-spoken with the Resume verb', () => {
    it('localises every known gateway retry row', () => {
      expect(retryProse('⟳ Connection lost — please retry.')).toBe('Connection lost — resume to pick up where it stopped.')
      expect(retryProse('⟳ Session busy — please retry.')).toBe('Session busy — resume to pick up where it stopped.')
      expect(retryProse('⟳ Turn stalled — please retry.')).toBe('Turn stalled — resume to pick up where it stopped.')
      expect(retryProse('⟳ Tool appeared stalled — please retry.')).toBe('Tool appeared stalled — resume to pick up where it stopped.')
      expect(retryProse('⟳ Backend hiccup — please retry.')).toBe('The agent hit a brief problem — resume to pick up where it stopped.')
    })

    it('keeps the exit-code detail a connection-lost row carries', () => {
      expect(retryProse('⟳ Connection lost (exit 1) — please retry.')).toBe('Connection lost (exit 1) — resume to pick up where it stopped.')
      expect(retryProse('⟳ Connection lost (exit -9) — please retry.')).toBe('Connection lost (exit -9) — resume to pick up where it stopped.')
    })

    it('leaves anything else verbatim — an unknown or newer gateway string must still reach the screen', () => {
      expect(retryProse('⟳ Connection lost — please retry')).toBeNull()      // no full stop: not the wire shape
      expect(retryProse('Connection lost — please retry.')).toBeNull()        // no glyph
      expect(retryProse('⟳ Something new — please retry.')).toBeNull()
      expect(retryProse('boom')).toBeNull()
      render(<ErrorCard content="⟳ Something new — please retry." />)
      expect(screen.getByTestId('error-card')).toHaveTextContent('⟳ Something new — please retry.')
    })

    it('never renders "retry" beside the Resume button', () => {
      render(<ErrorCard content="⟳ Connection lost — please retry." onContinue={() => {}} />)
      const card = screen.getByTestId('error-card')
      expect(card).toHaveTextContent('Resume')
      expect(card).not.toHaveTextContent(/retry/i)
    })

    it('keeps the wire text on a row with no Resume control — an instruction to resume must have a button to point at', () => {
      render(<ErrorCard content="⟳ Session busy — please retry." />)
      const card = screen.getByTestId('error-card')
      expect(card).toHaveTextContent('⟳ Session busy — please retry.')
      expect(card).not.toHaveTextContent(/resume/i)
      expect(screen.queryByTestId('error-card-continue')).toBeNull()
    })
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
describe('ErrorCard — member agent file changed', () => {
  const content = 'materialization_changed: This crew member\'s agent file changed outside the Capabilities page.'
  const meta = { code: 'materialization_changed', member: 'reviewer' }

  it('offers Open Customize and NO Resume, with plain copy instead of the code', () => {
    const onOpenCapabilities = vi.fn()
    render(<ErrorCard content={content} meta={meta} onContinue={() => {}} onOpenCapabilities={onOpenCapabilities} />)
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    expect(screen.getByTestId('error-card')).toHaveTextContent(i18nT('pages.chat.errorCard.capabilities_changed'))
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('materialization_changed')
    fireEvent.click(screen.getByTestId('error-card-open-capabilities'))
    expect(onOpenCapabilities).toHaveBeenCalledTimes(1)
  })

  it('hides the code on a surface with no crew editor', () => {
    render(<ErrorCard content={content} meta={meta} />)
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('materialization_changed:')
    expect(screen.queryByTestId('error-card-open-capabilities')).toBeNull()
  })

  it('recognises the row only by its structural code', () => {
    expect(capabilityRefusal({ meta })).toBeDefined()
    expect(capabilityRefusal({ meta: undefined })).toBeUndefined()
    expect(capabilityRefusal({ meta: { code: 'memory_unavailable' } })).toBeUndefined()
    expect(capabilityRefusal({ meta: { code: 'toString' } })).toBeUndefined()
  })
})

describe('ErrorCard — sibling capability refusals', () => {
  const vars = { file: 'reviewer-private.json', project: '/work/reviewer-project', name: 'reviewer', field: i18nT('pages.kiroCrewAgentsPage.built_from') }
  const cases = [
    { code: 'parent_identity_changed', copy: 'capabilities_parent_changed', button: 'open_crewmate' },
    { code: 'foreign_private_copy', copy: 'capabilities_foreign_copy', button: 'open_crewmate' },
    { code: 'project_identity_changed', copy: 'capabilities_project_changed', button: null },
    { code: 'private_template_shadowed', copy: 'capabilities_template_shadowed', button: null },
  ] as const

  for (const { code, copy, button } of cases) {
    it(`${code}: readable copy, its own fix, and NO Resume`, () => {
      const onOpenCapabilities = vi.fn()
      const meta = { code, member: 'reviewer', file: 'reviewer-private.json', project: '/work/reviewer-project' }
      render(<ErrorCard content={`${code}: English text`} meta={meta} onContinue={() => {}} onOpenCapabilities={button ? onOpenCapabilities : undefined} />)
      const card = screen.getByTestId('error-card')
      expect(card).toHaveTextContent(i18nT(`pages.chat.errorCard.${copy}`, vars))
      expect(card).not.toHaveTextContent(code)
      expect(card).not.toHaveTextContent('{{')
      expect(screen.queryByTestId('error-card-continue')).toBeNull()
      if (button) {
        expect(screen.getByTestId('error-card-open-capabilities')).toHaveTextContent(i18nT(`pages.chat.errorCard.${button}`, { name: 'reviewer' }))
        fireEvent.click(screen.getByTestId('error-card-open-capabilities'))
        expect(onOpenCapabilities).toHaveBeenCalledTimes(1)
      } else {
        expect(screen.queryByTestId('error-card-open-capabilities')).toBeNull()
      }
      cleanup()
    })
  }

  it('names the mate on its editor button and the project to go to', () => {
    render(<ErrorCard content="foreign_private_copy: x" meta={{ code: 'foreign_private_copy', member: 'reviewer' }} onOpenCapabilities={() => {}} />)
    expect(screen.getByTestId('error-card-open-capabilities')).toHaveTextContent('reviewer')
    cleanup()
    render(<ErrorCard content="project_identity_changed: x" meta={{ code: 'project_identity_changed', member: 'reviewer', project: '/work/reviewer-project' }} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('/work/reviewer-project')
  })

  it('names the shadowing file and the project that holds it in the copy', () => {
    render(<ErrorCard content="private_template_shadowed: x" meta={{ code: 'private_template_shadowed', member: 'reviewer', file: 'reviewer-private.json', project: '/work/reviewer-project' }} />)
    const card = screen.getByTestId('error-card')
    expect(card).toHaveTextContent('reviewer-private.json')
    expect(card).toHaveTextContent('/work/reviewer-project')
  })

  it('says another project when the member saved with no project to name', () => {
    render(<ErrorCard content="project_identity_changed: x" meta={{ code: 'project_identity_changed', member: 'reviewer' }} />)
    const card = screen.getByTestId('error-card')
    expect(card).toHaveTextContent(i18nT('pages.chat.errorCard.capabilities_project_changed_other'))
    expect(card).not.toHaveTextContent(i18nT('pages.chat.errorCard.capabilities_project_changed', { project: '' }))
  })

  it('names no empty mate when the foreign-copy row carries no member', () => {
    render(<ErrorCard content="foreign_private_copy: x" meta={{ code: 'foreign_private_copy', member: '' }} />)
    const card = screen.getByTestId('error-card')
    const field = i18nT('pages.kiroCrewAgentsPage.built_from')
    expect(card).toHaveTextContent(i18nT('pages.chat.errorCard.capabilities_foreign_copy_unnamed', { field }))
    expect(card).not.toHaveTextContent(i18nT('pages.chat.errorCard.capabilities_foreign_copy', { name: '', field }))
  })
})

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

describe('ErrorCard — conversation too big for the model', () => {
  const TOO_LONG = 'Input is too long: this conversation does not fit in one request to the model.'

  it('offers Start a fresh session and NO Resume, with the hint saying what carries over', () => {
    const onContinue = vi.fn()
    const onFreshSession = vi.fn()
    render(<ErrorCard content={TOO_LONG} onContinue={onContinue} onFreshSession={onFreshSession} />)
    const card = screen.getByTestId('error-card')
    expect(card).toHaveAttribute('data-context-overflow', 'true')
    expect(card).toHaveTextContent('Input is too long')
    expect(screen.getByTestId('error-card-fresh-session-hint')).toHaveTextContent(i18nT('pages.chat.errorCard.fresh_session_hint'))
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    fireEvent.click(screen.getByTestId('error-card-fresh-session'))
    expect(onFreshSession).toHaveBeenCalledTimes(1)
    expect(onContinue).not.toHaveBeenCalled()
    cleanup()
  })

  it('disables the press while it runs, so it cannot double-fire', () => {
    const onFreshSession = vi.fn()
    render(<ErrorCard content={TOO_LONG} onFreshSession={onFreshSession} freshSession="pending" />)
    const btn = screen.getByTestId('error-card-fresh-session')
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(onFreshSession).not.toHaveBeenCalled()
    cleanup()
  })

  it('says to type the next message once the fresh session is ready, drops the button, the stale instruction and the danger colours', () => {
    render(<ErrorCard content={TOO_LONG} onFreshSession={() => {}} freshSession="done" />)
    expect(screen.queryByTestId('error-card-fresh-session')).toBeNull()
    // Recovered: the card drops the failure's colours.
    expect(screen.getByTestId('error-card').className).not.toMatch(/bg-danger-subtle/)
    // The sentence that asks for a fresh session is gone once one exists.
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('Input is too long')
    expect(screen.getByTestId('error-card-fresh-session-hint')).toHaveTextContent(i18nT('pages.chat.errorCard.fresh_session_done'))
    cleanup()
  })

  it('recognises the context_overflow kind on both carriers', () => {
    expect(isContextOverflow({ kind: 'context_overflow' })).toBe(true)
    expect(isContextOverflow({ meta: { kind: 'context_overflow' } })).toBe(true)
    expect(isContextOverflow({ kind: 'usage_limit' })).toBe(false)
    expect(isContextOverflow({})).toBe(false)
  })
})

/**
 * A feature request refused for a spent plan allowance (#13342). The header's
 * "Request a Feature" is an agent turn by design, so at the usage limit it used
 * to dead-end on this very row. When the host knows the row belongs to that
 * flow it hands the card the non-inference route -- the repo's feature-request
 * form -- and the card offers it INSTEAD of Resume, which would only replay the
 * rejection. The backend's own sentence (which limit, request id) stays.
 */
describe('ErrorCard — feature request refused for a usage limit', () => {
  const prose = '❌ The monthly usage limit has been reached. Retrying will not help until the limit resets. (request_id: be06fbe8)'

  it('offers the feature-request form with a one-line explanation and NO Continue, even when resumable', () => {
    const onContinue = vi.fn()
    render(<ErrorCard content={prose} onContinue={onContinue} featureRequestFormUrl={FEATURE_REQUEST_FORM_URL} />)
    const card = screen.getByTestId('error-card')
    expect(card).toHaveAttribute('data-usage-limit-fallback', 'true')
    // The backend's sentence is still the first thing on the card.
    expect(card).toHaveTextContent('The monthly usage limit has been reached.')
    expect(card).toHaveTextContent(i18nT('pages.chat.errorCard.feature_request_form_hint'))
    const link = screen.getByTestId('error-card-feature-request-form')
    expect(link.tagName).toBe('A')
    expect(link).toHaveAttribute('href', FEATURE_REQUEST_FORM_URL)
    // A new tab, opened without a handle back to this window.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toMatch(/noopener/)
    expect(link.getAttribute('rel')).toMatch(/noreferrer/)
    expect(link).toHaveAccessibleName(i18nT('pages.chat.errorCard.feature_request_form'))
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    expect(onContinue).not.toHaveBeenCalled()
    cleanup()
  })

  it('lets the action wrap inside the card instead of running past its edge: the label is long by design (destination and cost; 62 chars in de), so the control may shrink and balance its lines', () => {
    render(<ErrorCard content={prose} onContinue={() => undefined} featureRequestFormUrl={FEATURE_REQUEST_FORM_URL} />)
    const link = screen.getByTestId('error-card-feature-request-form')
    const classes = link.className.split(/\s+/)
    // `shrink-0` on a flex item in the wrapping action row pins it at its full
    // one-line width, so a card narrower than the label is overflowed, not
    // wrapped. Without it the control shrinks to its longest word and the
    // label breaks into lines.
    expect(classes).not.toContain('shrink-0')
    expect(classes).not.toContain('truncate')
    expect(classes).not.toContain('whitespace-nowrap')
    // Balanced lines: two similar halves, not one long line and one word.
    expect(classes).toContain('text-balance')
    // The icon beside the label keeps its size when the control shrinks.
    expect(link.querySelector('svg')?.getAttribute('class') ?? '').toMatch(/\bshrink-0\b/)
    cleanup()
  })

  it('says kiro-cli is missing, not that usage ran out, on a kiro-cli refusal', () => {
    const missing = '❌ kiro-cli not found (searched 2 directories: /a, /b). Install it from https://kiro.dev/cli/, or point KIROCREW_KIRO_BIN at the binary.'
    render(<ErrorCard content={missing} onContinue={() => undefined} featureRequestFormUrl={FEATURE_REQUEST_FORM_URL} kiroCliMissing />)
    const card = screen.getByTestId('error-card')
    expect(card).toHaveTextContent('kiro-cli not found')
    expect(screen.getByTestId('error-card-feature-request-hint')).toHaveTextContent(i18nT('pages.chat.errorCard.feature_request_form_cli_missing_hint'))
    expect(card).not.toHaveTextContent(i18nT('pages.chat.errorCard.feature_request_form_hint'))
    const link = screen.getByTestId('error-card-feature-request-form')
    expect(link).toHaveAttribute('href', FEATURE_REQUEST_FORM_URL)
    expect(link).toHaveAccessibleName(i18nT('pages.chat.errorCard.feature_request_form_cli_missing'))
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    cleanup()
  })

  it('recognises the kiro_cli_missing kind on both the live and the rebuilt carrier', () => {
    expect(isKiroCliMissing({ kind: 'kiro_cli_missing' })).toBe(true)
    expect(isKiroCliMissing({ meta: { kind: 'kiro_cli_missing' } })).toBe(true)
    expect(isKiroCliMissing({ kind: 'usage_limit' })).toBe(false)
    expect(isKiroCliMissing({})).toBe(false)
  })

  it('points at the repo\'s feature_request issue template, not a blank issue', () => {
    const url = new URL(FEATURE_REQUEST_FORM_URL)
    expect(url.origin + url.pathname).toBe('https://github.com/kirodotdev/KiroCrew/issues/new')
    expect(url.searchParams.get('template')).toBe('feature_request.yml')
  })

  it('recognises the usage_limit kind on both the live and the rebuilt carrier', () => {
    expect(isUsageLimit({ kind: 'usage_limit' })).toBe(true)
    expect(isUsageLimit({ meta: { kind: 'usage_limit' } })).toBe(true)
    expect(isUsageLimit({ kind: 'auth_required' })).toBe(false)
    expect(isUsageLimit({})).toBe(false)
  })

  it('renders the ordinary row when no form route is handed to it', () => {
    // The same prose in a slot the pill did not create: today's card, Resume
    // and all. A usage limit outside the feature-request flow has no
    // non-inference route to offer.
    render(<ErrorCard content={prose} onContinue={() => undefined} />)
    expect(screen.queryByTestId('error-card-feature-request-form')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-usage-limit-fallback')
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
  })
})

describe('ErrorCard — a session start that failed twice in a row', () => {
  const prose = 'Request session/new timed out after 90s (4/4 session-injected MCP server(s) reported)'

  it('shows the restart remedy, naming the command, and NO Resume', () => {
    render(<ErrorCard content={prose} meta={{ kind: 'session_start_failed' }} sessionStartRepeat />)
    const card = screen.getByTestId('error-card')
    // The backend's own sentence stays first -- it carries the diagnostic.
    expect(card).toHaveTextContent(prose)
    const hint = screen.getByTestId('error-card-session-start-repeat-hint')
    expect(hint).toHaveTextContent(i18nT('pages.chat.errorCard.session_start_repeat_hint', { command: 'kirocrew restart' }))
    // The command is the one thing to copy, so it is a code chip at body weight,
    // not a muted footnote (the only remaining next step must not be the least
    // visible thing on the card).
    expect(screen.getByTestId('error-card-restart-command')).toHaveTextContent('kirocrew restart')
    expect(screen.getByTestId('error-card-restart-command').tagName).toBe('CODE')
    expect(hint.className).not.toMatch(/text-muted/)
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    expect(card).not.toHaveAttribute('data-continuable')
  })

  it('keeps the first failure as today: Resume, no hint', () => {
    render(<ErrorCard content={prose} meta={{ kind: 'session_start_failed' }} onContinue={() => undefined} />)
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
    expect(screen.queryByTestId('error-card-session-start-repeat-hint')).toBeNull()
    expect(screen.queryByTestId('error-card-restart-gateway')).toBeNull()
  })

  describe('the restart command chip can be copied (#13791)', () => {
    afterEach(() => { clipboard.copyCode.mockReset() })
    const copyButton = () => within(screen.getByTestId('error-card-restart-command-copy')).getByRole('button')

    it('uses the shared copy control and copies the exact command', async () => {
      clipboard.copyCode.mockResolvedValue(true)
      render(<ErrorCard content={prose} meta={{ kind: 'session_start_failed' }} sessionStartRepeat />)
      expect(copyButton()).toHaveAccessibleName(i18nT('components.settingRef.copyCommand'))
      fireEvent.click(copyButton())
      await waitFor(() => expect(copyButton()).toHaveAccessibleName(i18nT('components.settingRef.copied')))
      expect(clipboard.copyCode).toHaveBeenCalledWith('kirocrew restart')
    })

    it('reports a dead clipboard instead of painting a tick', async () => {
      clipboard.copyCode.mockResolvedValue(false)
      render(<ErrorCard content={prose} meta={{ kind: 'session_start_failed' }} sessionStartRepeat />)
      fireEvent.click(copyButton())
      await waitFor(() => expect(copyButton()).toHaveAccessibleName(i18nT('components.settingRef.copyFailed')))
    })
  })

  describe('the in-card restart action (#13791)', () => {
    afterEach(() => { onlineManager.setOnline(true) })
    const withClient = (ui: ReactElement) =>
      render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>{ui}</QueryClientProvider>)
    const renderRepeat = (onRestartGateway: () => Promise<unknown>) =>
      withClient(<ErrorCard content={prose} meta={{ kind: 'session_start_failed' }} sessionStartRepeat onRestartGateway={onRestartGateway} />)
    const button = () => screen.getByTestId('error-card-restart-gateway')
    // The shared RestartGatewayButton arms on the first press and fires on the second.
    const armAndConfirm = () => { fireEvent.click(button()); fireEvent.click(button()) }

    it('offers the shared restart button in place of the terminal hint, keeping the diagnostic and withholding Resume', () => {
      renderRepeat(() => Promise.resolve())
      const card = screen.getByTestId('error-card')
      expect(card).toHaveTextContent(prose)
      expect(screen.getByTestId('error-card-session-start-restart-hint')).toHaveTextContent(i18nT('pages.chat.errorCard.session_start_repeat_restart_hint'))
      expect(button()).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_gateway'))
      expect(screen.queryByTestId('error-card-session-start-repeat-hint')).toBeNull()
      expect(screen.queryByTestId('error-card-continue')).toBeNull()
    })

    it('the first press only arms, because a restart stops every running chat', () => {
      const restart = vi.fn(() => Promise.resolve())
      renderRepeat(restart)
      fireEvent.click(button())
      expect(button()).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_confirm'))
      expect(restart).not.toHaveBeenCalled()
    })

    it('restarts once on the second press and then says the page reconnects, with no second button', async () => {
      const restart = vi.fn(() => Promise.resolve({ ok: true, status: 'restarting' }))
      renderRepeat(restart)
      armAndConfirm()
      expect(await screen.findByTestId('error-card-restarting')).toHaveTextContent(i18nT('pages.chat.errorCard.restart_gateway_restarting'))
      expect(restart).toHaveBeenCalledTimes(1)
      expect(screen.queryByTestId('error-card-restart-gateway')).toBeNull()
      // The failure being fixed stops reading as an alarm above the restarting line.
      expect(screen.getByTestId('error-card-session-start-prose')).toHaveClass('text-muted')
      expect(screen.getByTestId('error-card-session-start-prose')).not.toHaveClass('text-danger')
    })

    it('a request the gateway never answered is a failure, not a restart: the notice, the terminal hint and the button stay', async () => {
      // `/api/restart` replies before it restarts, so only its 200 means the
      // restart was accepted; a rejected fetch never reached it.
      renderRepeat(() => Promise.reject(new TypeError('Failed to fetch')))
      armAndConfirm()
      const notice = await screen.findByTestId('error-card-restart-failed')
      expect(notice).toHaveTextContent(i18nT('pages.chat.errorCard.restart_gateway_failed'))
      // With no server sentence the title is said once, not as title and message.
      expect(notice.textContent?.split(i18nT('pages.chat.errorCard.restart_gateway_failed')).length).toBe(2)
      expect(screen.queryByTestId('error-card-restarting')).toBeNull()
      // One next step beside the button that can still work, never the plain
      // terminal hint (which would read as "the button is pointless").
      expect(screen.getByTestId('error-card-restart-retry-hint')).toHaveTextContent(i18nT('pages.chat.errorCard.restart_gateway_retry_hint', { command: 'kirocrew restart', button: i18nT('pages.settings.aboutPanel.restart_gateway') }))
      // The retry is the same disruptive restart, so its cost line stays in view.
      expect(screen.getByTestId('error-card-session-start-restart-hint')).toHaveTextContent(i18nT('pages.chat.errorCard.session_start_repeat_restart_hint'))
      expect(screen.queryByTestId('error-card-session-start-repeat-hint')).toBeNull()
      // A transport failure can clear on its own, so the retry stays offered.
      expect(button()).toBeEnabled()
    })

    it('the endpoint refusing a restart that cannot run (409) shows its reason and the terminal, and drops the button a retry cannot pass', async () => {
      // `/api/restart` checks the launcher and interpreter BEFORE its 200, so
      // this refusal arrives on the request itself, never as a later push.
      const sentence = 'Cannot restart: invalid gateway launcher path'
      renderRepeat(() => Promise.reject(new ApiError(409, sentence, JSON.stringify({ ok: false, error: sentence, code: 'restart_launcher_invalid' }))))
      armAndConfirm()
      const notice = await screen.findByTestId('error-card-restart-failed')
      // The plain-words title leads; the server's sentence follows as detail.
      expect(notice).toHaveTextContent(i18nT('pages.chat.errorCard.restart_gateway_failed'))
      expect(notice).toHaveTextContent('Cannot restart: invalid gateway launcher path')
      expect(screen.queryByTestId('error-card-restarting')).toBeNull()
      // Pressing again would meet the same refusal: the terminal is the one next step.
      expect(screen.getByTestId('error-card-session-start-repeat-hint')).toHaveTextContent('kirocrew restart')
      expect(screen.queryByTestId('error-card-restart-retry-hint')).toBeNull()
      expect(screen.queryByTestId('error-card-restart-refused-hint')).toBeNull()
      expect(screen.queryByTestId('error-card-restart-gateway')).toBeNull()
    })

    it('a 409 without a cannot-run code keeps the retry: only those codes mean a second press must fail', async () => {
      renderRepeat(() => Promise.reject(new ApiError(409, 'Busy', JSON.stringify({ error: 'Busy', code: 'something_else' }))))
      armAndConfirm()
      await screen.findByTestId('error-card-restart-failed')
      expect(screen.getByTestId('error-card-restart-retry-hint')).toBeInTheDocument()
      expect(button()).toBeEnabled()
    })

    it('a lapsed-cookie 403 (authRequired) keeps the button: the client refreshes it, so the next press can pass', async () => {
      renderRepeat(() => Promise.reject(new ApiError(403, 'Sign in again', '', true)))
      armAndConfirm()
      await screen.findByTestId('error-card-restart-failed')
      expect(button()).toBeEnabled()
      expect(screen.queryByTestId('error-card-restart-refused-hint')).toBeNull()
    })

    it('while offline it waits instead of claiming a restart it never sent', async () => {
      onlineManager.setOnline(false)
      const restart = vi.fn(() => Promise.resolve())
      const { unmount } = renderRepeat(restart)
      armAndConfirm()
      await waitFor(() => expect(button()).toBeDisabled())
      expect(restart).not.toHaveBeenCalled()
      expect(screen.queryByTestId('error-card-restarting')).toBeNull()
      // Unmount before going back online, so the resumed mutation settles
      // against no mounted card rather than outside act().
      unmount()
    })

    it('a 403 refusal shows through ErrorNotice, tells the viewer who can restart, and drops the button it can never pass', async () => {
      renderRepeat(() => Promise.reject(new ApiError(403, 'Only the owner can restart the gateway.')))
      armAndConfirm()
      const notice = await screen.findByTestId('error-card-restart-failed')
      // Says outright that THIS account cannot, with the server's sentence below.
      expect(notice).toHaveTextContent(i18nT('pages.chat.errorCard.restart_gateway_refused_title'))
      expect(notice).toHaveTextContent('Only the owner can restart the gateway.')
      expect(notice).toHaveAttribute('role', 'alert')
      // One instruction, addressed to whoever runs the gateway: the command
      // is theirs to run, so the viewer is not ALSO told to run it.
      const refused = screen.getByTestId('error-card-restart-refused-hint')
      expect(refused).toHaveTextContent(i18nT('pages.chat.errorCard.restart_gateway_refused_hint', { command: 'kirocrew restart' }))
      expect(within(refused).getByTestId('error-card-restart-command')).toHaveTextContent('kirocrew restart')
      expect(screen.queryByTestId('error-card-session-start-repeat-hint')).toBeNull()
      expect(screen.queryByTestId('error-card-session-start-restart-hint')).toBeNull()
      expect(screen.queryByTestId('error-card-restart-gateway')).toBeNull()
      expect(screen.queryByTestId('error-card-restarting')).toBeNull()
      // No Ask-the-agent: that chat would need the same failing session start.
      expect(within(notice).queryByRole('button')).toBeNull()
    })

    it('another server error keeps the retry', async () => {
      renderRepeat(() => Promise.reject(new ApiError(500, 'Restart failed')))
      armAndConfirm()
      expect(await screen.findByTestId('error-card-restart-failed')).toHaveTextContent('Restart failed')
      expect(button()).toBeEnabled()
    })
  })
})

describe('sessionStartFailureStreak mirrors the server scan', () => {
  const row = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
    ({ role, content: '', ts: 0, ...over }) as ChatMessage
  const failed = () => row('error', { content: 'Request session/new timed out after 90s', meta: { kind: 'session_start_failed' } })
  const resumed = () => row('inject', { content: '[Continue — requested by the user] …', meta: { injectKind: 'recovery' } })

  it('counts consecutive tagged failures across Resume rows', () => {
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed()])).toBe(1)
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), resumed(), failed()])).toBe(2)
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), resumed(), failed(), resumed(), failed()])).toBe(3)
  })

  it('a typed message or an assistant reply starts the count over', () => {
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), resumed(), failed(), row('user', { content: 'again' }), failed()])).toBe(1)
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), row('assistant', { content: 'ok' })])).toBe(0)
  })

  it('a row that opens a turn of its own ends the streak; the Resume row does not', () => {
    const cron = row('inject', { content: '[Cron notification from "job"] …', meta: { injectKind: 'cron' } })
    const synthesis = row('inject', { content: 'synthesis', meta: { injectKind: 'synthesis' } })
    const nudge = row('nudge', { content: '[auto-nudge cycle 2]' })
    const sub = row('subagent', { content: '[Subagent completion event] …' })
    for (const opener of [cron, synthesis, nudge, sub]) {
      expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), opener, failed()])).toBe(1)
    }
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), resumed(), failed()])).toBe(2)
  })

  it('an error of another kind, or a Stop card, ends the streak', () => {
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), row('error', { content: '⟳ Connection lost — please retry.' })])).toBe(0)
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), row('error', { content: 'x' }), failed()])).toBe(1)
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), row('system', { kind: 'stop_event' }), failed()])).toBe(1)
  })

  it('walks past a system notice and reads both kind carriers', () => {
    const notice = row('assistant', { content: 'Auto-compacted.', meta: { kind: 'compaction' } })
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), failed(), notice, failed()])).toBe(2)
    const rebuilt = row('error', { content: 'timed out', kind: 'session_start_failed' })
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), rebuilt, resumed(), rebuilt])).toBe(2)
    expect(isSessionStartFailed(rebuilt)).toBe(true)
    expect(isSessionStartFailed(failed())).toBe(true)
    expect(isSessionStartFailed(row('error', { content: 'timed out' }))).toBe(false)
  })

  it('never counts an untagged timeout -- the kind decides, not the prose', () => {
    const untagged = () => row('error', { content: 'Request session/new timed out after 90s' })
    expect(sessionStartFailureStreak([row('user', { content: 'hi' }), untagged(), resumed(), untagged()])).toBe(0)
  })
})

/**
 * A turn stopped at its time limit. Resume still finishes this turn; the
 * settings button is how the next long turn avoids the cut, so the card offers
 * both rather than one instead of the other.
 */
describe('ErrorCard — turn hit its time limit', () => {
  it('offers Resume and the turn-limit setting side by side', () => {
    const onContinue = vi.fn()
    const onOpenTurnLimit = vi.fn()
    render(<ErrorCard content="⏱️ This turn hit the 4-hour limit." onContinue={onContinue} onOpenTurnLimit={onOpenTurnLimit} />)
    const card = screen.getByTestId('error-card')
    expect(card).toHaveAttribute('data-turn-timeout', 'true')
    expect(card).toHaveTextContent('This turn hit the 4-hour limit.')
    const limit = screen.getByTestId('error-card-turn-limit')
    expect(limit).toHaveTextContent(i18nT('pages.chat.errorCard.turn_limit'))
    fireEvent.click(limit)
    expect(onOpenTurnLimit).toHaveBeenCalledTimes(1)
    expect(onContinue).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('error-card-continue'))
    expect(onContinue).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('still links the setting on a settled row with no Resume', () => {
    render(<ErrorCard content="⏱️ limit" onOpenTurnLimit={() => {}} />)
    expect(screen.getByTestId('error-card-turn-limit')).toBeVisible()
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    cleanup()
  })

  it('recognises the turn_timeout kind on both the live and the rebuilt carrier', () => {
    expect(isTurnTimeout({ kind: 'turn_timeout' })).toBe(true)
    expect(isTurnTimeout({ meta: { kind: 'turn_timeout' } })).toBe(true)
    expect(isTurnTimeout({ kind: 'auth_required' })).toBe(false)
    expect(isTurnTimeout({})).toBe(false)
  })

  it('renders no settings button on a surface with no settings route', () => {
    render(<ErrorCard content="⏱️ limit" onContinue={() => {}} />)
    expect(screen.queryByTestId('error-card-turn-limit')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-turn-timeout')
    cleanup()
  })
})
