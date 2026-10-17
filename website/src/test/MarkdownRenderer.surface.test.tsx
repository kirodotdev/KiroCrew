import React, { useContext } from 'react'
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import * as facade from '../components/MarkdownRenderer'
import * as contexts from '../components/markdown/contexts'
import * as linkTargets from '../components/markdown/linkTargets'
import * as pathReferences from '../components/markdown/pathReferences'
import * as mermaidBlock from '../components/markdown/MermaidBlock'
import * as copyFeedback from '../components/markdown/copyFeedback'
import * as images from '../components/markdown/ImgWithFallback'
import * as sanitize from '../components/markdown/sanitize'
import * as treeTransforms from '../components/markdown/treeTransforms'
import * as lightbox from '../components/markdown/Lightbox'

/**
 * The module's public surface and its sanitizer schema, pinned as data.
 *
 * About 150 specs mock this module by path and six modules import its named
 * exports, so its export set, the identity and default value of each
 * exported context, and the default export's memo wrapper are contracts in
 * their own right, independent of any rendered output.
 *
 * The sanitizer is the security boundary for model-authored HTML. Its allowlist
 * is pinned here as the verdict it reaches on every tag and a broad attribute
 * set, so a table edit anywhere in the pipeline shows up as a diff of this
 * matrix rather than as a changed rendering somewhere downstream.
 */

describe('MarkdownRenderer public surface', () => {
  it('exports exactly this set of named values', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'BasePathCtx',
      'COPIED_FLASH_MS',
      'COPY_FAILED_FLASH_MS',
      'CompactImagesCtx',
      'ImageVersionCtx',
      'Lightbox',
      'LinkOverrideCtx',
      'LinkUnfurlCtx',
      'MERMAID_FONTS_READY_CAP_MS',
      'MdSourceCtx',
      'artifactSlugFromHref',
      'default',
      'dispatchLightbox',
      'fixCjkAutolinkBoundaries',
      'fixCodeFences',
      'fixUnencodedLinkDestinations',
      'isPathCandidate',
      'pendingImageBoxStyle',
      'rehypeSanitize',
      'rehypeStableRootKeys',
      'remarkVerbatimUnknownTags',
      'reservedImageClass',
      'reservedImageStyle',
      'soleLinkInParagraph',
      'splitLineRef',
      'unfurlableHref',
    ])
  })

  it('default-exports the renderer wrapped in memo', () => {
    const component = facade.default as unknown as { $$typeof: symbol; type: { name: string } }
    expect(component.$$typeof).toBe(Symbol.for('react.memo'))
    expect(component.type.name).toBe('MarkdownRenderer')
  })

  it('keeps each exported timing constant', () => {
    expect(facade.MERMAID_FONTS_READY_CAP_MS).toBe(2500)
    expect(facade.COPIED_FLASH_MS).toBe(1500)
    expect(facade.COPY_FAILED_FLASH_MS).toBe(3000)
  })

  it('gives every exported context its documented default', () => {
    const seen: Record<string, unknown> = {}
    function Probe() {
      seen.basePath = useContext(facade.BasePathCtx)
      seen.compact = useContext(facade.CompactImagesCtx)
      seen.version = useContext(facade.ImageVersionCtx)
      seen.source = useContext(facade.MdSourceCtx)
      seen.override = useContext(facade.LinkOverrideCtx)
      seen.unfurl = useContext(facade.LinkUnfurlCtx)
      return null
    }
    render(<Probe />)
    expect(seen).toEqual({
      basePath: null,
      compact: false,
      version: null,
      source: null,
      override: null,
      unfurl: { enabled: false, live: false },
    })
  })

  it('re-exports each owner\'s own object, never a second copy', () => {
    const owners: Record<string, Record<string, unknown>> = {
      BasePathCtx: contexts, CompactImagesCtx: contexts, ImageVersionCtx: contexts, MdSourceCtx: contexts,
      LinkOverrideCtx: contexts, LinkUnfurlCtx: contexts,
      artifactSlugFromHref: linkTargets, soleLinkInParagraph: linkTargets, unfurlableHref: linkTargets,
      isPathCandidate: pathReferences, splitLineRef: pathReferences,
      MERMAID_FONTS_READY_CAP_MS: mermaidBlock,
      COPIED_FLASH_MS: copyFeedback, COPY_FAILED_FLASH_MS: copyFeedback,
      pendingImageBoxStyle: images, reservedImageClass: images, reservedImageStyle: images,
      rehypeSanitize: sanitize, remarkVerbatimUnknownTags: sanitize,
      rehypeStableRootKeys: treeTransforms,
      dispatchLightbox: lightbox, Lightbox: lightbox,
    }
    for (const [name, owner] of Object.entries(owners)) {
      expect(facade[name as keyof typeof facade], name).toBe(owner[name])
    }
  })

})

