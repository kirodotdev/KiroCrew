/**
 * Every product `<ChatInput>` mount decides `lexicalComposer` explicitly.
 *
 * The rich paste-pill composer is the product default on every chat surface,
 * but the DEFAULT of the prop itself stays off (`lexicalComposer?: boolean`,
 * see ChatInput.tsx) so a bare `<ChatInput>` — the textarea path that remains
 * the chunk-load-failure fallback, and the component-level tests that pin it —
 * keeps its contract. That leaves a hole the Design Review lane named on
 * #11100: a FUTURE host that omits the prop would silently get back the
 * textarea/mirror path whose defect (#8309) this composer exists to remove,
 * with nothing red. This scan closes the hole at the level the decision is
 * made: every product mount must name the prop. An explicit
 * `lexicalComposer={false}` is allowed — it is a visible decision a reviewer
 * can read, unlike an omission.
 *
 * A typed forwarding wrapper — `function W(props: React.ComponentProps<typeof
 * ChatInput>)` re-mounting `<ChatInput {...props} />` — is followed to its own
 * `<W …>` mounts, which are held to the same rules: React's stable-props shims
 * forward the decision rather than make it, and must not reopen the hole.
 *
 * Scope: production `.tsx` under src/, excluding the exact src/test tree, the
 * composer's dev-only harness page, Storybook stories (`*.stories.tsx` never
 * reach the production bundle — see `.storybook/main.ts`), and ChatInput.tsx
 * itself.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, extname, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const walk = (dir: string): string[] => {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      if (p === join(SRC, 'test') || p === join(SRC, 'composer', '__harness__') || name === 'node_modules') continue
      out.push(...walk(p))
    } else if (extname(name) === '.tsx' && !/\.(test|stories)\.[jt]sx?$/.test(name) && p !== join(SRC, 'components', 'ChatInput.tsx')) {
      out.push(p)
    }
  }
  return out
}

/**
 * Comments are dropped before any scan (a doc comment may say "renders
 * <ChatInput>"): block comments, and line comments that start a line — the
 * shapes prose lives in.
 */
const stripComments = (src: string): string => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

/**
 * The opening tags of every `<Name ...>` in comment-stripped code, props
 * included. A prop value may itself contain `>` (`onX={() => ...}`), so the tag
 * ends at the first `>` outside braces, not the first `>`.
 */
function openingTags(code: string, name: string): string[] {
  const tags: string[] = []
  const re = new RegExp(`<${name}\\b`, 'g')
  for (let m = re.exec(code); m; m = re.exec(code)) {
    let depth = 0
    let i = m.index
    for (; i < code.length; i++) {
      const c = code[i]
      if (c === '{') depth++
      else if (c === '}') depth--
      else if (c === '>' && depth === 0) break
    }
    tags.push(code.slice(m.index, i + 1))
  }
  return tags
}

/**
 * A typed forwarding wrapper: `function Name(props: React.ComponentProps<typeof
 * ChatInput>)` or the arrow-const of the same shape (the `React.` prefix is
 * optional). Captures: [1] function name, [2] const name, [3] the parameter.
 */
const WRAPPER_DECL = /\b(?:function\s+([A-Z][\w$]*)|const\s+([A-Z][\w$]*)\s*=)\s*\(\s*([\w$]+)\s*:\s*(?:React\.)?ComponentProps<typeof ChatInput>\s*\)/g

/**
 * The mounts a source file is held to. Every direct `<ChatInput ...>` tag, plus
 * the `<Name ...>` tags of each typed forwarding wrapper declared in the file
 * (ChatPage's `StableChatInput`, a stable-props shim for `memo(ChatInput)`).
 * The wrapper's own pass-through — a `<ChatInput>` whose props are ONLY a spread
 * of its parameter, `{...props}` or `{...call(props)}` — forwards the decision
 * rather than making it, so it is dropped in favour of the wrapper mounts. Only
 * then: a wrapper the file never mounts keeps its shim as a flagged mount, so
 * the file cannot pass silently. Any other spread (an untyped parameter, extra
 * props beside the spread) is an ordinary mount and stays under the rules.
 *
 * Shims are claimed by OCCURRENCE, not by text: two wrappers sharing a parameter
 * name emit textually identical shims, so each mounted wrapper drops exactly ONE
 * not-yet-claimed matching `<ChatInput …>` occurrence. An unmounted wrapper then
 * leaves one identical shim occurrence still flagged, rather than a text-keyed
 * set silently swallowing both.
 */
