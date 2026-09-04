import { describe, it, expect } from 'vitest'
import {
  extractToken, commandStart, commandWord, shouldComplete, foldersOnly, atWordEnd,
  extendsWord,
  isPlainWord, isSafeName, shellEscape, commonPrefix, buildInsertion, acceptSuffix,
  unescapeWord,
  completionMode, commandArgv, commandSuffix, isCommandToken,
} from '../utils/terminalCompletion'

/* The prompt used across these cases mirrors a real oh-my-zsh (af-magic) line:
 * a path, a git branch, then a `»` terminator. The heuristic must find the
 * command after the LAST terminator, not inside the prompt text. */
const PROMPT = '~/work/KiroCrew (feat/x) » '

describe('extractToken', () => {
  it('returns the word ending at the cursor', () => {
    const line = `${PROMPT}cd ../Kiro`
    expect(extractToken(line, line.length)).toEqual({ token: '../Kiro', start: PROMPT.length + 3 })
  })

  it('returns an empty token when the cursor follows a space', () => {
    // The row is space-padded (translateToString(false)), so a trailing space
    // must read as "new word", not as the previous one.
    const line = `${PROMPT}ls     `
    expect(extractToken(line, PROMPT.length + 3)).toEqual({ token: '', start: PROMPT.length + 3 })
  })

  it('clamps a cursor past the end of the line', () => {
    expect(extractToken('cd x', 99)).toEqual({ token: 'x', start: 3 })
  })

  it('handles a cursor at column zero', () => {
    expect(extractToken('', 0)).toEqual({ token: '', start: 0 })
  })

  // Regression: this feature's own escaping puts `my\ dir/` on screen, so a
  // tokeniser that stops at the escaped space would then complete `dir/` against
  // the cwd — the walk into the directory dead-ends.
  it('keeps an escaped space inside the word', () => {
    const line = `${PROMPT}cd my\\ dir/`
    expect(extractToken(line, line.length))
      .toEqual({ token: 'my\\ dir/', start: PROMPT.length + 3 })
  })

  it('breaks at a space whose backslash is itself escaped', () => {
    // `cd my\\ dir` — the backslash is a literal, so the space really breaks.
    const line = `${PROMPT}cd my\\\\ dir`
    expect(extractToken(line, line.length)).toEqual({ token: 'dir', start: line.length - 3 })
  })
})

describe('unescapeWord', () => {
  it('decodes backslash escapes to the literal filesystem text', () => {
    expect(unescapeWord('my\\ dir/')).toBe('my dir/')
    expect(unescapeWord('a\\(b\\)')).toBe('a(b)')
    expect(unescapeWord('a\\\\b')).toBe('a\\b')
  })

  it('leaves an unescaped word untouched', () => {
    expect(unescapeWord('src/comp')).toBe('src/comp')
  })

  it('round-trips whatever shellEscape produced', () => {
    for (const name of ['my dir', 'a;b', '$x`y`', 'a\\b', '(a)[b]*?']) {
      expect(unescapeWord(shellEscape(name))).toBe(name)
    }
  })
})

describe('commandStart', () => {
  it('prefers an OSC prompt marker when one is available', () => {
    const line = `${PROMPT}cd ../`
    expect(commandStart(line, line.length, PROMPT.length)).toBe(PROMPT.length)
  })

  it('ignores a marker that sits after the token', () => {
    const line = `${PROMPT}cd ../`
    expect(commandStart(line, PROMPT.length + 3, 999)).toBe(PROMPT.length)
  })

  it('falls back to the last prompt terminator', () => {
    const line = `${PROMPT}cd ../`
    expect(commandStart(line, line.length)).toBe(PROMPT.length)
  })

  it('returns 0 when there is no recognisable prompt', () => {
    expect(commandStart('cd ../', 3)).toBe(0)
  })
})

