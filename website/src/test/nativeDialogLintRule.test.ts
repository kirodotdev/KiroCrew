/** The renderer lint gate refuses bare, dotted, computed and destructured
 *  spellings of the browser-native dialogs on every global object. */
import { describe, it, expect } from 'vitest'
import { join } from 'path'
import { ESLint } from 'eslint'

const WEBSITE_ROOT = join(__dirname, '..', '..')
// Same budget as hljsCoreOnly: loading the full config is slow on Windows.
const ESLINT_PROBE_TIMEOUT_MS = 120_000

const GLOBALS = ['window', 'globalThis', 'self']
const APIS = ['confirm', 'alert', 'prompt']

const probes = APIS.flatMap(api => [
  `${api}('x')`,
  ...GLOBALS.flatMap(g => [
    `${g}.${api}('x')`,
    `${g}['${api}']('x')`,
    `const { ${api}: ${g}_${api} } = ${g}`,
  ]),
])

describe('native dialog lint rule', () => {
  it('reports every probe as an error', async () => {
    const eslint = new ESLint({ cwd: WEBSITE_ROOT })
    const [result] = await eslint.lintText(`export function probe() {\n${probes.map(p => `  ${p}\n`).join('')}}\n`, {
      filePath: join(WEBSITE_ROOT, 'src', 'nativeDialogProbe.ts'),
    })
    const hits = result.messages.filter(m => m.ruleId === 'no-restricted-globals' || m.ruleId === 'no-restricted-properties')
    expect(hits.map(m => m.line).sort((a, b) => a - b)).toEqual(probes.map((_, i) => i + 2))
    expect(hits.every(m => m.severity === 2)).toBe(true)
  }, ESLINT_PROBE_TIMEOUT_MS)
})
