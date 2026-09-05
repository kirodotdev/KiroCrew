import { describe, it, expect } from 'vitest'
import {
  runInTerminalText,
  shellForFenceLang,
  shellBaseName,
  posixSingleQuote,
} from '../utils/fenceShell'

/**
 * A fence tag redirects a snippet only when the running shell genuinely cannot
 * read it, and only ever to an absolute path the gateway resolved. Everything
 * else comes through byte-identical, because the snippets that work today are
 * the ones with the most to lose.
 */

const FOUND = {
  bash: '/usr/bin/bash',
  sh: '/usr/bin/sh',
  zsh: '/usr/bin/zsh',
  fish: '/usr/bin/fish',
}

describe('shellForFenceLang', () => {
  it('maps the four tags that name a shell binary', () => {
    expect(shellForFenceLang('bash')).toBe('bash')
    expect(shellForFenceLang('sh')).toBe('sh')
    expect(shellForFenceLang('zsh')).toBe('zsh')
    expect(shellForFenceLang('fish')).toBe('fish')
  })

  it('names no shell for the generic transcript tags', () => {
    expect(shellForFenceLang('shell')).toBeUndefined()
    expect(shellForFenceLang('console')).toBeUndefined()
    expect(shellForFenceLang('terminal')).toBeUndefined()
  })

  it('names no shell for an absent tag', () => {
    expect(shellForFenceLang(undefined)).toBeUndefined()
    expect(shellForFenceLang('')).toBeUndefined()
  })
})

describe('shellBaseName', () => {
  it('reduces a posix path to the program name', () => {
    expect(shellBaseName('/usr/bin/zsh')).toBe('zsh')
    expect(shellBaseName('/bin/bash')).toBe('bash')
  })

  it('reduces a windows path and drops the suffix', () => {
    expect(shellBaseName('C:\\Windows\\System32\\powershell.exe')).toBe('powershell')
  })
})

describe('posixSingleQuote', () => {
  it('closes, escapes and reopens an embedded quote so nothing escapes the quoting', () => {
    expect(posixSingleQuote("it's")).toBe("'it'\\''s'")
  })

  it('leaves shell metacharacters inert inside the quotes', () => {
    expect(posixSingleQuote('rm -rf $HOME; echo `id`')).toBe("'rm -rf $HOME; echo `id`'")
  })
})

describe('runInTerminalText across the fish boundary', () => {
  it('runs a fish fence through fish when the host is posix', () => {
    expect(runInTerminalText('set greeting hello', 'fish', '/usr/bin/bash', FOUND))
      .toBe("'/usr/bin/fish' --no-config -c 'set greeting hello'")
  })

  it('does not delegate to bash, whose -c sources BASH_ENV before the snippet', () => {
    // Measured: `BASH_ENV=f bash -c 'echo x'` runs f first, so a delegated line
    // would execute startup code the user never saw in the dialog. No bash flag
    // suppresses it, so this direction is not shipped at all.
    expect(runInTerminalText('export A=1', 'bash', '/usr/bin/fish', FOUND))
      .toBe('export A=1')
    expect(runInTerminalText('greeting=hello', 'sh', '/usr/bin/fish', FOUND))
      .toBe('greeting=hello')
  })

  it('names the shell by absolute path, never by bare name', () => {
    const text = runInTerminalText('set greeting hello', 'fish', '/usr/bin/bash', FOUND)
    expect(text.startsWith("'/usr/bin/fish'")).toBe(true)
    expect(text.startsWith('fish ')).toBe(false)
  })

  it('refuses the handoff when the gateway reported no path for that shell', () => {
    // fish absent from the map: invoking a bare name here is exactly the
    // project-PATH hijack the absolute-path rule exists to prevent.
    const withoutFish = { bash: '/usr/bin/bash', sh: '/usr/bin/sh', zsh: '/usr/bin/zsh' }
    expect(runInTerminalText('set greeting hello', 'fish', '/usr/bin/bash', withoutFish))
      .toBe('set greeting hello')
  })

  it('refuses the handoff when no map was reported at all', () => {
    expect(runInTerminalText('set greeting hello', 'fish', '/usr/bin/bash'))
      .toBe('set greeting hello')
  })

  it('quotes a snippet containing a single quote safely', () => {
    expect(runInTerminalText("echo 'hi'", 'fish', '/bin/bash', FOUND))
      .toBe("'/usr/bin/fish' --no-config -c 'echo '\\''hi'\\'''")
  })
})

describe('runInTerminalText leaves compatible and unknown cases alone', () => {
  it('does not wrap a bash fence on a zsh host, so cd and export still persist', () => {
    // The macOS default: _resolve_shell prefers $SHELL, and bash is the tag
    // agents emit most. Wrapping here would silently drop the cd.
    expect(runInTerminalText('cd proj && export A=1', 'bash', '/usr/bin/zsh', FOUND))
      .toBe('cd proj && export A=1')
  })

  it('does not wrap an sh fence on a bash host, since bash reads posix sh', () => {
    expect(runInTerminalText('greeting=hello', 'sh', '/bin/bash', FOUND))
      .toBe('greeting=hello')
  })

  it('does not wrap a zsh fence on a bash host', () => {
    expect(runInTerminalText('print -r -- x', 'zsh', '/bin/bash', FOUND))
      .toBe('print -r -- x')
  })

  it('does not wrap when the fence and the host are the same shell', () => {
    expect(runInTerminalText('set greeting hello', 'fish', '/usr/bin/fish', FOUND))
      .toBe('set greeting hello')
  })

  it('preserves a backslash-bearing snippet exactly when it delegates', () => {
    // The host is POSIX here, so POSIX quoting is correct and the backslash must
    // survive verbatim inside the quoted argument.
    const snippet = "string match '\\d'"
    expect(runInTerminalText(snippet, 'fish', '/bin/bash', FOUND))
      .toBe("'/usr/bin/fish' --no-config -c 'string match '\\''\\d'\\'''")
  })

  it('leaves a generic tag untouched', () => {
    expect(runInTerminalText('ls -la', 'console', '/usr/bin/fish', FOUND)).toBe('ls -la')
    expect(runInTerminalText('ls -la', 'shell', '/usr/bin/bash', FOUND)).toBe('ls -la')
  })

  it('does not guess when the launched shell is unknown', () => {
    expect(runInTerminalText('set greeting hello', 'fish', undefined, FOUND))
      .toBe('set greeting hello')
    expect(runInTerminalText('set greeting hello', 'fish', '', FOUND))
      .toBe('set greeting hello')
  })

  it('does not quote for a host whose escaping differs', () => {
    expect(runInTerminalText('set greeting hello', 'fish', 'C:\\Windows\\powershell.exe', FOUND))
      .toBe('set greeting hello')
  })
})
