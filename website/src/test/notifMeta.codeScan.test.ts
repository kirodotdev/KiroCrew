import { performance } from 'node:perf_hooks'
import { describe, expect, it } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import { stripMd } from '../components/notifications/notifMeta'

describe('stripMd code-region scan', () => {
  it('keeps an unclosed fence literal through end of input', () => {
    const input = '```sh\n*cache*\n_tmp_'
    const expected = '*cache*\n_tmp_'
    expect(unified().use(remarkParse).parse(input).children[0])
      .toMatchObject({ type: 'code', value: expected })
    expect(stripMd(input)).toBe(expected)
  })

  it('discards an arbitrary fence info string', () => {
    expect(stripMd('```sh title=example\n*cache*\n```')).toBe('*cache*')
  })

  it.each([
    ['empty unclosed fence', '```sh title=example', ''],
    ['three-space indentation', '   ```sh\n*cache*\n   ```', '*cache*'],
    ['four-space indentation is prose', '    ```sh\n*cache*', '```sh cache'],
    ['closing run with a suffix stays literal', '```\n*cache*\n``` trailing\n_tmp_\n```', '*cache*\n``` trailing\n_tmp_'],
    ['four-space closing run stays literal', '```\n*cache*\n    ```\n_tmp_', '*cache*\n    ```\n_tmp_'],
    ['inline spans cannot cross a newline', '`*first*\n_second_`', '`first second`'],
    ['longer runs stay literal inside a single-backtick span', '`*first*``_second_`', '*first*``_second_'],
    ['double-backtick spans keep markers literal', '``rm -rf *cache*``', 'rm -rf *cache*'],
    ['triple-backtick inline span after prose', 'Run ```*cache* _tmp_``` now', 'Run *cache* _tmp_ now'],
    ['different-length runs cannot close a span', '``*first*```_second_``', '*first*```_second_'],
    ['shorter runs stay literal inside a span', '``a `b` c``', 'a `b` c'],
    ['multi-backtick span before a next-line fence', 'Run ``*cache*``\n```sh\n_tmp_\n```\n**after**', 'Run *cache* _tmp_ after'],
    ['unmatched inline opener before a fence', '`*first*\n```\n_second_', '`first _second_'],
    ['fenced inline markers stay literal', '```\n`*first*` _second_\n```\n**after**', '`*first*` _second_ after'],
  ])('%s', (_label, input, expected) => {
    if (expected.includes('\n')) {
      expect(unified().use(remarkParse).parse(input).children[0])
        .toMatchObject({ type: 'code', value: expected })
    }
    expect(stripMd(input)).toBe(expected)
  })

  it('has subquadratic growth for repeated unclosed-fence lines', () => {
    const small = '```a\n'.repeat(500)
    const large = '```a\n'.repeat(4000)
    const measure = (input: string) => {
      const start = performance.now()
      for (let repeat = 0; repeat < 3; repeat++) stripMd(input)
      return performance.now() - start
    }
    // Warm both sizes, alternate measurements, and take medians to reduce JIT
    // and scheduling noise. At 8x input, linear growth is ~8x and quadratic
    // growth is ~64x; 24x allows threefold headroom without a machine-speed cap.
    for (let warmup = 0; warmup < 3; warmup++) {
      stripMd(small)
      stripMd(large)
    }
    const smallTimes: number[] = []
    const largeTimes: number[] = []
    for (let sample = 0; sample < 7; sample++) {
      if (sample % 2) {
        largeTimes.push(measure(large))
        smallTimes.push(measure(small))
      } else {
        smallTimes.push(measure(small))
        largeTimes.push(measure(large))
      }
    }
    const median = (values: number[]) => values.sort((a, b) => a - b)[3]
    expect(median(largeTimes) / median(smallTimes)).toBeLessThan(24)
  })
})