describe('commandWord', () => {
  const cmd = (after: string, tokenOffset = after.length) =>
    commandWord(PROMPT + after, PROMPT.length + tokenOffset)

  it('finds the command before the token', () => {
    expect(cmd('cd ')).toBe('cd')
  })

  it('is unaffected by words inside the prompt', () => {
    expect(cmd('ls ./')).toBe('ls')
  })

  it('uses the last command of a pipeline', () => {
    expect(cmd('cat a.txt | grep -n foo ')).toBe('grep')
  })

  it('uses the last command after &&', () => {
    expect(cmd('npm run build && cd ')).toBe('cd')
  })

  it('steps past wrappers', () => {
    expect(cmd('sudo rm ')).toBe('rm')
    expect(cmd('env FOO=1 ls ')).toBe('ls')
  })

  it('is empty while the command name itself is being typed', () => {
    expect(commandWord(`${PROMPT}cd`, PROMPT.length)).toBe('')
  })
})

describe('shouldComplete', () => {
  it('triggers on a path-shaped token whatever the command', () => {
    expect(shouldComplete('../Kiro', 'unknowncmd')).toBe(true)
    expect(shouldComplete('./', 'unknowncmd')).toBe(true)
    expect(shouldComplete('~/w', 'unknowncmd')).toBe(true)
    expect(shouldComplete('src/comp', 'unknowncmd')).toBe(true)
  })

  it('triggers on a bare token for a known path command', () => {
    expect(shouldComplete('', 'ls')).toBe(true)
    expect(shouldComplete('Kir', 'cd')).toBe(true)
  })

  it('stays out of the way for non-path commands', () => {
    expect(shouldComplete('', 'echo')).toBe(false)
    expect(shouldComplete('hello', 'echo')).toBe(false)
  })

  it('never triggers on flags or shell expansions', () => {
    expect(shouldComplete('-l', 'ls')).toBe(false)
    expect(shouldComplete('--color', 'ls')).toBe(false)
    expect(shouldComplete('$HOME', 'ls')).toBe(false)
    expect(shouldComplete('`pwd', 'ls')).toBe(false)
  })

  it('never triggers while the command name is still being typed', () => {
    expect(shouldComplete('cd', '')).toBe(false)
  })
})

describe('foldersOnly', () => {
  it('narrows to directories for directory-only commands', () => {
    expect(foldersOnly('cd')).toBe(true)
    expect(foldersOnly('mkdir')).toBe(true)
  })

  it('keeps files for everything else', () => {
    expect(foldersOnly('ls')).toBe(false)
    expect(foldersOnly('unknowncmd')).toBe(false)
  })
})

describe('atWordEnd', () => {
  it('accepts a cursor at the end of the line', () => {
    expect(atWordEnd('cd docs', 7)).toBe(true)
  })

  it('accepts a cursor followed by whitespace', () => {
    // The screen row is space-padded, so this is the ordinary "typing" case.
    expect(atWordEnd('cd docs   ', 7)).toBe(true)
  })

  it('rejects a cursor inside a word', () => {
    // `cd do⎸cs` — completing here would leave the `cs` behind.
    expect(atWordEnd('cd docs', 5)).toBe(false)
  })
})