type Hast = { type: string; tagName?: string; value?: string; properties?: Record<string, unknown>; children?: Hast[] }

/** Tags the allowlist admits, plus a spread it must not. */
const TAGS = [
  'div', 'span', 'p', 'br', 'hr', 'h1', 'h6', 'ul', 'ol', 'li', 'strong', 'b', 'em', 'i', 'del', 's', 'u', 'mark',
  'small', 'sup', 'sub', 'kbd', 'abbr', 'cite', 'q', 'var', 'samp', 'code', 'pre', 'a', 'img', 'picture', 'source',
  'video', 'audio', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
  'blockquote', 'details', 'summary', 'figure', 'figcaption', 'section', 'article', 'header', 'footer', 'nav',
  'aside', 'time', 'input', 'dl', 'dt', 'dd', 'ruby', 'rt', 'rp', 'wbr', 'svg', 'path', 'circle', 'rect', 'line',
  'polyline', 'polygon', 'text', 'g', 'defs', 'use', 'tspan', 'ellipse', 'lineargradient', 'radialgradient', 'stop',
  'title', 'desc', 'clippath', 'marker', 'math', 'inlinemath',
  'script', 'style', 'iframe', 'object', 'embed', 'form', 'link', 'meta', 'base', 'noscript', 'textarea', 'button',
  'select', 'dialog', 'frame', 'template', 'customTag',
]

/** Every attribute any rule admits, plus the shapes the rules exist to drop. */
const ATTRS: Record<string, unknown> = {
  className: ['x'], id: 'i', title: 't', dir: 'ltr', lang: 'en', role: 'note', align: 'left',
  href: 'https://ok.example', name: 'n', target: '_blank', rel: 'noopener', src: 'https://ok.example/a.png', alt: 'a',
  width: '1', height: '2', loading: 'lazy', type: 'checkbox', checked: true, disabled: true, start: '2',
  reversed: true, value: '3', colSpan: 2, rowSpan: 2, headers: 'h', scope: 'col', span: 1, srcSet: 'a.png 1x',
  media: 'm', sizes: 's', controls: true, poster: 'https://ok.example/p.png', loop: true, muted: true,
  preload: 'none', open: true, dateTime: '2026-01-01', viewBox: '0 0 1 1', xmlns: 'http://www.w3.org/2000/svg',
  fill: 'none', stroke: 'red', strokeWidth: '1', d: 'M0 0', points: '0,0', x: '0', cx: '0', r: '1', offset: '0',
  stopColor: 'red', transform: 'scale(1)', refX: '0', orient: 'auto', ariaLabel: 'l', dataFoo: 'f',
  dataMessageEdit: 'reserved', 'data-message-actions': 'reserved', style: 'color:red', onClick: 'x()',
  onerror: 'x()', formAction: 'x', srcDoc: '<b>', background: 'x', ping: 'x', xlinkHref: 'x',
}

function sanitizedAttrs(tag: string, props: Record<string, unknown>): string {
  const tree: Hast = { type: 'root', children: [{ type: 'element', tagName: tag, properties: { ...props }, children: [] }] }
  ;(facade.rehypeSanitize() as unknown as (t: Hast) => void)(tree)
  const out = tree.children ?? []
  if (out.length === 0) return 'dropped'
  const node = out[0]
  if (node.tagName !== tag) return `-> ${node.tagName}.${(node.properties?.className as string[] | undefined)?.join('.') ?? ''}`
  return Object.keys(node.properties ?? {}).sort().join(' ') || '(none)'
}

