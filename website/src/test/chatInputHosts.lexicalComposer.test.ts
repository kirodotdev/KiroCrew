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
 * Scope: production `.tsx` under src/, excluding the exact src/test tree, the
 * composer's dev-only harness page, and ChatInput.tsx itself.
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
    } else if (extname(name) === '.tsx' && !/\.test\.[jt]sx?$/.test(name) && p !== join(SRC, 'components', 'ChatInput.tsx')) {
      out.push(p)
    }
  }
  return out
}

/**
 * The opening tags of every `<ChatInput ...>` in a source file, props included.
 * Comments are dropped first (a doc comment may say "renders <ChatInput>"): block
 * comments, and line comments that start a line — the shapes prose lives in. A
 * prop value may itself contain `>` (`onX={() => ...}`), so the tag ends at
 * the first `>` outside braces, not the first `>`.
 */
function chatInputMounts(src: string): string[] {
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const mounts: string[] = []
  const re = /<ChatInput\b/g
  for (let m = re.exec(code); m; m = re.exec(code)) {
    let depth = 0
    let i = m.index
    for (; i < code.length; i++) {
      const c = code[i]
      if (c === '{') depth++
      else if (c === '}') depth--
      else if (c === '>' && depth === 0) break
    }
    mounts.push(code.slice(m.index, i + 1))
  }
  return mounts
}

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
      mounts.filter(tag => !/\blexicalComposer\b/.test(tag)).map(tag => `${f.slice(SRC.length + 1)}: ${tag.replace(/\s+/g, ' ').slice(0, 80)}…`))
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
      const hookCalled = /\buseTouchDeviceAtMount\(\)/.test(src) && !/\buseIsTouchDevice\(\)/.test(src)
      return mounts
        .filter(tag => !hookCalled || !/\blexicalComposer=\{!touchDevice\}/.test(tag))
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
})
