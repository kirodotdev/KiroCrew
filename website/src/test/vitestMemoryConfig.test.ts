import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('vitest worker memory configuration', () => {
  it('keeps coverage forks within the hosted runner memory budget', () => {
    const config = readFileSync(resolve(process.cwd(), 'vite.config.ts'), 'utf8')

    expect(config).toMatch(/maxWorkers:\s*2,/)
    expect(config).toMatch(/execArgv:\s*\['--max-old-space-size=3072'\],/)
  })
})
