import React from 'react'
import { readFileSync } from 'node:fs'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'

// Pins the Dev Container chip's contract: it appears in the composer's context
// shelf ONLY while a container is actually up for the active project, it names
// the 12-char container id `docker ps` prints, and it owns the one exit from the
// container — a menu whose single item withdraws trust for this project.

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  project: '/home/u/work/KiroCrew',
}

const FULL_ID = '3f2a1b0c9d8e7f6a5b4c3d2e1f009988'

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

/** The chip's own control, addressed by its accessible name. */
const chipButton = () => screen.getByRole('button', { name: /dev container/i })

/** A ResizeObserver whose callback we can fire with a width of our choosing.
 *  The ambient stub in integration/setup.ts never fires, and happy-dom reports
 *  every contentRect as 0, so the shelf's compact breakpoint cannot be reached
 *  without driving the observer directly. Same harness as McpAppFrame.test.tsx. */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb
    FakeResizeObserver.instances.push(this)
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  fire(width: number) {
    this.cb(
      [{ contentRect: { width } }] as unknown as ResizeObserverEntry[],
      this as unknown as ResizeObserver,
    )
  }
}

/** Report `width` to every observer the render installed, so the shelf
 *  re-measures. Below 340px it drops the chips' label spans (shelfCompact). */
function resizeShelf(width: number) {
  act(() => {
    for (const ro of FakeResizeObserver.instances) ro.fire(width)
  })
}