function chatInputMounts(src: string): string[] {
  const code = stripComments(src)
  const direct = openingTags(code, 'ChatInput')
  const claimed = new Set<number>()
  const forwarded: string[] = []
  for (const [, fn, arrow, param] of code.matchAll(WRAPPER_DECL)) {
    const mounts = openingTags(code, fn ?? arrow)
    if (mounts.length === 0) continue
    const p = param.replace(/\$/g, '\\$')
    const shim = new RegExp(`^<ChatInput\\s+\\{\\.\\.\\.(?:${p}|[\\w$]+\\(\\s*${p}\\s*\\))\\}\\s*/?>$`)
    const hit = direct.findIndex((tag, i) => !claimed.has(i) && shim.test(tag))
    if (hit !== -1) claimed.add(hit)
    forwarded.push(...mounts)
  }
  return [...direct.filter((_, i) => !claimed.has(i)), ...forwarded]
}

const namesLexicalComposer = (tag: string): boolean => /\blexicalComposer\b/.test(tag)

/** `lexicalComposer={!touchDevice}` on the tag, fed by `useTouchDeviceAtMount()` in the file. */
const gatedAtMount = (src: string, tag: string): boolean =>
  /\buseTouchDeviceAtMount\(\)/.test(src) && !/\buseIsTouchDevice\(\)/.test(src) && /\blexicalComposer=\{!touchDevice\}/.test(tag)

