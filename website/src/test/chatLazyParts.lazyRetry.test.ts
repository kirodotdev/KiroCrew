/**
 * The chat page's lazy parts load through `lazyRetry`.
 *
 * `React.lazy()` caches a rejected import, so a part kept on a module-level
 * `lazy()` never loads again after its chunk fails: no Try Again can fetch it,
 * and with no boundary of its own the failure replaces the whole chat page.
 * `lazyRetry` (components/lazyRetry.tsx) shows the failure in the part's own
 * place and imports again on Try Again. This keeps every chat page module off
 * a plain `lazy()`, so a new lazy part there takes the same path.
 */
import { describe, expect, it } from 'vitest'

const SOURCES: Record<string, string> = {
  ...import.meta.glob<string>(
    ['../pages/chat/**/*.{ts,tsx}', '!../pages/chat/**/*.test.{ts,tsx}'],
    { query: '?raw', import: 'default', eager: true },
  ),
  ...import.meta.glob<string>('../components/PreviewTabBody.tsx', { query: '?raw', import: 'default', eager: true }),
}

// Still on a plain lazy(): the phone menu's update item renders inside a Radix
// menu, so its Try Again has to be a menu item, which lazyRetry has no layout for.
const KNOWN_PLAIN = ['../pages/chat/page/MobileTopBar.tsx']

const IMPORTS_LAZY = /import\s*(?:\w+\s*,\s*)?\{[^}]*\blazy\b[^}]*\}\s*from\s*['"]react['"]/
const REACT_DOT_LAZY = /\bReact\.lazy\(/

describe('the chat page lazy parts', () => {
  it('load through lazyRetry, never a plain React.lazy()', () => {
    expect(Object.keys(SOURCES)).toContain('../components/PreviewTabBody.tsx')
    expect(Object.keys(SOURCES)).toContain('../pages/chat/SidePanel.tsx')
    const plain = Object.entries(SOURCES)
      .filter(([, source]) => IMPORTS_LAZY.test(source) || REACT_DOT_LAZY.test(source))
      .map(([path]) => path)
      .sort()
    expect(plain).toEqual(KNOWN_PLAIN)
  })
})
