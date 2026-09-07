/**
 * Handing a shell snippet to the shell its code fence names.
 *
 * "Run in terminal" writes a snippet into an ALREADY-RUNNING PTY, so the shell
 * that interprets it is whatever the terminal tab launched. A fence tagged
 * `fish` was therefore piped into bash and failed on its first line: the fence
 * language decided whether the button appeared but not which shell ran the code.
 *
 * A snippet whose fence names an INCOMPATIBLE shell is run through that shell as
 * a child of the one already running: `/usr/bin/fish -c '<snippet>'`.
 *
 * Two rules keep this narrow, because not wrapping is always today's behavior
 * and wrapping is the intervention that has to earn its place:
 *
 *  - Only an incompatible pair is wrapped, and the only incompatible boundary is
 *    fish against the POSIX family. bash, sh and zsh run each other's ordinary
 *    commands, so wrapping a ```bash snippet on a zsh host (the macOS default,
 *    since `_resolve_shell` prefers `$SHELL`) would take `cd` and `export` away
 *    from the most common snippet there is, to cure a mismatch that mostly does
 *    not exist. What this deliberately does NOT fix: a bash-only construct under
 *    a strict-POSIX `/bin/sh`, or a zsh builtin under bash. Those fail exactly as
 *    they do today, and the fence tag alone cannot tell whether a given snippet
 *    uses such a construct -- deciding that would need to parse the snippet.
 *  - A wrapped snippet runs in a subshell, so ITS `cd`/`export` do not outlive
 *    it. Accepted only on the fish boundary, where the snippet did not run
 *    correctly at all before.
 *
 * The shell is always named by ABSOLUTE path, taken from the map the gateway
 * resolved and reported. A bare name would be resolved again by the running
 * shell in the terminal's project cwd, where a relative `PATH` entry could
 * supply a planted binary -- the same reason `_resolve_shell` pins the path it
 * validated. A shell absent from that map is not invoked at all.
 *
 * The gateway is never asked to spawn a shell of the client's choosing. It
 * reports what it launched and what exists; the already-running shell does the
 * rest. A fence tag, which is agent-influenceable content, only ever selects
 * among values the gateway itself produced.
 */

import { posixSingleQuote } from './posixQuote'

/**
 * Fence tags that NAME a shell binary, mapped to the program to invoke.
 *
 * The seven tags the button appears on split unevenly. These four name both an
 * executable and a syntax. The other three -- `shell`, `console`, `terminal` --
 * name no particular shell (`console` conventionally marks a session transcript,
 * which is why prompt characters are stripped before running), so there is
 * nothing to honor and they keep the configured shell, exactly as before.
 */
const FENCE_SHELLS: Record<string, string> = {
  bash: 'bash',
  sh: 'sh',
  zsh: 'zsh',
  fish: 'fish',
}

/**
 * Shells that read POSIX-style syntax and POSIX-style single quoting.
 *
 * Membership decides two things at once, which is why it is one list: whether a
 * tag and a host are compatible (same family, no delegation), and whether the
 * wrapper below may quote for that host at all. The quoting has to satisfy the
 * shell RECEIVING the typed line, and only these shells read it as intended.
 * PowerShell and cmd escape an embedded quote by doubling it. fish treats a
 * backslash inside single quotes as an escape, so a snippet carrying one would
 * reach the target with it collapsed, and one carrying both a backslash and a
 * quote would leave fish reading an unterminated quote. Neither is a host this
 * types into.
 */
const POSIX_FAMILY = new Set(['bash', 'sh', 'zsh', 'dash', 'ash', 'ksh'])

/** Syntax family of a shell program name, or undefined when unrecognized. */
function shellFamily(name: string): 'posix' | 'fish' | undefined {
  if (name === 'fish') return 'fish'
  if (POSIX_FAMILY.has(name)) return 'posix'
  return undefined
}

/** Program name of a shell path, lowercased and without a Windows suffix. */
export function shellBaseName(shellPath: string): string {
  const base = shellPath.replace(/\\/g, '/').split('/').pop() ?? ''
  return base.toLowerCase().replace(/\.exe$/, '')
}

/** The shell a fence tag names, or undefined when the tag names none. */
export function shellForFenceLang(lang?: string): string | undefined {
  if (!lang) return undefined
  return FENCE_SHELLS[lang.toLowerCase()]
}

/**
 * Quote `s` as a single POSIX word. Shared with the webhook request examples,
 * which need the same idiom for an unrelated reason.
 */
export { posixSingleQuote }

/**
 * The text to write into a terminal for a fence-tagged snippet.
 *
 * Returns `code` unchanged -- today's behavior -- unless every one of these
 * holds: the fence names a shell, the launched shell is known and recognized,
 * the two are in different syntax families, and the gateway reported an absolute
 * path for the shell the fence names.
 *
 * @param fenceShells name -> absolute path, as reported by the gateway. A shell
 * missing here is never invoked: guessing a path is exactly the hijack this
 * avoids.
 */
export function runInTerminalText(
  code: string,
  lang: string | undefined,
  launchedShell: string | undefined,
  fenceShells: Record<string, string> = {},
): string {
  const target = shellForFenceLang(lang)
  if (!target) return code
  if (!launchedShell) return code

  // Two asymmetries, both measured rather than assumed.
  //
  // The quoting has to satisfy the shell RECEIVING the typed line, and only a
  // POSIX host reads the `'\''` idiom as intended -- fish treats a backslash
  // inside single quotes as an escape.
  //
  // And only fish is delegated TO. A `bash -c` (likewise sh/zsh via bash) sources
  // `$BASH_ENV` when non-interactive: with that set, `BASH_ENV=f bash -c 'echo x'`
  // runs f BEFORE the snippet, so the delegated line would execute startup code
  // the user never saw in the confirmation dialog. No bash flag suppresses it
  // (`--norc`/`--noprofile` govern interactive and login shells), so suppressing
  // it would mean manipulating the child environment from a typed line -- more
  // machinery, and fragile on a fish host. `fish --no-config -c` has no such
  // preload, which is why the fish direction is the one that ships.
  const hostFamily = shellFamily(shellBaseName(launchedShell))
  if (hostFamily !== 'posix') return code
  if (target !== 'fish') return code

  const targetPath = fenceShells[target]
  if (!targetPath) return code

  // --no-config: run exactly the approved snippet, not the user's config.fish.
  return `${posixSingleQuote(targetPath)} --no-config -c ${posixSingleQuote(code.trimEnd())}`
}
