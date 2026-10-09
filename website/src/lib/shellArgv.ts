/**
 * A quote-aware argv splitter for command strings a user types into a plain
 * text input (test/build commands for Review Fix validation).
 *
 * `input.trim().split(/\s+/)` — the previous approach — breaks the instant a
 * command carries a quoted argument with a space in it
 * (`npm test -- --grep "my test"` became `['npm', 'test', '--', '--grep',
 * '"my', 'test"']`, four tokens too many). This is a literal tokenizer, not a
 * shell: it understands single/double quotes and backslash escapes so the
 * user's argv comes out the way a shell would parse it, but it does NOT do
 * shell expansion — no globs, no `$VAR`, no `~`, no subshells. Those stay
 * literal characters, which is the right behavior for a string that is sent
 * to the backend as an argv array and exec'd directly, never re-parsed by a
 * shell.
 */
export interface SplitShellArgvResult {
  argv: string[]
  error?: 'unterminated_quote'
}

export function splitShellArgv(input: string): SplitShellArgvResult {
  const args: string[] = []
  let current = ''
  let hasCurrent = false
  let quote: '"' | "'" | null = null

  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i]
    if (quote) {
      if (ch === quote) {
        quote = null
      } else if (quote === '"' && ch === '\\' && (input[i + 1] === '"' || input[i + 1] === '\\')) {
        // Inside double quotes, backslash only escapes a quote or itself —
        // matching POSIX shell quoting rather than treating every backslash
        // as an escape (which would mangle a Windows-style path).
        current += input[i + 1]
        i += 1
      } else {
        current += ch
      }
      hasCurrent = true
      continue
    }
    if (ch === '"' || ch === "'") {
      quote = ch
      hasCurrent = true
      continue
    }
    if (ch === '\\' && i + 1 < input.length && /[\s"'\\]/.test(input[i + 1])) {
      // Outside quotes, a backslash is only an escape when it precedes
      // whitespace, a quote, or another backslash. Any other backslash
      // (a Windows path separator like `C:\tools\pytest.exe`) is literal —
      // otherwise `C:\tools\pytest.exe` mangled into `C:toolspytest.exe`.
      current += input[i + 1]
      i += 1
      hasCurrent = true
      continue
    }
    if (/\s/.test(ch)) {
      if (hasCurrent) {
        args.push(current)
        current = ''
        hasCurrent = false
      }
      continue
    }
    current += ch
    hasCurrent = true
  }
  if (quote) {
    // An unterminated quote still yields the token gathered so far rather
    // than dropping it, but the caller must not treat this as a clean argv —
    // the shell would refuse to run it too.
    if (hasCurrent) args.push(current)
    return { argv: args, error: 'unterminated_quote' }
  }
  if (hasCurrent) args.push(current)
  return { argv: args }
}