describe('ChatInput hosts — lexicalComposer is decided at every product mount', () => {
  const mountsByFile = walk(SRC)
    .map(f => [f, chatInputMounts(readFileSync(f, 'utf-8'))] as const)
    .filter(([, mounts]) => mounts.length > 0)

  it('finds the three product hosts (the scan itself is not vacuous)', () => {
    const files = mountsByFile.map(([f]) => f.slice(SRC.length + 1)).sort()
    expect(files).toEqual(expect.arrayContaining([
      'components/ChatPane.tsx',
      'pages/ChatPage.tsx',
      'pages/chat/SideChat.tsx',
    ]))
  })

  it('every product <ChatInput> mount names the lexicalComposer prop', () => {
    const silent = mountsByFile.flatMap(([f, mounts]) =>
      mounts.filter(tag => !namesLexicalComposer(tag)).map(tag => `${f.slice(SRC.length + 1)}: ${tag.replace(/\s+/g, ' ').slice(0, 80)}…`))
    expect(silent).toEqual([])
  })

  // Touch devices keep the textarea composer until a device pass records that
  // soft-keyboard composition and pill reorder hold up on the Lexical composer
  // (Design Review on #11100). The gate is a visible decision at each mount —
  // `lexicalComposer={!touchDevice}` fed by `useTouchDeviceAtMount()` — so lifting
  // it later is a deliberate edit here, not a silent drift on one host. The
  // MOUNT-latched hook, not the reactive `useIsTouchDevice()`: a live pointer
  // capability change must not hard-swap the editor under a draft (Opus Review
  // on #11100; `website/AGENTS.md`, "never a hard swap of two components").
  it('every product <ChatInput> mount gates the rich composer on the touch-device signal, latched at mount', () => {
    const ungated = mountsByFile.flatMap(([f, mounts]) => {
      const src = readFileSync(f, 'utf-8')
      return mounts
        .filter(tag => !gatedAtMount(src, tag))
        .map(tag => `${f.slice(SRC.length + 1)}: ${tag.replace(/\s+/g, ' ').slice(0, 80)}…`)
    })
    expect(ungated).toEqual([])
  })

  it('reads a mount whose props contain arrows and nested braces as ONE tag, and ignores prose in comments', () => {
    const src = `<ChatInput\n  onSend={(t) => { if (t.length > 0) send(t) }}\n  lexicalComposer\n/>`
    expect(chatInputMounts(src)).toEqual([src])
    expect(chatInputMounts('<ChatInputSomethingElse foo />')).toEqual([])
    expect(chatInputMounts('/**\n * Renders the REAL native <ChatInput> inside <SlotProvider>.\n */\n// <ChatInput> here too\nconst x = 1')).toEqual([])
  })

  it('follows a typed forwarding wrapper to its own mounts, and holds ONLY that shape to it', () => {
    const hook = 'const touchDevice = useTouchDeviceAtMount()\n'
    const wrapper = 'function StableChatInput(props: React.ComponentProps<typeof ChatInput>) {\n  return <ChatInput {...useStableCallbackProps(props)} />\n}\n'
    // Gated wrapper mount: the shim is dropped, the mount is what gets judged, and it passes.
    const gated = '<StableChatInput\n  lexicalComposer={!touchDevice}\n  onSend={() => send()}\n/>'
    expect(chatInputMounts(hook + wrapper + gated)).toEqual([gated])
    expect(namesLexicalComposer(gated)).toBe(true)
    expect(gatedAtMount(hook + wrapper + gated, gated)).toBe(true)
    // Ungated wrapper mount: judged like a direct mount, and flagged by both rules.
    const ungated = '<StableChatInput onSend={() => send()} />'
    expect(chatInputMounts(hook + wrapper + ungated)).toEqual([ungated])
    expect(namesLexicalComposer(ungated)).toBe(false)
    expect(gatedAtMount(hook + wrapper + ungated, ungated)).toBe(false)
    // A wrapper the file never mounts keeps its shim as a flagged mount — no silent pass.
    expect(chatInputMounts(hook + wrapper)).toEqual(['<ChatInput {...useStableCallbackProps(props)} />'])
    // The bare-spread and arrow-const / unprefixed-type forms are the same wrapper shape.
    expect(chatInputMounts('const W = (p: ComponentProps<typeof ChatInput>) => <ChatInput {...p} />\n<W lexicalComposer />')).toEqual(['<W lexicalComposer />'])
    // Not the shape: an untyped spread, or a spread with props beside it, stays a flagged direct mount.
    const untyped = 'function Shim(rest: ShimProps) {\n  return <ChatInput {...rest} />\n}\n<Shim lexicalComposer={!touchDevice} />'
    expect(chatInputMounts(untyped)).toEqual(['<ChatInput {...rest} />'])
    const extra = 'const W = (p: ComponentProps<typeof ChatInput>) => <ChatInput {...p} disabled />\n<W lexicalComposer />'
    expect(chatInputMounts(extra)).toEqual(['<ChatInput {...p} disabled />', '<W lexicalComposer />'])
    // Two wrappers sharing a param name emit textually identical shims. Claiming by
    // occurrence, not text: only A mounted -> A's shim is dropped, B's identical shim
    // stays flagged; both mounted -> both shims dropped.
    const twoWrappers = 'function A(props: React.ComponentProps<typeof ChatInput>) {\n  return <ChatInput {...props} />\n}\n'
      + 'function B(props: React.ComponentProps<typeof ChatInput>) {\n  return <ChatInput {...props} />\n}\n'
    const aMount = '<A\n  lexicalComposer={!touchDevice}\n  onSend={() => send()}\n/>'
    const bMount = '<B lexicalComposer={!touchDevice} />'
    expect(chatInputMounts(hook + twoWrappers + aMount)).toEqual(['<ChatInput {...props} />', aMount])
    expect(chatInputMounts(hook + twoWrappers + aMount + bMount)).toEqual([aMount, bMount])
  })
})
