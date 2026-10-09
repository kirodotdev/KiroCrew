import { describe, it, expect } from 'vitest'
import { splitShellArgv } from './shellArgv'

describe('splitShellArgv', () => {
  it.each([
    ['splits plain whitespace-separated words', 'npm run build', ['npm', 'run', 'build']],
    ['collapses repeated whitespace and trims the ends', '  pytest   -q  ', ['pytest', '-q']],
    ['keeps a double-quoted argument with spaces as one token', 'npm test -- --grep "my test name"',
      ['npm', 'test', '--', '--grep', 'my test name']],
    ['keeps a single-quoted argument with spaces as one token', "pytest -k 'test one two'",
      ['pytest', '-k', 'test one two']],
    ['unescapes a backslash-escaped space outside quotes', 'make target\\ with\\ spaces',
      ['make', 'target with spaces']],
    ['unescapes a backslash-escaped quote inside double quotes', 'echo "say \\"hi\\""', ['echo', 'say "hi"']],
    ['treats a lone backslash inside single quotes as a literal character', "echo 'C:\\path'", ['echo', 'C:\\path']],
    // A backslash outside quotes that precedes neither whitespace, a quote, nor
    // another backslash is literal — a Windows path is not an escape sequence.
    ['keeps an unescaped Windows path backslash literal', 'C:\\tools\\pytest.exe', ['C:\\tools\\pytest.exe']],
    ['does not expand globs, variables, or tilde', 'echo $HOME ~/bin *.ts', ['echo', '$HOME', '~/bin', '*.ts']],
    ['joins adjacent quoted and unquoted segments into one token', '--name="my value"', ['--name=my value']],
    ['returns an empty array for an empty string', '', []],
    ['returns an empty array for a whitespace-only string', '   ', []],
    ['recovers the token gathered so far from an unterminated quote', 'pytest -k "still typing',
      ['pytest', '-k', 'still typing']],
  ])('%s', (_name, input, expected) => {
    expect(splitShellArgv(input).argv).toEqual(expected)
  })

  it('reports no error for a cleanly terminated command', () => {
    expect(splitShellArgv('pytest -q').error).toBeUndefined()
  })

  it('reports an unterminated_quote error when a quote is never closed', () => {
    expect(splitShellArgv('pytest -k "still typing').error).toBe('unterminated_quote')
  })
})
