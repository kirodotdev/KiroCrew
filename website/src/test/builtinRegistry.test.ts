import { describe, it, expect } from 'vitest'
import { hasBuiltinComponent, getBuiltinApp, BUILTIN_COMPONENT_REGISTRY } from '../apps/builtinRegistry'


describe('builtinRegistry', () => {
  describe('hasBuiltinComponent', () => {
    it('returns true for registered routes', () => {
      expect(hasBuiltinComponent('/worlds')).toBe(true)
      expect(hasBuiltinComponent('/channels')).toBe(true)
    })

    it('returns false for unregistered routes', () => {
      expect(hasBuiltinComponent('/chat')).toBe(false)
      expect(hasBuiltinComponent('/nonexistent')).toBe(false)
      expect(hasBuiltinComponent('/apps')).toBe(false)
      expect(hasBuiltinComponent('')).toBe(false)
    })
  })

  describe('getBuiltinApp', () => {
    it('returns the component and owning app for registered routes', () => {
      const entry = getBuiltinApp('/channels')
      expect(entry).toBeDefined()
      // Lazy components have $$typeof and _payload
      expect(entry!.component).toHaveProperty('$$typeof')
      expect(entry!.appId).toBe('channels')
    })

    it('returns undefined for unregistered routes', () => {
      expect(getBuiltinApp('/nonexistent')).toBeUndefined()
      expect(getBuiltinApp('/chat')).toBeUndefined()
    })
  })

  describe('BUILTIN_COMPONENT_REGISTRY', () => {
    it('contains all expected builtin app routes', () => {
      const expectedRoutes = ['/worlds', '/channels', '/dev-fleet']
      for (const route of expectedRoutes) {
        expect(BUILTIN_COMPONENT_REGISTRY).toHaveProperty(route)
      }
    })

    it('all values carry a lazy component and an appId', () => {
      for (const entry of Object.values(BUILTIN_COMPONENT_REGISTRY)) {
        expect(entry.component).toHaveProperty('$$typeof', Symbol.for('react.lazy'))
        expect(typeof entry.appId).toBe('string')
      }
    })
  })
})