describe('isPlainWord', () => {
  const plain = (after: string) => {
    const line = PROMPT + after
    const { token, start } = extractToken(line, line.length)
    return isPlainWord(line, PROMPT.length, start, token)
  }

  it('accepts an ordinary word', () => {
    expect(plain('cd ../Kiro')).toBe(true)
    expect(plain('cd ')).toBe(true)
  })

  it('rejects a word inside quotes', () => {
    // `D` is really the tail of `My D`; completing it would target the cwd.
    expect(plain('cd "My D')).toBe(false)
    expect(plain("cd 'My D")).toBe(false)
  })

  it('accepts a backslash-escaped word', () => {
    // `cd My\ D` is ONE word; the escaping is understood, not refused.
    expect(plain('cd My\\ D')).toBe(true)
    expect(plain('cd my\\ dir/')).toBe(true)
  })

  it('accepts a word after an escaped backslash', () => {
    // `cd My\\ ` — the backslash is itself escaped, so the space really breaks.
    expect(plain('cd My\\\\ D')).toBe(true)
  })

  it('rejects a word ending in an unfinished escape', () => {
    // The escaped character has not been typed yet, so there is no word to look up.
    expect(plain('cd my\\')).toBe(false)
    expect(plain('cd my\\\\\\')).toBe(false)
  })

  it('rejects a word containing a quote', () => {
    expect(isPlainWord('cd a"b', 0, 3, 'a"b')).toBe(false)
    expect(isPlainWord("cd a'b", 0, 3, "a'b")).toBe(false)
  })

  it('rejects a token that is only the tail of an escaped word', () => {
    // `extractToken` does not produce this, but the backstop must hold if a
    // caller hands in a start position that splits an escaped break.
    expect(isPlainWord('cd My\\ D', 0, 7, 'D')).toBe(false)
  })

  it('ignores a quote inside the prompt', () => {
    // A prompt like `~/it's » ` must not disable completion for the whole line.
    const line = "~/it's » cd sr"
    expect(isPlainWord(line, 9, 12, 'sr')).toBe(true)
  })
})

describe('isSafeName', () => {
  it('accepts ordinary names, including non-ASCII', () => {
    expect(isSafeName('my file.txt')).toBe(true)
    expect(isSafeName('项目')).toBe(true)
  })

  it('rejects a name carrying a control character', () => {
    // `touch $'x\nrm -rf y'` is legal on disk; typed verbatim the newline would
    // submit the rest as a command.
    expect(isSafeName('x\nrm -rf y')).toBe(false)
    expect(isSafeName('x\rreset')).toBe(false)
    expect(isSafeName('x\x1b[2J')).toBe(false)
  })
})

describe('isSafeName — lone surrogates', () => {
  // The server decodes an undecodable filename byte with surrogateescape, JSON
  // carries the lone surrogate, and TextEncoder then turns it into U+FFFD — so
  // accepting such a name would type a path that does not exist on disk.
  it('rejects a lone high surrogate', () => {
    expect(isSafeName('bad\uD800name')).toBe(false)
  })

  it('rejects a lone low surrogate', () => {
    expect(isSafeName('bad\uDC00name')).toBe(false)
  })

  it('accepts a well-formed supplementary character', () => {
    // A valid pair is one supplementary code point, not a surrogate, so ordinary
    // emoji and rare-CJK filenames must still complete.
    expect(isSafeName('report\u{1F4C1}')).toBe(true)
    expect(isSafeName('\u{20BB7}.txt')).toBe(true)
  })
})

describe('shellEscape', () => {
  it('leaves a safe name untouched', () => {
    expect(shellEscape('KiroCrew-terminal_v2.1')).toBe('KiroCrew-terminal_v2.1')
  })

  it('leaves letters and digits of any script untouched', () => {
    expect(shellEscape('项目-2')).toBe('项目-2')
  })

  it('neutralises every metacharacter that could change the command', () => {
    expect(shellEscape('a b')).toBe('a\\ b')
    expect(shellEscape('a;rm -rf b')).toBe('a\\;rm\\ -rf\\ b')
    expect(shellEscape('$x`y`')).toBe('\\$x\\`y\\`')
    expect(shellEscape('a&b|c')).toBe('a\\&b\\|c')
    expect(shellEscape('(a)[b]*?')).toBe('\\(a\\)\\[b\\]\\*\\?')
    expect(shellEscape('a"b\'c')).toBe('a\\"b\\\'c')
    expect(shellEscape('a\\b')).toBe('a\\\\b')
  })

  it('produces one shell word, so nothing needs closing', () => {
    // Backslashes rather than quotes: the result never opens a quoted region,
    // so appending `/` and re-triggering still works.
    expect(shellEscape('my dir')).not.toContain('"')
  })
})

