/**
 * Commands contributed by installed apps.
 *
 * This is what lets a Command Bar row live OUTSIDE this repository. An app
 * declares `contributes.commands` in its manifest; this module turns that
 * declaration into rows the launcher can rank and run. Nothing here executes app
 * code — a contribution is data, and the host is the only thing that acts on it.
 *
 * **Everything below re-validates what the backend already checks.** That is not
 * belt-and-braces for its own sake: `AppManifest` only grew a typed `contributes`
 * field recently, and an unknown top-level manifest key reaches this dashboard
 * through the manifest's `extra` bucket without passing any schema at all. So an
 * app installed by an older gateway, or one whose `app.json` was edited in place,
 * can put an arbitrary object on this path. Manifest data from a third party is
 * untrusted input; the same posture the overlay resolver takes.
 *
 * A bad declaration is SKIPPED with a warning, never thrown: a malformed app must
 * not be able to take the launcher down (and by extension the Cmd+K gesture) for
 * every other app on the instance.
 *
 * Kept pure and dependency-light so the rules are pinned by unit test rather than
 * by reading the component. Icons stay STRINGS here; resolving one to a glyph is
 * the renderer's job.
 */

/** The subset of `GET /api/apps` this module reads. */
export interface CommandAppRecord {
  name: string
  displayName?: string
  enabled?: boolean
  manifest?: {
    contributes?: {
      commands?: unknown
    }
  }
}

/** The one value a contributed command collects before it can act. */
export interface ContributedArgument {
  placeholder: string
  hint: string
  /**
   * Whether a value is acceptable — a HOST-implemented check, chosen by the
   * manifest's `kind`, never a matcher the manifest supplied itself.
   *
   * This is the field that used to be an app-supplied `RegExp`, and the change is
   * the point rather than a refactor. A regex is a small program, and this one runs
   * against the field on every keystroke on the thread that draws the launcher:
   * `^(a+)+$` and `^(a|aa)+$` are both under ten characters and both exponential,
   * and JavaScript cannot interrupt a synchronous match. Screening the pattern
   * syntactically was tried and abandoned — such a check can only recognize shapes,
   * so each version invites the next hostile pattern it does not cover. Every
   * implementation behind this predicate runs in time proportional to the input no
   * matter what the manifest asks for.
   */
  accept: (value: string) => boolean
  /** App-supplied message shown when the value is not accepted. */
  patternError: string
}

export interface ContributedCommand {
  /**
   * Row id, namespaced by the contributing app.
   *
   * The namespace is what makes a contributed row structurally unable to
   * impersonate a builtin one: builtin ids are `command:*` / `setting:*` / `app:*`,
   * and this shape can only ever collide with another contribution from the same
   * app under the same command id — which the manifest already refuses as a
   * duplicate.
   */
  id: string
  /** `displayName` of the contributing app, or its name. */
  appLabel: string
  title: string
  subtitle: string
  /** Host glyph name; the renderer maps it, and an unknown name falls back. */
  icon: string
  keywords: string[]
  /** Prompt template. Contains `{argument}` exactly when `argument` is set. */
  prompt: string
  /** Send the seeded prompt immediately rather than leaving it in the composer. */
  autoSend: boolean
  argument: ContributedArgument | null
}

/**
 * The placeholder a prompt template uses to interpolate the collected argument.
 *
 * A regex rather than a string constant, for two reasons. It is the only form that
 * substitutes every occurrence in one pass; and a string constant holding
 * `{argument}` reads as untranslated UI copy to the strict i18n rule, which is wrong
 * — this is a structural token in a manifest contract, never rendered.
 */
const ARGUMENT_TOKEN_RE = /\{argument\}/g
// The same token as a plain string, for counting occurrences without running a regex.
// Built from a char code so the strict i18n rule does not read `{argument}` as UI copy.
const ARGUMENT_TOKEN_LITERAL = `${String.fromCharCode(123)}argument${String.fromCharCode(125)}`

/** Whether a prompt template interpolates the collected value. */
function interpolatesArgument(prompt: string): boolean {
  // `test` on a /g regex advances lastIndex, so a fresh copy per call keeps this
  // predicate pure — a shared /g regex answers differently on alternate calls.
  return new RegExp(ARGUMENT_TOKEN_RE.source).test(prompt)
}

/** Same narrow kebab slug the manifest enforces. */
const COMMAND_ID_RE = /^[a-z0-9][a-z0-9-]*$/

/** Caps, all mirroring the manifest's own. */
const MAX_HOSTS = 20
const MAX_PROMPT = 4000
const MAX_TITLE = 120

