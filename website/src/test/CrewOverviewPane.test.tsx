/**
 * Guards on the overview pane's "Shared" pill.
 *
 * The pill must come from each node's OWN resource. Driving both nodes from one
 * OR-ed value (crews matching workspace OR memory store) passes the common case
 * and fails the inverse: a crew whose memory store alone is shared gets "Shared"
 * on its actually-private workspace, and nothing on the shared store. So the
 * inverse case is the one that carries the weight here.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import CrewOverviewPane from '../components/crew/CrewOverviewPane'

function renderPane(over: Partial<React.ComponentProps<typeof CrewOverviewPane>> = {}) {
  render(
    <CrewOverviewPane
      hub={<span data-testid="hub" />}
      templateLabel="Agent Template"
      template="kirocrew"
      workspace="oncall"
      memoryStore="oncall-mem"
      modelLabel="Inherited"
      modelInherited
      resolvedModel="claude-opus-5"
      activeSchedules={2}
      routingWords={3}
      sharingCrews={1}
      workspaceShared={false}
      memoryShared={false}
      webhookTokens={0}
      {...over}
    />,
  )
}

const isTagged = (key: string) =>
  (screen.getByTestId(`crew-wire-${key}`).textContent || '').includes('Shared')

describe('crew overview pane — the Shared pill follows its own resource', () => {
  it('tags the workspace when the WORKSPACE is shared', () => {
    renderPane({ workspaceShared: true })
    expect(isTagged('workspace')).toBe(true)
    expect(isTagged('memory')).toBe(false)
  })

  it('tags the memory store when the MEMORY STORE is shared', () => {
    // The inverse case: an OR-ed flag would tag the workspace here instead.
    renderPane({ memoryShared: true })
    expect(isTagged('memory')).toBe(true)
    expect(isTagged('workspace')).toBe(false)
  })

  it('tags both when both are shared', () => {
    renderPane({ workspaceShared: true, memoryShared: true })
    expect(isTagged('workspace')).toBe(true)
    expect(isTagged('memory')).toBe(true)
  })

  it('tags neither when the storage is private', () => {
    renderPane()
    expect(isTagged('workspace')).toBe(false)
    expect(isTagged('memory')).toBe(false)
  })
})

describe('crew overview pane — facts the three explanation boxes used to carry', () => {
  it('reports an unreadable schedule count as unknown, never as zero', () => {
    renderPane({ schedulesUnknown: true })
    // Absence of an answer and an answer of none are different claims.
    expect(screen.getByTestId('crew-wire-schedules').textContent).not.toContain('0')
  })

  it('shows the resolved model, which the crew itself only inherits', () => {
    renderPane()
    expect(screen.getByText('claude-opus-5')).toBeInTheDocument()
    expect(screen.getByTestId('crew-wire-model').textContent).toContain('Inherited')
  })
})

describe('crew overview pane — the webhook node reports the binding fact', () => {
  // The testid element itself carries the ghost treatment classes.
  const nodeBox = () => screen.getByTestId('crew-wire-webhook')

  it('stays a dashed ghost while nothing is bound', () => {
    renderPane({ webhookTokens: 0 })
    const node = screen.getByTestId('crew-wire-webhook')
    expect(node.textContent).toContain('No tokens bound')
    expect(nodeBox().className).toContain('border-dashed')
  })

  it('turns solid and counts once a token names this crew', () => {
    // The ghost treatment claims "a real input with no crew binding"; once a
    // binding exists that claim is false and the node must stop making it.
    renderPane({ webhookTokens: 2 })
    const node = screen.getByTestId('crew-wire-webhook')
    expect(node.textContent).toContain('2')
    expect(node.textContent).not.toContain('No tokens bound')
    expect(nodeBox().className).not.toContain('border-dashed')
  })

  it('reports an unreadable webhook store as unknown, still ghosted', () => {
    // A store that cannot be read is not evidence a binding exists.
    renderPane({ webhookTokens: 0, webhooksUnknown: true })
    const node = screen.getByTestId('crew-wire-webhook')
    expect(node.textContent).not.toContain('0')
    expect(nodeBox().className).toContain('border-dashed')
  })
})