describe('commonPrefix', () => {
  it('returns the shared leading text', () => {
    expect(commonPrefix(['KiroCrew', 'KiroCli'])).toBe('KiroC')
    expect(commonPrefix(['docs', 'doctor', 'dock'])).toBe('doc')
  })

  it('returns the whole name for a single entry', () => {
    expect(commonPrefix(['KiroCrew'])).toBe('KiroCrew')
  })

  it('returns empty when nothing is shared', () => {
    expect(commonPrefix(['abc', 'xyz'])).toBe('')
  })

  it('returns empty for no entries', () => {
    expect(commonPrefix([])).toBe('')
  })

  // A UTF-16 comparison stops between the surrogate halves of `𝐀`/`𝐁` and would
  // hand a lone surrogate to the PTY, which receives it as U+FFFD.
  it('never splits a surrogate pair', () => {
    expect(commonPrefix(['a\u{1D400}x', 'a\u{1D401}y'])).toBe('a')
    expect(commonPrefix(['a\u{1D400}x', 'a\u{1D400}y'])).toBe('a\u{1D400}')
  })
})

describe('buildInsertion — option-shaped names', () => {
  // `vim -c:!sh evil` runs a command: `-c` is an OPTION to the program, and no
  // escaping changes that (`\-c` is still `-c`), so the path has to be made
  // explicit instead.
  it('prefixes ./ so an option-shaped name stays a path', () => {
    expect(buildInsertion('', '-c:!sh evil', ' '))
      .toEqual({ erase: 0, text: './-c:\\!sh\\ evil ' })
  })

  it('retypes the word when the ./ guard displaces what was typed', () => {
    // A substring match: `evil` selected `-c:!sh evil`, so the typed text is not
    // a prefix of the guarded insertion.
    expect(buildInsertion('evil', '-c:!sh evil', ' '))
      .toEqual({ erase: 4, text: './-c:\\!sh\\ evil ' })
  })

  it('guards Tab\'s common prefix too', () => {
    // Same choke point, no suffix: `-ab` is as dangerous partial as whole.
    expect(buildInsertion('', '-ab')).toEqual({ erase: 0, text: './-ab' })
  })

  it('leaves a name inside a directory alone', () => {
    // `sub/-x` cannot parse as an option, so no guard is needed and none is added.
    expect(buildInsertion('sub/', '-x', ' ')).toEqual({ erase: 0, text: '-x ' })
  })

  it('does not re-guard a word that already starts with ./', () => {
    expect(buildInsertion('./', '-x', ' ')).toEqual({ erase: 0, text: '-x ' })
  })

  // `+` is vim's startup-command prefix, so `vim +:!id` executes `id` — the same
  // failure class as `-`, reached through a different character.
  it('guards a leading plus as well', () => {
    expect(buildInsertion('', '+:!id', ' ')).toEqual({ erase: 0, text: './+:\\!id ' })
  })

  it('guards a leading plus on Tab\'s common prefix', () => {
    expect(buildInsertion('', '+cmd')).toEqual({ erase: 0, text: './+cmd' })
  })

  it('leaves a plus-named file inside a directory alone', () => {
    expect(buildInsertion('sub/', '+x', ' ')).toEqual({ erase: 0, text: '+x ' })
  })

  it('does not guard a plus that is not leading', () => {
    expect(buildInsertion('', 'a+b', ' ')).toEqual({ erase: 0, text: 'a+b ' })
  })

  // `rsync`/`scp` read `host:path` as a REMOTE target, so a local file called
  // `attacker.example:drop` would make `rsync secret.txt <that>` upload the file.
  it('guards a colon anywhere in a bare name', () => {
    expect(buildInsertion('', 'attacker.example:drop', ' '))
      .toEqual({ erase: 0, text: './attacker.example:drop ' })
  })

  it('guards a colon on Tab\'s common prefix', () => {
    expect(buildInsertion('', 'host:pa')).toEqual({ erase: 0, text: './host:pa' })
  })

  it('leaves a colon-named file inside a directory alone', () => {
    // A word with a directory part is already unambiguously a path.
    expect(buildInsertion('sub/', 'a:b', ' ')).toEqual({ erase: 0, text: 'a:b ' })
  })
})