describe('MarkdownRenderer sanitizer schema', () => {
  it('reaches this verdict for every tag over a broad attribute set', () => {
    const verdicts = TAGS.map(tag => `${tag}: ${sanitizedAttrs(tag, ATTRS)}`).join('\n')
    expect(verdicts).toMatchInlineSnapshot(`
      "div: align ariaLabel className dataFoo dir id lang role title
      span: align ariaLabel className dataFoo dir id lang role title
      p: align ariaLabel className dataFoo dir id lang role title
      br: align ariaLabel className dataFoo dir id lang role title
      hr: align ariaLabel className dataFoo dir id lang role title
      h1: align ariaLabel className dataFoo dir id lang role title
      h6: align ariaLabel className dataFoo dir id lang role title
      ul: align ariaLabel className dataFoo dir id lang role title
      ol: align ariaLabel className dataFoo dir id lang reversed role start title type
      li: align ariaLabel className dataFoo dir id lang role title value
      strong: align ariaLabel className dataFoo dir id lang role title
      b: align ariaLabel className dataFoo dir id lang role title
      em: align ariaLabel className dataFoo dir id lang role title
      i: align ariaLabel className dataFoo dir id lang role title
      del: align ariaLabel className dataFoo dir id lang role title
      s: align ariaLabel className dataFoo dir id lang role title
      u: align ariaLabel className dataFoo dir id lang role title
      mark: align ariaLabel className dataFoo dir id lang role title
      small: align ariaLabel className dataFoo dir id lang role title
      sup: align ariaLabel className dataFoo dir id lang role title
      sub: align ariaLabel className dataFoo dir id lang role title
      kbd: align ariaLabel className dataFoo dir id lang role title
      abbr: align ariaLabel className dataFoo dir id lang role title
      cite: align ariaLabel className dataFoo dir id lang role title
      q: align ariaLabel className dataFoo dir id lang role title
      var: align ariaLabel className dataFoo dir id lang role title
      samp: align ariaLabel className dataFoo dir id lang role title
      code: align ariaLabel className dataFoo dir id lang role title
      pre: align ariaLabel className dataFoo dir id lang role title
      a: align ariaLabel className dataFoo dir href id lang name rel role target title
      img: align alt ariaLabel className dataFoo dir height id lang loading role src title width
      picture: align ariaLabel className dataFoo dir id lang role title
      source: align ariaLabel className dataFoo dir id lang media role sizes src srcSet title type
      video: align ariaLabel className controls dataFoo dir height id lang loop muted poster preload role src title width
      audio: align ariaLabel className controls dataFoo dir id lang loop muted preload role src title
      table: align ariaLabel className dataFoo dir id lang role title
      thead: align ariaLabel className dataFoo dir id lang role title
      tbody: align ariaLabel className dataFoo dir id lang role title
      tfoot: align ariaLabel className dataFoo dir id lang role title
      tr: align ariaLabel className dataFoo dir id lang role title
      th: align ariaLabel className colSpan dataFoo dir headers id lang role rowSpan scope title
      td: align ariaLabel className colSpan dataFoo dir headers id lang role rowSpan title
      caption: align ariaLabel className dataFoo dir id lang role title
      colgroup: align ariaLabel className dataFoo dir id lang role span title width
      col: align ariaLabel className dataFoo dir id lang role span title width
      blockquote: align ariaLabel className dataFoo dir id lang role title
      details: align ariaLabel className dataFoo dir id lang open role title
      summary: align ariaLabel className dataFoo dir id lang role title
      figure: align ariaLabel className dataFoo dir id lang role title
      figcaption: align ariaLabel className dataFoo dir id lang role title
      section: align ariaLabel className dataFoo dir id lang role title
      article: align ariaLabel className dataFoo dir id lang role title
      header: align ariaLabel className dataFoo dir id lang role title
      footer: align ariaLabel className dataFoo dir id lang role title
      nav: align ariaLabel className dataFoo dir id lang role title
      aside: align ariaLabel className dataFoo dir id lang role title
      time: align ariaLabel className dataFoo dateTime dir id lang role title
      input: checked disabled type
      dl: align ariaLabel className dataFoo dir id lang role title
      dt: align ariaLabel className dataFoo dir id lang role title
      dd: align ariaLabel className dataFoo dir id lang role title
      ruby: align ariaLabel className dataFoo dir id lang role title
      rt: align ariaLabel className dataFoo dir id lang role title
      rp: align ariaLabel className dataFoo dir id lang role title
      wbr: align ariaLabel className dataFoo dir id lang role title
      svg: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      path: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      circle: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      rect: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      line: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      polyline: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      polygon: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      text: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      g: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      defs: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      use: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      tspan: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      ellipse: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      lineargradient: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      radialgradient: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      stop: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      title: align ariaLabel className dataFoo dir id lang role title
      desc: align ariaLabel className dataFoo dir id lang role title
      clippath: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      marker: align ariaLabel className cx d dataFoo dir fill height id lang offset orient points r refX role stopColor stroke strokeWidth title transform viewBox width x xmlns
      math: align ariaLabel className dataFoo dir id lang role title
      inlinemath: align ariaLabel className dataFoo dir id lang role title
      script: -> span.escaped-tag
      style: -> span.escaped-tag
      iframe: -> span.escaped-tag
      object: -> span.escaped-tag
      embed: -> span.escaped-tag
      form: -> span.escaped-tag
      link: -> span.escaped-tag
      meta: -> span.escaped-tag
      base: -> span.escaped-tag
      noscript: -> span.escaped-tag
      textarea: -> span.escaped-tag
      button: -> span.escaped-tag
      select: -> span.escaped-tag
      dialog: -> span.escaped-tag
      frame: -> span.escaped-tag
      template: -> span.escaped-tag
      customTag: -> span.escaped-tag"
    `)
  })

  it('drops dangerous protocols from every retained URL attribute, but keeps inline images', () => {
    const cases: Array<[string, Record<string, unknown>]> = [
      ['a', { href: 'javascript:alert(1)' }],
      ['a', { href: ' JaVaScRiPt:alert(1)' }],
      ['a', { href: 'java\u0000script:alert(1)' }],
      ['a', { href: 'vbscript:x' }],
      ['a', { href: 'data:text/html,x' }],
      ['img', { src: 'data:image/png;base64,AAAA' }],
      ['img', { src: 'data:text/html,x' }],
      ['video', { src: 'javascript:x', poster: 'data:image/png;base64,AAAA' }],
      ['source', { src: 'vbscript:x', srcSet: 'ok.png' }],
    ]
    expect(cases.map(([tag, props]) => `${tag} ${JSON.stringify(props)}: ${sanitizedAttrs(tag, props)}`).join('\n')).toMatchInlineSnapshot(`
      "a {"href":"javascript:alert(1)"}: (none)
      a {"href":" JaVaScRiPt:alert(1)"}: (none)
      a {"href":"java\\u0000script:alert(1)"}: (none)
      a {"href":"vbscript:x"}: (none)
      a {"href":"data:text/html,x"}: (none)
      img {"src":"data:image/png;base64,AAAA"}: src
      img {"src":"data:text/html,x"}: (none)
      video {"src":"javascript:x","poster":"data:image/png;base64,AAAA"}: (none)
      source {"src":"vbscript:x","srcSet":"ok.png"}: srcSet"
    `)
  })

  it('reduces every input to a disabled checkbox or nothing', () => {
    expect([
      sanitizedAttrs('input', { type: 'checkbox', checked: true, name: 'x', onChange: 'y' }),
      sanitizedAttrs('input', { type: 'text', value: 'x' }),
      sanitizedAttrs('input', {}),
    ]).toEqual(['checked disabled type', 'dropped', 'dropped'])
  })
})