describe('ChatInput Dev Container chip', () => {
  it('is absent while no container is running', () => {
    renderWithProviders(<ChatInput {...defaultProps} devcontainerId={FULL_ID} />)
    expect(screen.queryByText('Dev Container')).not.toBeInTheDocument()
  })

  it('names the container id truncated to twelve characters in the tooltip', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerId={FULL_ID} />,
    )
    const title = chipButton().getAttribute('title') || ''
    expect(title).toContain('3f2a1b0c9d8e')
    // The full 32-char id would overflow the tooltip and is not what the user
    // pastes into a docker command.
    expect(title).not.toContain(FULL_ID)
  })

  it('describes the PROJECT, not the session, so it cannot contradict the execution chip', () => {
    // A session can be running on the host while a container is up for the
    // project -- the case the execution chip exists for. Session-scoped wording
    // here would state the opposite of the chip beside it.
    renderWithProviders(<ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning />)
    expect(chipButton().getAttribute('title')).toBe(
      'A Dev Container is running for this project',
    )
  })

  it('mounts on trust alone, so withdraw exists before any container is built', () => {
    // The trust card tells the user they can withdraw trust from this chip. A
    // user who grants and immediately reconsiders would otherwise have no
    // control anywhere until a container ran -- the outcome they want to avoid.
    renderWithProviders(<ChatInput {...defaultProps} devcontainerTrusted />)
    const chip = chipButton()
    expect(chip).toBeTruthy()
    expect(chip.getAttribute('title')).toBe(
      "This project's Dev Container setup is trusted. No container is running right now.",
    )
  })

  it('is a menu control, so it is reachable by keyboard', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerId={FULL_ID} />,
    )
    const chip = chipButton()
    expect(chip).toHaveAttribute('aria-haspopup', 'menu')
    expect(chip).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the menu closed until the chip is clicked', () => {
    renderWithProviders(<ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning />)
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument()
    fireEvent.click(chipButton())
    expect(screen.getByRole('menuitem', { name: /withdraw trust/i })).toBeInTheDocument()
    expect(chipButton()).toHaveAttribute('aria-expanded', 'true')
  })

  it('withdraws trust for the status project, then refetches', async () => {
    // The trust key is the status response's realpath `project_dir`, not the
    // `project` label — a revoke against the label could miss the granted entry.
    const untrust = vi.spyOn(api, 'devcontainerUntrust').mockResolvedValue({
      trusted: false,
      removed: true,
    })
    const onDevcontainerUntrust = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        devcontainerTrusted devcontainerRunning
        devcontainerId={FULL_ID}
        devcontainerProject="/real/path/KiroCrew"
        onDevcontainerUntrust={onDevcontainerUntrust}
      />,
    )
    fireEvent.click(chipButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))

    await waitFor(() => expect(untrust).toHaveBeenCalledWith('/real/path/KiroCrew'))
    await waitFor(() => expect(onDevcontainerUntrust).toHaveBeenCalledTimes(1))
    // Menu closes on success, so the chip does not look like it is still armed.
    await waitFor(() => expect(screen.queryByRole('menuitem')).not.toBeInTheDocument())
  })

  it('keeps the menu open and calls no refetch when the revoke fails', async () => {
    const untrust = vi
      .spyOn(api, 'devcontainerUntrust')
      .mockRejectedValue(new Error('gateway down'))
    const onDevcontainerUntrust = vi.fn()
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        devcontainerTrusted devcontainerRunning
        devcontainerProject="/real/path/KiroCrew"
        onDevcontainerUntrust={onDevcontainerUntrust}
      />,
    )
    fireEvent.click(chipButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))

    await waitFor(() => expect(untrust).toHaveBeenCalledTimes(1))
    expect(onDevcontainerUntrust).not.toHaveBeenCalled()
    expect(screen.getByRole('menuitem', { name: /withdraw trust/i })).toBeInTheDocument()
  })

  it('reports why the revoke failed instead of leaving a silent menu', async () => {
    // The menu staying open with no explanation reads as a dead control, and
    // trust is still granted, so the failure has to be visible.
    vi.spyOn(api, 'devcontainerUntrust').mockRejectedValue(new Error('gateway down'))
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        devcontainerTrusted devcontainerRunning
        devcontainerProject="/real/path/KiroCrew"
        onDevcontainerUntrust={vi.fn()}
      />,
    )
    fireEvent.click(chipButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('gateway down')
  })

  it('falls back to a stated failure when the error carries no message', async () => {
    vi.spyOn(api, 'devcontainerUntrust').mockRejectedValue(new Error(''))
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        devcontainerTrusted devcontainerRunning
        devcontainerProject="/real/path/KiroCrew"
        onDevcontainerUntrust={vi.fn()}
      />,
    )
    fireEvent.click(chipButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not withdraw trust')
  })

  it('clears a stale failure when the menu is reopened', async () => {
    vi.spyOn(api, 'devcontainerUntrust').mockRejectedValue(new Error('gateway down'))
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        devcontainerTrusted devcontainerRunning
        devcontainerProject="/real/path/KiroCrew"
        onDevcontainerUntrust={vi.fn()}
      />,
    )
    fireEvent.click(chipButton())
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))
    await screen.findByRole('alert')

    fireEvent.click(chipButton()) // close
    fireEvent.click(chipButton()) // reopen
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says the running session stays in the container, and describes the item with it', () => {
    // Revoking deletes the grant; it does not evict the container this turn runs
    // in. Without the note the unchanged chip reads as a failed withdrawal.
    renderWithProviders(
      <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerProject="/real/path/KiroCrew" />,
    )
    fireEvent.click(chipButton())
    const note = screen.getByText(/this session stays in it until it restarts/i)
    expect(note).toBeInTheDocument()
    expect(note).toHaveTextContent(/withdrawing stops future sessions from using the container/i)
    const describedBy = screen
      .getByRole('menuitem', { name: /withdraw trust/i })
      .getAttribute('aria-describedby')
    expect(describedBy).toBe(note.id)
  })

  describe('Escape closes the menu', () => {
    // Outside-mousedown was the only exit. A keyboard user who opened the menu
    // has no pointer to click away with.
    it('closes on Escape and returns focus to the chip', async () => {
      renderWithProviders(
        <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerProject="/real/path" />,
      )
      const chip = chipButton()
      fireEvent.click(chip)
      expect(screen.getByRole('menuitem', { name: /withdraw trust/i })).toBeInTheDocument()

      fireEvent.keyDown(document, { key: 'Escape' })

      await waitFor(() => expect(screen.queryByRole('menuitem')).not.toBeInTheDocument())
      expect(chipButton()).toHaveAttribute('aria-expanded', 'false')
      // Focus must land somewhere deliberate: the control that opened the menu.
      expect(document.activeElement).toBe(chipButton())
    })

    it('leaves other keys alone', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerProject="/real/path" />,
      )
      fireEvent.click(chipButton())
      fireEvent.keyDown(document, { key: 'Enter' })
      fireEvent.keyDown(document, { key: 'a' })
      expect(screen.getByRole('menuitem', { name: /withdraw trust/i })).toBeInTheDocument()
    })
  })

  it('disables the item with no project to revoke against', () => {
    renderWithProviders(<ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning />)
    fireEvent.click(chipButton())
    expect(screen.getByRole('menuitem', { name: /withdraw trust/i })).toBeDisabled()
  })

  // The chip is an icon + a label span that the compact shelf drops. Its name
  // therefore has to come from aria-label, not from the span, or a screen reader
  // reaches an unnamed menu button on a narrow window.
  describe('accessible name', () => {
    const origRO = globalThis.ResizeObserver

    beforeEach(() => {
      FakeResizeObserver.instances = []
      globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    })
    afterEach(() => {
      globalThis.ResizeObserver = origRO
    })

    it('carries an explicit label rather than relying on the visible span', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerId={FULL_ID} />,
      )
      // Asserted on the attribute as well as the computed name: removing the
      // aria-label would still leave the wide-shelf name intact via the span,
      // so only the attribute pins the compact case's source of truth.
      expect(chipButton()).toHaveAttribute('aria-label', 'Dev Container')
    })

    it('keeps its name once the compact shelf drops the label span', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} devcontainerTrusted devcontainerRunning devcontainerId={FULL_ID} />,
      )
      expect(screen.getByText('Dev Container')).toBeInTheDocument()

      resizeShelf(200)

      // The span is gone and the Box icon is aria-hidden, so the name below can
      // only be coming from aria-label. Matched EXACTLY, not by substring: the
      // accname algorithm falls back to `title`, and the tooltip sentence
      // ("This session runs in a Dev Container…") also contains the phrase — a
      // loose match would pass with the aria-label deleted.
      expect(screen.queryByText('Dev Container')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Dev Container' })).toBeInTheDocument()
    })

    it('drops the sibling chips to icon-only at the same width', () => {
      // Guards the test above from silently passing on a shelf that never went
      // compact: the agent and project chips lose their labels on the same flag.
      renderWithProviders(
        <ChatInput
          {...defaultProps}
          agentName="kirocrew"
          onAgentClick={vi.fn()}
          devcontainerTrusted devcontainerRunning
        />,
      )
      expect(screen.getByText('kirocrew')).toBeInTheDocument()

      resizeShelf(200)

      expect(screen.queryByText('kirocrew')).not.toBeInTheDocument()
      // Still named, by the same aria-label pattern the Dev Container chip uses.
      expect(screen.getByRole('button', { name: /Agent: kirocrew/ })).toBeInTheDocument()
    })
  })
})

