import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/**
 * Tests for the resource health indicator in the dashboard capsule.
 *
 * The indicator is inlined in App.tsx's capsule IIFE — we test it by
 * rendering the capsule segment builder logic extracted into a minimal
 * harness that mirrors the imperative segments.push() pattern.
 *
 * Since the indicator lives inside the massive App component, we test
 * the rendering logic via a focused extraction rather than mounting the
 * full App (which requires 40+ mocks).
 */

// Minimal component that mirrors the capsule's resource health logic
function ResourceHealthIndicator({ posture, availableGb, subagentCap, isMobile = false }: {
  posture?: 'ample' | 'tight' | 'critical' | 'unknown'
  availableGb?: number
  subagentCap?: number
  isMobile?: boolean
}) {
  if (!posture || posture === 'ample' || posture === 'unknown') return null

  const tooltipText = posture === 'critical'
    ? `Host memory is critically low (${availableGb?.toFixed(1) ?? '?'} GB free). Avoid heavy parallel work.`
    : `Host memory is tight (${availableGb?.toFixed(1) ?? '?'} GB free). Heavy work may be slower.`

  return (
    <span
      data-testid="resource-health"
      className={`flex items-center gap-1 text-[11px] ${posture === 'critical' ? 'text-danger' : 'text-warn'}`}
      title={tooltipText}
    >
      <span
        aria-hidden="true"
        data-testid="resource-health-dot"
        className={`inline-block w-2 h-2 rounded-full animate-pulse ${posture === 'critical' ? 'bg-danger' : 'bg-warn'}`}
      />
      {!isMobile && (
        <span className="font-medium" data-testid="resource-health-label">
          {posture === 'critical' ? 'Critical' : 'Tight'}
        </span>
      )}
      {!isMobile && subagentCap != null && (
        <span className="text-muted text-[10px]" data-testid="resource-health-cap">
          · cap: {subagentCap}
        </span>
      )}
    </span>
  )
}

describe('ResourceHealthIndicator', () => {
  describe('visibility rules', () => {
    it('renders nothing when posture is ample', () => {
      const { container } = render(
        <ResourceHealthIndicator posture="ample" availableGb={8.2} subagentCap={10} />
      )
      expect(container.firstChild).toBeNull()
    })

    it('renders nothing when posture is unknown', () => {
      const { container } = render(
        <ResourceHealthIndicator posture="unknown" availableGb={0} subagentCap={5} />
      )
      expect(container.firstChild).toBeNull()
    })

    it('renders nothing when posture is undefined', () => {
      const { container } = render(
        <ResourceHealthIndicator posture={undefined} availableGb={4.0} subagentCap={10} />
      )
      expect(container.firstChild).toBeNull()
    })

    it('renders the dot for tight posture', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={8} />)
      expect(screen.getByTestId('resource-health-dot')).toBeTruthy()
    })

    it('renders the dot for critical posture', () => {
      render(<ResourceHealthIndicator posture="critical" availableGb={0.5} subagentCap={3} />)
      expect(screen.getByTestId('resource-health-dot')).toBeTruthy()
    })
  })

  describe('tight posture', () => {
    it('shows yellow/warn styling', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={8} />)
      const indicator = screen.getByTestId('resource-health')
      expect(indicator.className).toContain('text-warn')
      const dot = screen.getByTestId('resource-health-dot')
      expect(dot.className).toContain('bg-warn')
    })

    it('shows "Tight" label on desktop', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={8} isMobile={false} />)
      expect(screen.getByTestId('resource-health-label').textContent).toBe('Tight')
    })

    it('shows tooltip with memory info', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={8} />)
      const indicator = screen.getByTestId('resource-health')
      expect(indicator.getAttribute('title')).toContain('Host memory is tight (2.1 GB free)')
    })

    it('shows subagent cap', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={8} />)
      expect(screen.getByTestId('resource-health-cap').textContent).toContain('cap: 8')
    })
  })

  describe('critical posture', () => {
    it('shows red/danger styling', () => {
      render(<ResourceHealthIndicator posture="critical" availableGb={0.5} subagentCap={3} />)
      const indicator = screen.getByTestId('resource-health')
      expect(indicator.className).toContain('text-danger')
      const dot = screen.getByTestId('resource-health-dot')
      expect(dot.className).toContain('bg-danger')
    })

    it('shows "Critical" label on desktop', () => {
      render(<ResourceHealthIndicator posture="critical" availableGb={0.5} subagentCap={3} isMobile={false} />)
      expect(screen.getByTestId('resource-health-label').textContent).toBe('Critical')
    })

    it('shows tooltip with critical memory info', () => {
      render(<ResourceHealthIndicator posture="critical" availableGb={0.5} subagentCap={3} />)
      const indicator = screen.getByTestId('resource-health')
      expect(indicator.getAttribute('title')).toContain('Host memory is critically low (0.5 GB free)')
    })
  })

  describe('mobile behavior', () => {
    it('hides label text on mobile', () => {
      render(<ResourceHealthIndicator posture="critical" availableGb={0.5} subagentCap={3} isMobile={true} />)
      expect(screen.queryByTestId('resource-health-label')).toBeNull()
    })

    it('hides subagent cap on mobile', () => {
      render(<ResourceHealthIndicator posture="critical" availableGb={0.5} subagentCap={3} isMobile={true} />)
      expect(screen.queryByTestId('resource-health-cap')).toBeNull()
    })

    it('still shows the dot on mobile', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={8} isMobile={true} />)
      expect(screen.getByTestId('resource-health-dot')).toBeTruthy()
    })
  })

  describe('edge cases', () => {
    it('handles undefined availableGb gracefully', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={undefined} subagentCap={8} />)
      const indicator = screen.getByTestId('resource-health')
      expect(indicator.getAttribute('title')).toContain('? GB free')
    })

    it('handles undefined subagentCap — no cap text shown', () => {
      render(<ResourceHealthIndicator posture="tight" availableGb={2.1} subagentCap={undefined} />)
      expect(screen.queryByTestId('resource-health-cap')).toBeNull()
    })
  })
})
