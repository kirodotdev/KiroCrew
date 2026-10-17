import { describe, it, expect } from 'vitest'
import { buildSrcdoc, resolveWidgetTheme, THEME_VAR_NAMES } from '../lib/widgetSrcdoc'

// The shape that shipped unreadable: light Tailwind card backgrounds, no text
// color, no theme vars. On a dark dashboard the body's themed light `--text`
// is inherited onto an off-white card -- white on white.
const LIGHT_CARDS =
  '<div class="grid gap-2">' +
  '<div class="rounded bg-green-50 border-l-4 border-green-600 p-3">#9098 drag hook</div>' +
  '<div class="rounded bg-orange-50 border-l-4 border-orange-500 p-3">#8916 GPT lane</div>' +
  '</div>'

const DARK_VARS = { '--bg': '#1a1a2e', '--text': '#e5e7eb', '--card': '#23233a' }

// The detector is module-private; its only caller is resolveWidgetTheme, and a
// hit is observable as the mode flipping to light on a dark dashboard.
const isLightCanvasAuthored = (html: string): boolean =>
  resolveWidgetTheme(html, DARK_VARS, 'dark').mode === 'light'

describe('light-canvas detection (through resolveWidgetTheme)', () => {
  it('flags light Tailwind background classes with no theme awareness', () => {
    expect(isLightCanvasAuthored(LIGHT_CARDS)).toBe(true)
    expect(isLightCanvasAuthored('<div class="bg-white p-2">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div class="hover:bg-slate-100">x</div>')).toBe(true)
  })

  it.each([
    'min-[300px]:',
    'max-[600px]:',
    'supports-[display:grid]:',
    '[&:hover]:',
    'data-[state=open]:',
    'group-[.x]:',
    '@md:',
    '*:',
    '**:',
    '2xl:',
    'min-[300px]:supports-[display:grid]:hover:',
  ])('flags light backgrounds with the %s variant prefix', (prefix) => {
    for (const background of ['bg-white', 'bg-slate-100', 'bg-[#f0fdf4]']) {
      expect(isLightCanvasAuthored(`<div class="${prefix}${background}">x</div>`)).toBe(true)
    }
  })

  it.each([
    'min-[300px]:bg-gray-800',
    'data-[state=open]:bg-[#1f2937]',
    'xbg-white',
    'xbg-[#f0fdf4]',
    '!min-[300px]:bg-white',
    '!data-[state=open]:bg-[#f0fdf4]',
  ])('ignores dark backgrounds or missing token boundaries: %s', (className) => {
    expect(isLightCanvasAuthored(`<div class="${className}">x</div>`)).toBe(false)
  })

  it('flags Tailwind arbitrary-value light backgrounds by luminance', () => {
    expect(isLightCanvasAuthored('<div class="bg-[#f0fdf4] p-2">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div class="bg-[rgb(250,250,250)]">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div class="bg-[rgb(250_250_250)]">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div class="bg-[white]">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div class="bg-[#1f2937]">x</div>')).toBe(false)
  })

  it('flags inline light background literals by luminance', () => {
    expect(isLightCanvasAuthored('<div style="background:#f0fdf4">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div style="background-color: #FFF">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div style="background: rgb(250, 250, 250)">x</div>')).toBe(true)
    expect(isLightCanvasAuthored('<div style="background:white">x</div>')).toBe(true)
  })

  it('ignores dark hardcoded backgrounds', () => {
    expect(isLightCanvasAuthored('<div style="background:#1f2937">x</div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-gray-800 text-white">x</div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-green-600">x</div>')).toBe(false)
  })

  it('stands down when the author uses theme vars -- they own the contract', () => {
    // Even a half-set pair with a light class elsewhere: the author reached for
    // the theme, so the frame must not override the user's palette.
    const themed = '<div class="bg-green-50" style="color:var(--card-fg)">x</div>'
    expect(isLightCanvasAuthored(themed)).toBe(false)
    expect(isLightCanvasAuthored('<div style="background:var( --card )">x</div>')).toBe(false)
  })

  it('stands down on a mixed palette: a hardcoded dark background beside the light one', () => {
    // Flipping this wholesale to light would only trade which half is unreadable.
    expect(isLightCanvasAuthored('<div class="bg-gray-800">h</div><div class="bg-white">c</div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-black"><div class="bg-slate-100">c</div></div>')).toBe(false)
    expect(isLightCanvasAuthored('<div style="background:#1f2937"><div style="background:#fff">c</div></div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-[#111827]"><div class="bg-[#f0fdf4]">c</div></div>')).toBe(false)
    // A mid-tone accent (a coloured border or button) is not a dark canvas.
    expect(isLightCanvasAuthored('<div class="bg-white"><span class="bg-green-600">ok</span></div>')).toBe(true)
  })

  it('stands down when the author wrote a dark: variant', () => {
    expect(isLightCanvasAuthored('<div class="bg-white dark:bg-gray-900">x</div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-white md:dark:bg-gray-900">x</div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-white group-hover:dark:bg-black">x</div>')).toBe(false)
    expect(isLightCanvasAuthored('<div class="bg-white [&:hover]:dark:bg-gray-900">x</div>')).toBe(false)
    // No token boundary before `dark:`, so it is not a variant; the light class still fires.
    expect(isLightCanvasAuthored('<div class="bg-white xdark:foo">x</div>')).toBe(true)
    // A dark branch that is not a background: nothing else masks it, so this is
    // the case that proves the chained-prefix match itself.
    expect(isLightCanvasAuthored('<div class="bg-white md:dark:text-white">x</div>')).toBe(false)
  })

  it('does not fire on plain content or on colour words inside text', () => {
    expect(isLightCanvasAuthored('')).toBe(false)
    expect(isLightCanvasAuthored('<p>background: a white house</p>')).toBe(false)
    expect(isLightCanvasAuthored('<p>hello</p>')).toBe(false)
  })

  it('reads only class/style attributes and <style> blocks, never rendered text', () => {
    // A widget explaining Tailwind: the class name and declaration are prose.
    expect(isLightCanvasAuthored('<p>Use bg-white for cards, or background:#fff</p>')).toBe(false)
    // The same word as a real class attribute does fire.
    expect(isLightCanvasAuthored('<p>bg-white</p><div class="bg-white">x</div>')).toBe(true)
    // A light background declared in a stylesheet block counts.
    expect(isLightCanvasAuthored('<style>.card{background:#f0fdf4}</style><div class="card">x</div>')).toBe(true)
    // A prose mention of a theme var is not theme awareness; the light card still fires.
    expect(isLightCanvasAuthored('<p>never write var(--bg) here</p><div class="bg-white">x</div>')).toBe(true)
    // Single-quoted attributes are read too.
    expect(isLightCanvasAuthored("<div class='bg-slate-100'>x</div>")).toBe(true)
  })
})