describe('buildInsertion — escaped separators', () => {
  // The request carries the DECODED word, so the server splits `sub\/do` at that
  // slash and answers with prefix `do`. If acceptance treated the escaped slash
  // as part of the name, it would erase `sub\/do` whole and lose the directory.
  it('splits at a backslash-escaped slash', () => {
    // Split at index 4, so only `do` is on screen and `docs` extends it.
    expect(buildInsertion('sub\\/do', 'docs', '/'))
      .toEqual({ erase: 0, text: 'cs/' })
  })

  it('splits at the LAST slash when several are escaped', () => {
    expect(buildInsertion('a\\/b\\/c', 'cat', ' '))
      .toEqual({ erase: 0, text: 'at ' })
  })

  it('still leaves an unescaped directory part alone', () => {
    expect(buildInsertion('sub/do', 'docs', '/')).toEqual({ erase: 0, text: 'cs/' })
  })
})

describe('buildInsertion — escaped on-screen text', () => {
  // Regression: after accepting `my dir/` the screen reads `my\ dir/`, so the
  // insertion has to be measured against the ESCAPED form.
  it('extends an already-escaped word', () => {
    expect(buildInsertion('my\\ d', 'my dir', '/')).toEqual({ erase: 0, text: 'ir/' })
  })

  it('completes a child of an escaped directory', () => {
    expect(buildInsertion('my\\ dir/', 'inner', '/')).toEqual({ erase: 0, text: 'inner/' })
  })

  it('erases only the last segment', () => {
    expect(buildInsertion('sub/Do', 'docs', '/')).toEqual({ erase: 2, text: 'docs/' })
  })

  it('erases in code points, not UTF-16 units', () => {
    // Two DELs for `𝐀b`: one DEL erases one character, so counting code units
    // would delete a third character the user typed before the word.
    expect(buildInsertion('\u{1D400}b', 'docs', '/')).toEqual({ erase: 2, text: 'docs/' })
  })
})

describe('buildInsertion', () => {
  it('appends only the missing suffix in the common case', () => {
    expect(buildInsertion('Kiro', 'KiroCrew', '/')).toEqual({ erase: 0, text: 'Crew/' })
  })

  it('rewrites the token when the typed case differs from disk', () => {
    // Matching is case-insensitive, so `kiro` can select `KiroCrew`; sending
    // only the suffix would leave a path that does not exist.
    expect(buildInsertion('kiro', 'KiroCrew', '/')).toEqual({ erase: 4, text: 'KiroCrew/' })
  })

  it('inserts the whole name for an empty token', () => {
    expect(buildInsertion('', 'docs', '/')).toEqual({ erase: 0, text: 'docs/' })
  })

  it('omits the suffix when none is asked for (Tab prefix insert)', () => {
    expect(buildInsertion('Ki', 'Kiro')).toEqual({ erase: 0, text: 'ro' })
  })

  it('escapes the replacement so a filename cannot alter the command', () => {
    // Every insertion path funnels through here, so escaping cannot be forgotten
    // by a caller.
    expect(buildInsertion('my', 'my file;id', ' '))
      .toEqual({ erase: 0, text: '\\ file\\;id ' })
  })

  it('retypes the word when escaping alters what was typed', () => {
    // The user typed `a*`, which escapes to `a\*` — the escaped name no longer
    // extends the typed text, so the fragment is erased and retyped.
    expect(buildInsertion('a*', 'a*b', ' ')).toEqual({ erase: 2, text: 'a\\*b ' })
  })
})

describe('acceptSuffix', () => {
  it('keeps the path open after a directory', () => {
    expect(acceptSuffix(true)).toBe('/')
  })

  it('closes the word after a file', () => {
    expect(acceptSuffix(false)).toBe(' ')
  })
})

