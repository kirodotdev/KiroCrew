import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('vitest worker memory configuration', () => {
  // Asserts the INVARIANT — a concurrency cap exists and stays small enough that
  // aggregate fork RSS fits a hosted runner — rather than one literal value. The
  // previous form pinned `maxWorkers: 2` exactly, so tuning the cap to the
  // runner's real core count (4 vCPU on ubuntu-latest) failed this test for a
  // change that does not touch the memory budget it is named for. A bound keeps
  // the protection that matters: what would break CI is the cap being REMOVED
  // (one fork per core on a high-core fleet, which is the OOM this guards) or
  // raised far past what the per-fork heap ceiling can cover.
  it('keeps coverage forks within the hosted runner memory budget', () => {
    const config = readFileSync(resolve(process.cwd(), 'vite.config.ts'), 'utf8')

    const workers = config.match(/maxWorkers:\s*(\d+),/)
    expect(workers, 'vite.config.ts must cap maxWorkers').not.toBeNull()
    // Peak single-fork RSS measured at ~1.0-1.4 GB under --coverage, so a
    // 4-worker ceiling keeps the aggregate near 3.5 GB — comfortably inside a
    // hosted runner, with room for the main process.
    expect(Number(workers![1])).toBeGreaterThanOrEqual(1)
    expect(Number(workers![1])).toBeLessThanOrEqual(4)

    expect(config).toMatch(/execArgv:\s*\['--max-old-space-size=3072'\],/)
  })
})
