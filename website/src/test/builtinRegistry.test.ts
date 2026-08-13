import { describe, it, expect } from 'vitest'
import { hasBuiltinComponent, getBuiltinComponent, BUILTIN_COMPONENT_REGISTRY } from '../apps/builtinRegistry'


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

  describe('getBuiltinComponent', () => {
    it('returns a lazy component for registered routes', () => {
      const component = getBuiltinComponent('/channels')
      expect(component).toBeDefined()
      // Lazy components have $$typeof and _payload
      expect(component).toHaveProperty('$$typeof')
    })

    it('returns undefined for unregistered routes', () => {
      expect(getBuiltinComponent('/nonexistent')).toBeUndefined()
      expect(getBuiltinComponent('/chat')).toBeUndefined()
    })
  })

  describe('BUILTIN_COMPONENT_REGISTRY', () => {
    it('contains all expected builtin app routes', () => {
      const expectedRoutes = ['/worlds', '/channels', '/dev-fleet']
      for (const route of expectedRoutes) {
        expect(BUILTIN_COMPONENT_REGISTRY).toHaveProperty(route)
      }
    })

    it('all values are lazy components', () => {
      for (const component of Object.values(BUILTIN_COMPONENT_REGISTRY)) {
        expect(component).toHaveProperty('$$typeof', Symbol.for('react.lazy'))
      }
    })
  })
})