describe('extendsWord', () => {
  // Matching is case-insensitive, so `doc` matches `Docs` AND `DoConfig`, whose
  // common prefix `Do` is SHORTER than what was typed — inserting it would eat a
  // character the user meant to keep.
  it('rejects a common prefix shorter than the typed word', () => {
    expect(extendsWord('Do', 'doc')).toBe(false)
  })

  it('accepts a prefix that genuinely extends the typed word', () => {
    expect(extendsWord('Docs', 'doc')).toBe(true)
  })

  it('accepts an equal-length prefix that only differs in case', () => {
    // Worth inserting: it corrects the word to the name that exists on disk.
    expect(extendsWord('Doc', 'doc')).toBe(true)
  })

  it('rejects an identical prefix', () => {
    expect(extendsWord('doc', 'doc')).toBe(false)
  })

  it('rejects a candidate that diverges from the typed word', () => {
    expect(extendsWord('other', 'doc')).toBe(false)
  })

  it('rejects an empty candidate', () => {
    expect(extendsWord('', 'doc')).toBe(false)
  })

  it('accepts any non-empty candidate when nothing is typed', () => {
    expect(extendsWord('docs', '')).toBe(true)
  })
})

describe('completionMode', () => {
  it('keeps every word the path tier already owned', () => {
    // The two tiers are disjoint and path wins wherever it applied before, so this
    // change cannot regress a word that already produced a menu.
    expect(completionMode('../', 'cd')).toBe('path')
    expect(completionMode('', 'ls')).toBe('path')
    expect(completionMode('src/', 'gh')).toBe('path')
    expect(completionMode('~/w', 'echo')).toBe('path')
    expect(completionMode('./x', 'anything')).toBe('path')
  })

  it('routes a non-path word under a non-path command to the command tier', () => {
    expect(completionMode('', 'gh')).toBe('command')
    expect(completionMode('cre', 'gh')).toBe('command')
    expect(completionMode('commit', 'git')).toBe('command')
  })

  it('routes a flag word to the command tier, which the path tier refuses', () => {
    expect(shouldComplete('--ti', 'gh')).toBe(false)
    expect(completionMode('--ti', 'gh')).toBe('command')
    expect(completionMode('-v', 'docker')).toBe('command')
  })

  it('leaves a path command on the path tier even where a tool might have answered', () => {
    // `python ⎸` must keep listing the cwd rather than start probing a tool that
    // has no completion protocol — the path tier's claim on these words is
    // stronger than the command tier's, so it is checked first and wins.
    expect(completionMode('', 'python')).toBe('path')
    expect(completionMode('foo', 'cat')).toBe('path')
  })

  it('stays shut with no command word and on shell expansions', () => {
    expect(completionMode('gh', '')).toBe('none')
    expect(completionMode('$HO', 'gh')).toBe('none')
    expect(completionMode('`ls', 'gh')).toBe('none')
  })

  it('leaves a word with a separator to the path tier, never the command tier', () => {
    // A word carrying a `/` cannot be a subcommand, so probing for one would
    // spend a subprocess to answer nothing.
    expect(completionMode('o/r', 'gh')).toBe('path')
    expect(completionMode('~/x', 'gh')).toBe('path')
  })
})

