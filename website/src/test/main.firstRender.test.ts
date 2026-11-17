import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The app's first render runs as a transition, so React renders it in slices
 * instead of one long task. `main.tsx` boots the app as it is imported, so it
 * cannot be loaded here; like the other pins on that file, this one reads its
 * source.
 */
describe('main.tsx first render', () => {
  const main = readFileSync(resolve(__dirname, '../main.tsx'), 'utf8')
  const boot = main.slice(main.indexOf('function boot('))

  it('hands the first render to React as a transition', () => {
    expect(boot).toMatch(/startTransition\(\(\) => \{\s*root\.render\(<AppMotionConfig>\{rootTree\}<\/AppMotionConfig>\)\s*\}\)/)
  })

  it('renders the root nowhere else, so no second, synchronous first render exists', () => {
    expect(boot.match(/\.render\(/g)).toHaveLength(1)
  })
})
