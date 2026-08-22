import { describe, it, expect } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import DevcontainerExecutionChip from '../components/DevcontainerExecutionChip'
import type { SessionExecution } from '../types'

// Pins the contract that makes the Dev Container feature honest about itself: a
// session that fell back to HOST execution must say so on screen, in words a
// first-time reader understands, and a session with no Dev Container config at
// all must say nothing. The accessible name is asserted as an ATTRIBUTE rather
// than through a role-name query, because `getByRole('button', { name })` also
// matches a `title` and would pass with `aria-label` deleted.

const container = (name: string | null = null): SessionExecution => ({
  mode: 'container',
  container_name: name,
  reason: null,
})

const host = (reason: string | null): SessionExecution => ({
  mode: 'host',
  container_name: null,
  reason,
})

/** The chip's own control. Queried by role only — the name is asserted separately. */
const chip = () => screen.getByRole('button')

/** Open the disclosure and return its panel. */
function openPanel() {
  fireEvent.click(chip())
  return screen.getByRole('dialog')
}

describe('DevcontainerExecutionChip — no config', () => {
  it('renders nothing when execution is absent', () => {
    const { container: root } = renderWithProviders(<DevcontainerExecutionChip />)
    expect(root).toBeEmptyDOMElement()
  })

  it('renders nothing when execution is null', () => {
    // A project with no devcontainer.json has no two worlds to tell apart, so a
    // chip here would invent a distinction rather than report one.
    const { container: root } = renderWithProviders(<DevcontainerExecutionChip execution={null} />)
    expect(root).toBeEmptyDOMElement()
  })

  it('renders nothing for a mode this build cannot interpret', () => {
    const { container: root } = renderWithProviders(
      <DevcontainerExecutionChip
        execution={{ mode: 'sandbox', container_name: null, reason: null } as unknown as SessionExecution}
      />,
    )
    expect(root).toBeEmptyDOMElement()
  })
})

describe('DevcontainerExecutionChip — container', () => {
  it('states that the session runs inside the project container', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={container()} />)
    expect(chip()).toHaveAttribute(
      'aria-label',
      "This session is running inside the project's container.",
    )
    expect(screen.getByText('In container')).toBeInTheDocument()
  })

  it('names the container when the backend supplied one', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={container('kirocrew_devcontainer')} />)
    expect(chip()).toHaveAttribute(
      'aria-label',
      "This session is running inside the project's container kirocrew_devcontainer.",
    )
    // The label carries the name too, so the shelf identifies WHICH container
    // without the user opening anything.
    expect(screen.getByText('kirocrew_devcontainer')).toBeInTheDocument()
  })

  it('explains what running in the container means, and offers no cause', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={container()} />)
    const panel = openPanel()
    expect(panel).toHaveTextContent(
      'Files and commands in this session run inside that container, not directly on your computer.',
    )
    // `reason` is null in the container case, so no fallback sentence belongs here.
    expect(panel).not.toHaveTextContent('could not be used')
  })

  it('drops the label but keeps the accessible name when compact', () => {
    // Below the shelf's breakpoint the label span is not rendered and the glyph
    // is aria-hidden, which would leave the control unnamed if the name came
    // from the span.
    renderWithProviders(<DevcontainerExecutionChip execution={container()} compact />)
    expect(screen.queryByText('In container')).not.toBeInTheDocument()
    expect(chip()).toHaveAttribute(
      'aria-label',
      "This session is running inside the project's container.",
    )
  })
})

describe('DevcontainerExecutionChip — host fallback', () => {
  it('states plainly that the code runs on the user own machine', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={host('build_failed')} />)
    expect(chip()).toHaveAttribute(
      'aria-label',
      "This session is running on your own machine, not inside the project's container.",
    )
    expect(screen.getByText('On your machine')).toBeInTheDocument()
  })

  it('is a standing condition, not an interruption', () => {
    // `status` rather than `alert`: the user should find it when they look, and
    // it must not talk over the turn in progress.
    renderWithProviders(<DevcontainerExecutionChip execution={host('untrusted')} />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says the work lands on the real machine', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={host('untrusted')} />)
    expect(openPanel()).toHaveTextContent(
      'Files and commands in this session run directly on your computer.',
    )
  })

  const REASONS: Array<[string, string]> = [
    ['untrusted', "You have not trusted this project's container setup yet, so it was not started."],
    ['build_failed', "The project's container could not be set up, so it was not used."],
    [
      'docker_unavailable',
      'Docker is not available on this computer, so there was nowhere to start the container.',
    ],
    [
      'config_changed',
      "The project's container setup changed after you trusted it, so it was not started again.",
    ],
    ['unsupported_platform', "This computer cannot run the project's container."],
  ]

  for (const [reason, sentence] of REASONS) {
    it(`explains '${reason}' in plain language, never as the raw token`, () => {
      renderWithProviders(<DevcontainerExecutionChip execution={host(reason)} />)
      const panel = openPanel()
      expect(panel).toHaveTextContent(sentence)
      // The token is a backend identifier. Leaking it is the defect this mapping
      // exists to prevent, so its absence is asserted rather than assumed.
      expect(panel).not.toHaveTextContent(reason)
    })
  }

  it('falls back to generic wording for a cause this build has never seen', () => {
    // The backend owns the vocabulary and can add a cause without a coordinated
    // frontend release. An unmapped token must degrade, not crash and not leak.
    renderWithProviders(<DevcontainerExecutionChip execution={host('quota_exceeded')} />)
    const panel = openPanel()
    expect(panel).toHaveTextContent("The project's container could not be used.")
    expect(panel).not.toHaveTextContent('quota_exceeded')
  })

  it('falls back to generic wording when no cause was supplied at all', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={host(null)} />)
    expect(openPanel()).toHaveTextContent("The project's container could not be used.")
  })
})

describe('DevcontainerExecutionChip — disclosure', () => {
  it('keeps the explanation closed until the chip is activated', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={host('untrusted')} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(chip()).toHaveAttribute('aria-haspopup', 'dialog')
    expect(chip()).toHaveAttribute('aria-expanded', 'false')
    openPanel()
    expect(chip()).toHaveAttribute('aria-expanded', 'true')
  })

  it('labels the panel with its own heading rather than repeating it', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={host('untrusted')} />)
    const panel = openPanel()
    const labelledBy = panel.getAttribute('aria-labelledby') || ''
    expect(labelledBy).not.toBe('')
    expect(document.getElementById(labelledBy)).toHaveTextContent(
      "This session is running on your own machine, not inside the project's container.",
    )
  })

  it('closes on Escape', () => {
    renderWithProviders(<DevcontainerExecutionChip execution={host('untrusted')} />)
    openPanel()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