describe('commandArgv', () => {
  it('returns the command and the words before the cursor', () => {
    const line = `${PROMPT}gh pr cre`
    expect(commandArgv(line, line.length - 3)).toEqual(['gh', 'pr'])
  })

  it('keeps flag words, which decide where in the tree the cursor is', () => {
    const line = `${PROMPT}kubectl -n kube-system get `
    expect(commandArgv(line, line.length)).toEqual(['kubectl', '-n', 'kube-system', 'get'])
  })

  it('steps past wrappers so the real tool lands at argv[0]', () => {
    const line = `${PROMPT}sudo docker ps `
    expect(commandArgv(line, line.length)).toEqual(['docker', 'ps'])
  })

  it('takes only the last command of a chain', () => {
    const line = `${PROMPT}make build && gh pr `
    expect(commandArgv(line, line.length)).toEqual(['gh', 'pr'])
  })

  it('decodes escapes — argv entries are literal, not shell text', () => {
    const line = `${PROMPT}gh pr view my\\ branch `
    expect(commandArgv(line, line.length)).toEqual(['gh', 'pr', 'view', 'my branch'])
  })

  it('is empty when no command has been typed', () => {
    const line = `${PROMPT}gh`
    expect(commandArgv(line, line.length - 2)).toEqual([])
  })
})

describe('buildInsertion — path guard is a property of the KIND, not the spelling', () => {
  it('guards a FILE named like a flag', () => {
    // `--force` as a filename must be typed `./--force` or the program reads it as
    // an option.
    expect(buildInsertion('--f', '--force', ' ')).toEqual({ erase: 3, text: './--force ' })
  })

  it('does not guard a FLAG named like a flag', () => {
    // The same characters, offered by the command tier, must be typed verbatim —
    // `./--force` would be a path that does not exist.
    expect(buildInsertion('--f', '--force', ' ', false)).toEqual({ erase: 0, text: 'orce ' })
  })

  it('leaves a colon-bearing subcommand alone', () => {
    // The `:` guard exists because rsync/scp read `host:path` as a REMOTE target.
    // An npm-style `run:build` subcommand is argv[1] and not a path at all.
    expect(buildInsertion('run', 'run:build', ' ', false))
      .toEqual({ erase: 0, text: ':build ' })
    expect(buildInsertion('run', 'run:build', ' '))
      .toEqual({ erase: 3, text: './run:build ' })
  })

  it('escapes a filename but never a command token', () => {
    // `=` is escaped in a filename (zsh equals-expansion) but must survive intact
    // in git's `--message=`, which is the tool asking for the value to follow.
    expect(buildInsertion('--m', '--message=', '')).toEqual({
      erase: 3, text: './--message\\=',
    })
    expect(buildInsertion('--m', '--message=', '', false)).toEqual({
      erase: 0, text: 'essage=',
    })
  })
})

describe('isCommandToken', () => {
  it('accepts real subcommand and flag shapes', () => {
    expect(isCommandToken('pr', false)).toBe(true)
    expect(isCommandToken('dry-run', false)).toBe(true)
    expect(isCommandToken('run:build', false)).toBe(true)
    expect(isCommandToken('-v', true)).toBe(true)
    expect(isCommandToken('--dry-run', true)).toBe(true)
    expect(isCommandToken('--message=', true)).toBe(true)
  })

  it('refuses anything a shell would reinterpret', () => {
    // The command tier inserts VERBATIM, so a value needing escaping is not a real
    // flag and must be refused rather than escaped.
    expect(isCommandToken('--x; rm -rf ~', true)).toBe(false)
    expect(isCommandToken('$(id)', false)).toBe(false)
    expect(isCommandToken('a b', false)).toBe(false)
    expect(isCommandToken('`id`', false)).toBe(false)
    expect(isCommandToken('a\nb', false)).toBe(false)
  })

  it('holds each kind to its own shape', () => {
    expect(isCommandToken('--repo', false)).toBe(false)  // a flag is not a subcommand
    expect(isCommandToken('pr', true)).toBe(false)       // and vice versa
    expect(isCommandToken('', false)).toBe(false)
    expect(isCommandToken('---', true)).toBe(false)
  })
})

describe('commandSuffix', () => {
  it('separates so the next word can be completed', () => {
    expect(commandSuffix(false)).toBe(' ')
  })

  it('adds nothing when the value is deliberately unfinished', () => {
    // cobra's NoSpace directive and git's `--message=` form both mean "the value
    // goes here"; a space would strand the cursor past it.
    expect(commandSuffix(true)).toBe('')
  })
})