describe('resolveWidgetTheme', () => {
  it('substitutes the light palette and light mode for a light-canvas widget on dark', () => {
    const r = resolveWidgetTheme(LIGHT_CARDS, DARK_VARS, 'dark')
    expect(r.mode).toBe('light')
    expect(r.themeVars['--bg']).toBe('#f3f4f6')
    expect(r.themeVars['--text']).toBe('#111827')
  })

  it('passes a light dashboard through untouched', () => {
    const light = { '--bg': '#fafafa', '--text': '#222' }
    const r = resolveWidgetTheme(LIGHT_CARDS, light, 'light')
    expect(r.mode).toBe('light')
    expect(r.themeVars).toBe(light)
  })

  it('passes a theme-aware widget on dark through untouched', () => {
    const html = '<div style="background:var(--card);color:var(--card-fg)">x</div>'
    const r = resolveWidgetTheme(html, DARK_VARS, 'dark')
    expect(r.mode).toBe('dark')
    expect(r.themeVars).toBe(DARK_VARS)
  })

  it('covers every exposed theme var so a stray var(--x) still resolves', () => {
    // Start from an EMPTY theme: every name must come from the fallback itself.
    const r = resolveWidgetTheme(LIGHT_CARDS, {}, 'dark')
    for (const name of THEME_VAR_NAMES) {
      expect(r.themeVars[name], name).toMatch(/^#[0-9a-f]{6}$/)
    }
  })
})

describe('buildSrcdoc light-canvas fallback', () => {
  it('renders a light-canvas widget on a dark dashboard as a light island', () => {
    const out = buildSrcdoc({ html: LIGHT_CARDS, themeVars: DARK_VARS, mode: 'dark' })
    expect(out).toContain('--bg:#f3f4f6')
    expect(out).toContain('--text:#111827')
    expect(out).toContain('color-scheme:light')
    expect(out).toContain('<body class="light">')
    expect(out).not.toContain('--text:#e5e7eb')
  })

  it('leaves a theme-aware widget on a dark dashboard dark', () => {
    const html = '<div style="background:var(--card);color:var(--card-fg)">x</div>'
    const out = buildSrcdoc({ html, themeVars: DARK_VARS, mode: 'dark' })
    expect(out).toContain('--text:#e5e7eb')
    expect(out).toContain('color-scheme:dark')
    expect(out).toContain('<body class="dark">')
  })

  it('never touches a light dashboard', () => {
    const out = buildSrcdoc({ html: LIGHT_CARDS, themeVars: { '--bg': '#fafafa' }, mode: 'light' })
    expect(out).toContain('--bg:#fafafa')
    expect(out).not.toContain('--bg:#f3f4f6')
  })
})