/**
 * Longest accepted argument VALUE, and a ceiling on what expansion may produce.
 *
 * The template is capped at 4000 characters, and `{argument}` is ten of them, so a
 * prompt may carry up to ~400 placeholders. Multiplied by an unbounded pasted value
 * that is a gigabyte-scale allocation — and `resolvePrompt` runs on every keystroke to
 * draw the preview, so it does not even need a submit to take the tab down.
 *
 * Every other bound in this contract limits what the APP declares. This is the one that
 * limits what the READER supplies, which is why it was missing: the value was treated
 * as the trusted half of the pair. It is trusted for CONTENT — it goes to an agent
 * verbatim — and untrusted for SIZE.
 */
const MAX_ARGUMENT_VALUE = 2000
const MAX_RESOLVED = 20000

/**
 * Bounds on the hidden match aliases.
 *
 * `rankRootRows` walks every keyword of every row on every keystroke, so this is the
 * one declared field whose cost is paid per character typed rather than once per render.
 * Twenty rows times an unbounded alias list is a launcher that stops responding while
 * showing nothing wrong.
 *
 * Over-long aliases are dropped rather than the whole command: a keyword is additive by
 * definition -- losing one costs a match the reader probably was not typing, while losing
 * the row costs them the command.
 */
const MAX_KEYWORDS = 30
const MAX_KEYWORD = 60

/** Matchers the HOST implements. The manifest picks one; it cannot supply its own. */
const KINDS = ['url', 'text'] as const

/**
 * A literal hostname, optionally with a leading dot meaning "this domain or any
 * subdomain of it". Fixed and host-owned — never built from manifest input.
 *
 * Mirrors `_HOST_RE` in `apps/manifest.py`. An entry that is not a hostname cannot ever
 * match a parsed URL's `hostname`, so accepting it would leave the command permanently
 * unusable with no explanation; the manifest names it as an error and so does this.
 */
const HOST_RE = /^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/

/**
 * Resolve a manifest's `kind` to the host's own check, or `null` if unknown.
 *
 * Every branch runs in time proportional to the input. `url` leans on the runtime's
 * URL parser rather than a pattern — that parser is linear and cannot be made to
 * backtrack, which is the entire reason this indirection exists.
 *
 * An unknown kind is refused rather than defaulting to `text`: a manifest asking for
 * a check this host does not have should not silently get a weaker one, since the
 * value goes on to be spliced into an instruction for an agent with tools.
 */
function matcherFor(kind: string, hosts: string[]): ((value: string) => boolean) | null {
  if (kind === 'text') return value => value.trim().length > 0
  if (kind === 'url') {
    return value => {
      let url: URL
      try {
        url = new URL(value.trim())
      } catch {
        return false
      }
      // Only the two web schemes. `javascript:` and `data:` parse as valid URLs, and
      // this value is shown back to the reader and handed to an agent.
      if (url.protocol !== 'https:' && url.protocol !== 'http:') return false
      if (hosts.length === 0) return true
      const host = url.hostname.toLowerCase()
      // A leading dot means "this domain or any subdomain of it". Without it the match
      // is exact, so `github.com` does not admit `github.com.evil.test`.
      return hosts.some(h => (h.startsWith('.') ? host === h.slice(1) || host.endsWith(h) : host === h))
    }
  }
  return null
}

/**
 * Most commands one app may contribute.
 *
 * A group is display-capped anyway, so this is not about layout — it is about an
 * app being unable to make ranking O(a lot) on every keystroke in the root.
 */
const MAX_COMMANDS_PER_APP = 20

function warnContributionSkipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
  console.warn(
    `[command-bar] app ${JSON.stringify(appName)} contributed command ${JSON.stringify(id)} was skipped: ${reason}`,
  )
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** The row survives; only its attribution is gone. Distinct wording from a skip. */
function warnAttributionClipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- otherwise the app author has no way to notice
  console.warn(
    `[command-bar] app ${JSON.stringify(appName)} command ${JSON.stringify(id)} renders with a clipped attribution: ${reason}`,
  )
}

/**
 * Build the argument descriptor, or report why it is unusable.
 *
 * Returns `undefined` for "no argument declared" and `null` for "declared but
 * invalid" — the caller must treat those differently: a command with no argument
 * is a perfectly good command, while one whose argument is broken must not run at
 * all, because its prompt interpolates a value that was never checked.
 */
