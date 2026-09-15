/**
 * RestartRequiredBadge — the restart hint must come from the schema, nothing else.
 *
 * The whole point of the flag is that a page cannot claim a restart the backend
 * did not declare, so the tests below pin both directions: a field marked
 * `requiresRestart` shows the pill, and a hot field, an unknown path, and a
 * schema that has not loaded yet all show NOTHING.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { RestartRequiredBadge, SchemaRestartBadge, schemaRequiresRestart } from './RestartRequiredBadge'
import type { SchemaEntry } from './resolveSettingRef'

// The hook is mocked so the component needs no QueryClientProvider. `hookSchema`
// stands in for the fetch result; a test flips it to exercise the loading case.
let hookSchema: Map<string, SchemaEntry> | undefined
let hookError: Error | null = null
vi.mock('./useConfigSchema', () => ({
  useConfigSchema: () => hookSchema,
  useConfigSchemaQuery: () => ({ schema: hookSchema, error: hookError }),
}))

function entry(path: string, requiresRestart?: boolean): SchemaEntry {
  return { path, type: 'string', ...(requiresRestart ? { requiresRestart } : {}) }
}

const SCHEMA = new Map<string, SchemaEntry>([
  ['dashboard.url', entry('dashboard.url', true)],
  ['session.pool_size', entry('session.pool_size')],
])

describe('schemaRequiresRestart', () => {
  it('is true only for an entry the backend flagged', () => {
    expect(schemaRequiresRestart('dashboard.url', SCHEMA)).toBe(true)
    expect(schemaRequiresRestart('session.pool_size', SCHEMA)).toBe(false)
  })

  it('is false for an unknown path and for a schema that has not loaded', () => {
    expect(schemaRequiresRestart('nope.nope', SCHEMA)).toBe(false)
    expect(schemaRequiresRestart('dashboard.url', undefined)).toBe(false)
  })
})

describe('<SchemaRestartBadge>', () => {
  it('renders the pill for a boot-only field', () => {
    const { queryByTestId } = render(<SchemaRestartBadge configKey="dashboard.url" schemaIndex={SCHEMA} />)
    expect(queryByTestId('restart-required-badge')).not.toBeNull()
  })

  it('renders nothing for a field that hot-reloads', () => {
    const { container, queryByTestId } = render(
      <SchemaRestartBadge configKey="session.pool_size" schemaIndex={SCHEMA} />,
    )
    expect(queryByTestId('restart-required-badge')).toBeNull()
    expect(container.innerHTML).toBe('')
  })

  it('renders nothing for a path absent from the schema', () => {
    const { queryByTestId } = render(<SchemaRestartBadge configKey="not.a.field" schemaIndex={SCHEMA} />)
    expect(queryByTestId('restart-required-badge')).toBeNull()
  })

  it('falls back to the hook when no schemaIndex prop is given', () => {
    hookSchema = SCHEMA
    const { queryByTestId } = render(<SchemaRestartBadge configKey="dashboard.url" />)
    expect(queryByTestId('restart-required-badge')).not.toBeNull()
  })

  it('shows no hint while the schema is still loading', () => {
    // A slow fetch must not produce a pessimistic hint: unknown != restart.
    hookSchema = undefined
    const { queryByTestId } = render(<SchemaRestartBadge configKey="dashboard.url" />)
    expect(queryByTestId('restart-required-badge')).toBeNull()
  })
})

describe('<RestartRequiredBadge>', () => {
  it('carries a translated label and an explanatory title', () => {
    const { getByTestId } = render(<RestartRequiredBadge />)
    const el = getByTestId('restart-required-badge')
    // i18nT resolves against the real catalog, so the key must not leak through.
    expect(el.textContent?.trim()).not.toContain('components.restartRequiredBadge')
    expect(el.getAttribute('title')).toBeTruthy()
  })

  it('surfaces a failed schema fetch as an ErrorNotice instead of a silent nothing', () => {
    // A fetch failure is not "hot": the badge cannot answer, and swallowing that
    // into null would strip every boot-only field of its hint with no signal.
    hookSchema = undefined
    hookError = new Error('Config schema fetch failed: 500')
    try {
      const { getByTestId, queryByTestId } = render(<SchemaRestartBadge configKey="dashboard.url" />)
      expect(getByTestId('restart-required-badge-error')).toBeInTheDocument()
      expect(queryByTestId('restart-required-badge')).toBeNull()
    } finally {
      hookError = null
    }
  })

  it('ignores the fetch error when a schemaIndex prop answers the question', () => {
    hookError = new Error('Config schema fetch failed: 500')
    try {
      const { getByTestId, queryByTestId } = render(
        <SchemaRestartBadge configKey="dashboard.url" schemaIndex={SCHEMA} />,
      )
      expect(getByTestId('restart-required-badge')).toBeInTheDocument()
      expect(queryByTestId('restart-required-badge-error')).toBeNull()
    } finally {
      hookError = null
    }
  })
})