describe('ChatInput execution chip', () => {
  // The trust chip above only appears while a container is UP for the project,
  // so a session that degraded to host execution had no surface at all. These
  // pin that the execution chip is wired into the shelf independently of it.

  it('is absent for a project with no Dev Container config', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(screen.queryByText('On your machine')).not.toBeInTheDocument()
    expect(screen.queryByText('In container')).not.toBeInTheDocument()
  })

  it('reports a host fallback even though no container is running', () => {
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        execution={{ mode: 'host', container_name: null, reason: 'docker_unavailable' }}
      />,
    )
    expect(
      screen.getByRole('button', {
        name: "This session is running on your own machine, not inside the project's container.",
      }),
    ).toBeInTheDocument()
  })

  it('sits beside the trust chip when the session is inside the container', () => {
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        devcontainerTrusted devcontainerRunning
        devcontainerProject="/real/path/KiroCrew"
        execution={{ mode: 'container', container_name: 'kirocrew_devcontainer', reason: null }}
      />,
    )
    // Both are present and distinct: the trust chip owns the exit from the
    // container, this one reports where the session landed.
    expect(chipButton()).toHaveAttribute('aria-haspopup', 'menu')
    expect(screen.getByText('kirocrew_devcontainer')).toBeInTheDocument()
  })
})

describe('Dev Container chip placement', () => {
  // Source-level, deliberately: the constraint is about which horizontal group a
  // control is a SIBLING in, and a rendered-DOM assertion cannot express "not in
  // that row" without pinning class names and collapsing the moment the shelf is
  // restyled. The two-action cap counts siblings in one group; the shelf row is
  // exempt from that cap but must not grow, so these chips live in their
  // own region and a future edit must not move them back.
  // Read from the repo root (vitest runs with website/ as cwd) rather than via
  // import.meta.url, which is not a file URL under this config.
  const source = readFileSync('src/components/ChatInput.tsx', 'utf-8')

  function shelfBlock(text: string): string {
    const start = text.indexOf('ref={shelfRef}')
    expect(start).toBeGreaterThan(-1)
    const end = text.indexOf('Dev Container chips sit in their own row', start)
    expect(end).toBeGreaterThan(start)
    return text.slice(start, end)
  }

  it('keeps both chips out of the context shelf row', () => {
    const shelf = shelfBlock(source)
    expect(shelf).not.toContain('devcWrapRef')
    expect(shelf).not.toContain('DevcontainerExecutionChip')
  })

  it('gates the region on the execution chip\'s own predicate', () => {
    // Not a copied condition: a duplicate drifts when a mode is added to the
    // chip, and its failure mode is an empty row, which reads as a render bug.
    expect(source).toContain('showsExecution(execution)')
    const chip = readFileSync('src/components/DevcontainerExecutionChip.tsx', 'utf-8')
    expect(chip).toContain('export function showsExecution')
    // The chip must USE the exported predicate, not merely export it, or the two
    // can still disagree.
    expect(chip).toContain('if (!showsExecution(execution)) return null')
  })
})