function readArgument(
  appName: string,
  id: string,
  raw: unknown,
): ContributedArgument | null | undefined {
  if (raw === undefined || raw === null) return undefined
  // `typeof [] === 'object'`, so an array would otherwise reach the property reads
  // below, find nothing, and settle on the default `text` matcher -- accepting any
  // non-empty value. The manifest validator compares against a dict, so leaving the
  // array accepted here would make the two validators disagree on the same input.
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    warnContributionSkipped(appName, id, 'argument is not an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  if ('pattern' in obj) {
    // An app written against the revision of this contract that accepted its own
    // regex. Refused rather than ignored: `pattern` is an unknown key now, so skipping
    // past it would leave the argument on the default `text` matcher — accepting ANY
    // non-empty string — while the app still declares autoSend and still believes its
    // pattern guards the value.
    warnContributionSkipped(
      appName,
      id,
      `argument.pattern is no longer accepted; declare argument.kind (${KINDS.join(', ')}) instead`,
    )
    return null
  }
  const kind = str(obj.kind) || 'text'
  if ('hosts' in obj && !Array.isArray(obj.hosts)) {
    // Coercing to `[]` would not mean "no opinion" — an empty allowlist means ANY host.
    // So `"hosts": "github.com"` would erase the restriction its author wrote, silently,
    // with autoSend still on. Refused for the same reason `pattern` is.
    warnContributionSkipped(appName, id, 'argument.hosts must be an array of hostnames')
    return null
  }
  const hostsRaw = Array.isArray(obj.hosts) ? obj.hosts : []
  const hosts = hostsRaw.map(h => str(h).trim().toLowerCase()).filter(Boolean)
  if (hosts.length > MAX_HOSTS) {
    warnContributionSkipped(appName, id, `argument.hosts exceeds ${MAX_HOSTS} entries`)
    return null
  }
  const badHost = hosts.find(h => !HOST_RE.test(h))
  if (badHost !== undefined) {
    warnContributionSkipped(appName, id, `argument.hosts entry is not a hostname: ${badHost}`)
    return null
  }
  const accept = matcherFor(kind, hosts)
  if (!accept) {
    warnContributionSkipped(appName, id, `argument.kind must be one of ${KINDS.join(', ')}`)
    return null
  }
  if (hosts.length > 0 && kind !== 'url') {
    warnContributionSkipped(appName, id, "argument.hosts applies only to kind 'url'")
    return null
  }
  // The last app-supplied strings with no bound. These are not searched, so they do not
  // cost per keystroke like the title and subtitle -- they cost LAYOUT: a kilobyte `hint`
  // renders as an unbroken paragraph in the argument body, and a kilobyte `patternError`
  // lands untruncated in the alert strip, both of which push the resolved-prompt preview
  // and the footer around. The preview is this feature's consent surface, so a string an
  // app chooses must not be able to move it off screen. Same cap and same refuse-rather-
  // than-trim rule as the title.
  const placeholder = str(obj.placeholder)
  const hint = str(obj.hint)
  const patternError = str(obj.patternError)
  for (const [field, value] of [
    ['placeholder', placeholder],
    ['hint', hint],
    ['patternError', patternError],
  ] as const) {
    if (value.length > MAX_TITLE) {
      warnContributionSkipped(appName, id, `argument.${field} exceeds ${MAX_TITLE} characters`)
      return null
    }
  }
  return {
    placeholder,
    hint,
    accept,
    patternError,
  }
}

