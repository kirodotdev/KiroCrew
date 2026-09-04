import { describe, it, expect } from 'vitest'
import * as React from 'react'
import * as lucideReact from 'lucide-react'

/**
 * The federated-app import map resolves `react`, `@kirocrew/app-sdk` and friends
 * to `/vendor/*.mjs` stubs that read `window.__kirocrew_modules`. Nothing else
 * populates that object, so if this module stops registering (or drops a key) an
 * app bundle gets its OWN React and every hook in it breaks at runtime — with no
 * type error, because the registry is a window property.
 */
describe('app-sdk shared-modules registry', () => {
  it('registers the HOST module instances, not fresh copies', async () => {
    await import('../app-sdk/shared-modules')
    const registry = (window as unknown as {
      __kirocrew_modules: Record<string, Record<string, unknown>>
    }).__kirocrew_modules

    expect(registry).toBeTruthy()
    // Identity, not shape: a second React instance is the failure this exists to
    // prevent, and it would satisfy any structural check.
    expect(registry.react.useState).toBe(React.useState)
    expect(registry.react.createElement).toBe(React.createElement)
    expect(registry['lucide-react'].Search).toBe(lucideReact.Search)
  })

  it('publishes every specifier the vendor stubs import', async () => {
    await import('../app-sdk/shared-modules')
    const registry = (window as unknown as {
      __kirocrew_modules: Record<string, unknown>
    }).__kirocrew_modules

    // A missing key is a bare-specifier import that resolves to undefined inside
    // an app bundle, which fails as "X is not a function" far from the cause.
    expect(Object.keys(registry).sort()).toEqual([
      '@kirocrew/app-sdk',
      '@kirocrew/ui',
      '@tanstack/react-query',
      'lucide-react',
      'react',
      'react-dom',
      'react/jsx-runtime',
    ])
    for (const [key, value] of Object.entries(registry)) {
      expect(value, key).toBeTruthy()
    }
  })
})