function readCommand(app: CommandAppRecord, raw: unknown): ContributedCommand | null {
  if (typeof raw !== 'object' || raw === null) {
    warnContributionSkipped(app.name, raw, 'entry is not an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  const id = str(obj.id)
  if (!COMMAND_ID_RE.test(id)) {
    warnContributionSkipped(app.name, obj.id, 'id must be lowercase alphanumeric with dashes')
    return null
  }
  const title = str(obj.title)
  if (!title) {
    warnContributionSkipped(app.name, id, 'missing title')
    return null
  }
  if (title.length > MAX_TITLE) {
    warnContributionSkipped(app.name, id, `title exceeds ${MAX_TITLE} characters`)
    return null
  }
  const prompt = str(obj.prompt)
  if (!prompt) {
    warnContributionSkipped(app.name, id, 'missing prompt')
    return null
  }
  if (prompt.length > MAX_PROMPT) {
    warnContributionSkipped(app.name, id, `prompt exceeds ${MAX_PROMPT} characters`)
    return null
  }
  // The last two searchable strings to be bounded, and the ones that made the other
  // bounds incomplete: `rankRootRows` runs `fuzzyMatch` over the row's SUBTITLE on every
  // keystroke (`rootIndex.ts`), and the rendered subtitle falls back to `appLabel`, so
  // both are scanned per character typed exactly like the title and keywords already
  // capped above. `displayName` carries no bound anywhere in the manifest, so without
  // this the launcher's per-keystroke work is set by a field nothing constrains.
  // Refused rather than trimmed, for the same reason the argument value is: this module
  // does not silently shorten what it was given and then act on the remainder.
  const subtitle = str(obj.subtitle)
  if (subtitle.length > MAX_TITLE) {
    warnContributionSkipped(app.name, id, `subtitle exceeds ${MAX_TITLE} characters`)
    return null
  }
  // An over-long app label costs the LENGTH of the attribution, never the attribution
  // itself. Two passes over this line pulled in opposite directions and the resolution
  // has to satisfy both. Refusing the command was a one-sided cap of exactly the kind
  // this file mirrors every other bound to prevent: `displayName` is validated for
  // PRESENCE only and bounded nowhere in the manifest, so an app with a 200-character
  // display name installed clean and then showed no commands and no error, while none
  // of the command's OWN fields was the thing that was too long. But dropping the label
  // to empty then erased the provenance: `kindLabel` renders a bare kind when there is
  // no label, which is character-for-character what a BUILTIN row shows, so a
  // contributed row whose prompt goes to a tool-enabled agent read as native.
  //
  // So the label degrades in steps and never becomes empty -- `displayName`, else the
  // app's `name`, else that name clipped. `name` needs the bound as much as
  // `displayName` did: `KEBAB_RE` constrains its alphabet and not its length, and this
  // label is what the rendered subtitle falls back to, so an unclipped 200-character
  // name would buy the attribution back by restoring the per-keystroke scan the caps
  // above exist to bound.
  //
  // The clip carries no ellipsis, and deliberately: it would be the one rendered string
  // literal in this file, and it is not copy that any of the eleven catalogs could
  // translate. It also would not earn its keep -- two over-long names sharing a
  // 120-character prefix render identically with or without a marker, so the ellipsis
  // does not disambiguate the provenance it would appear to qualify. What survives the
  // clip is what the label is for: a prefix of the contributing app's own identifier,
  // which is still unmistakably not a builtin.
  const rawAppLabel = app.displayName || app.name
  let appLabel = rawAppLabel
  if (appLabel.length > MAX_TITLE) {
    appLabel = app.name.slice(0, MAX_TITLE)
    warnAttributionClipped(app.name, id, `app label exceeds ${MAX_TITLE} characters`)
  }

  const argument = readArgument(app.name, id, obj.argument)
  if (argument === null) return null
  const interpolates = interpolatesArgument(prompt)
  if (argument === undefined && interpolates) {
    warnContributionSkipped(app.name, id, 'prompt interpolates {argument} but no argument is declared')
    return null
  }
  if (argument !== undefined && !interpolates) {
    // The reader would be asked for a value the command then ignores, and the
    // command would still run — a silently wrong result rather than a visible one.
    warnContributionSkipped(app.name, id, 'declares an argument but the prompt never uses {argument}')
    return null
  }

  const keywords = Array.isArray(obj.keywords)
    ? obj.keywords
        .filter((k): k is string => typeof k === 'string')
        .filter(k => k.length > 0 && k.length <= MAX_KEYWORD)
        .slice(0, MAX_KEYWORDS)
    : []

  return {
    id: `app:${app.name}:${id}`,
    appLabel,
    title,
    subtitle,
    icon: str(obj.icon),
    keywords,
    prompt,
    // `autoSend` is only honoured for a command that COLLECTS something, and the
    // clamp lives here rather than at the call site so no consumer can get it wrong.
    //
    // The host's consent mechanism for autoSend is the resolved-prompt preview, and
    // that preview lives in the argument state. A command with no argument never
    // enters it, so honouring autoSend there would send app-authored text to a
    // tool-enabled agent with nothing shown to the reader at all — the exact hole a
    // misleading row in a hostile manifest would aim for. Such a command still runs;
    // it lands in the composer, where the text is visible and one keystroke sends it.
    //
    // The manifest refuses this combination outright, so a validated app never
    // reaches here with it. This clamp is for the app that skipped that check: an
    // unknown manifest key arrives through `extra` having passed no schema at all.
    autoSend: obj.autoSend === true && argument !== undefined,
    argument: argument ?? null,
  }
}

/**
 * Every valid command contributed by the ENABLED installed apps.
 *
 * Disabled apps contribute nothing: the enable state is the reader's switch for
 * the whole app, and a row that still ran from a disabled app would make that
 * switch a lie.
 *
 * No provenance check beyond that, unlike the overlay resolver — and the
 * difference is the point. An overlay REPLACES a host surface, so only a builtin
 * may claim one; a command ADDS a row that the host renders and runs, which is
 * exactly the capability an external app is supposed to have.
 */
export function contributedCommands(apps: readonly CommandAppRecord[]): ContributedCommand[] {
  const out: ContributedCommand[] = []
  for (const app of apps) {
    // Per app, matching the manifest: two apps may use the same command id, since the
    // row id they produce is namespaced by app.
    const seen = new Set<string>()
    if (!app.enabled) continue
    // The name is the one field of the app the rows cannot do without, so it is checked
    // here rather than assumed from the backend's app-name contract. It namespaces the
    // row id, and it is the last fallback for the attribution label -- an app with no
    // name would produce `app::<id>` and, if its `displayName` were also over-long, a
    // row with no attribution at all, which is the exact indistinguishable-from-builtin
    // outcome the label chain below exists to rule out.
    if (!app.name) {
      warnContributionSkipped('(unnamed)', '(all)', 'app record has no name')
      continue
    }
    const raw = app.manifest?.contributes?.commands
    if (raw === undefined || raw === null) continue
    if (!Array.isArray(raw)) {
      warnContributionSkipped(app.name, '(all)', 'contributes.commands is not an array')
      continue
    }
    // Sliced BEFORE the loop, so the cap bounds the WORK and not just the output. A
    // per-entry `taken` counter that only advanced on success would let a manifest
    // with fifty thousand malformed entries run fifty thousand validations and fifty
    // thousand `console.warn` calls synchronously on the thread that draws the
    // launcher — every one of them rejected, and the dashboard frozen all the same.
    const entries = raw.slice(0, MAX_COMMANDS_PER_APP)
    if (raw.length > MAX_COMMANDS_PER_APP) {
      warnContributionSkipped(app.name, '(rest)', `more than ${MAX_COMMANDS_PER_APP} commands contributed`)
    }
    for (const entry of entries) {
      const cmd = readCommand(app, entry)
      if (!cmd) continue
      if (seen.has(cmd.id)) {
        // The manifest refuses a duplicate id because the second row silently wins the
        // frecency record and one of the two becomes unreachable by usage. Keeping both
        // here would reproduce exactly that, so the first declaration wins and the rest
        // are named on the console.
        warnContributionSkipped(app.name, entry, 'duplicate command id')
        continue
      }
      seen.add(cmd.id)
      out.push(cmd)
    }
  }
  return out
}

/**
 * Build the prompt to seed, with the collected argument spliced in.
 *
 * The value is inserted verbatim: it has already passed the host matcher named by the
 * command's argument kind, which is where the domain of acceptable input is decided.
 * Quoting or escaping here would corrupt the very thing the matcher admitted (a URL,
 * an identifier) for no gain — the recipient is an agent reading prose, not a shell.
 *
 * Verbatim is exactly why the replacement is a FUNCTION and not the string itself:
 * `String.replace` reads `$&`, `$1` and `$$` in a replacement string as references
 * to the match, so a pasted value containing one of those would be silently
 * rewritten into something the reader never typed. A replacer returns the value
 * untouched.
 */
export function resolvePrompt(command: ContributedCommand, argument: string): string {
  if (!command.argument) return command.prompt
  const value = argument.trim()
  // Refuse rather than truncate: a silently shortened value would be spliced into an
  // instruction for an agent with tools, and half a URL or half a query is a different
  // request from the one the reader made. `argumentIsValid` rejects an over-long value
  // first, so reaching this is a caller that skipped the check.
  if (value.length > MAX_ARGUMENT_VALUE) return command.prompt
  const occurrences = command.prompt.split(ARGUMENT_TOKEN_LITERAL).length - 1
  if (command.prompt.length + occurrences * value.length > MAX_RESOLVED) return command.prompt
  return command.prompt.replace(new RegExp(ARGUMENT_TOKEN_RE.source, 'g'), () => value)
}

/** Whether `value` satisfies the command's declared argument kind. */
export function argumentIsValid(command: ContributedCommand, value: string): boolean {
  if (!command.argument) return true
  const trimmed = value.trim()
  // Bounded before the matcher runs: this is the gate that stops an outsized paste from
  // reaching `resolvePrompt`, which the preview calls on every keystroke.
  if (trimmed.length > MAX_ARGUMENT_VALUE) return false
  const occurrences = command.prompt.split(ARGUMENT_TOKEN_LITERAL).length - 1
  if (command.prompt.length + occurrences * trimmed.length > MAX_RESOLVED) return false
  return command.argument.accept(trimmed)
}
