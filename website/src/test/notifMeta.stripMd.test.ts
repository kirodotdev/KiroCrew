/**
 * Contract for the shared markdown flattener behind every plain-text preview:
 * the native OS notification banner (`useNativeNotification`), the feed-row
 * excerpt (`NotificationFeed`) and the transcript turn minimap.
 *
 * Flattening by deleting every `* _ ~ ` # >` character rewrote text that was
 * never markdown: an approval body carries the shell command a user is being
 * asked to authorize, and `rm -rf build/*` displayed as `rm -rf build/`. So the
 * helper unwraps delimiters only where they PAIR, and keeps the contents of a
 * code span or fence literal — which is what lets a producer mark raw text as
 * code and have every preview show it verbatim.
 */
import { describe, expect, it } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import { stripMd } from '../components/notifications/notifMeta'

function expectLiteralCode(input: string, expected: string) {
  if (!expected.includes('\n')) return
  const code = unified().use(remarkParse).parse(input).children.find(node => node.type === 'code')
  expect(code).toBeDefined()
  expect(expected).toContain(code?.value)
}

describe('stripMd', () => {
  it.each([
    ['undefined', undefined],
    ['null', null],
    ['a number', 42],
    ['an object', { raw: '**x**' }],
    ['an array', ['**x**']],
  ])('returns an empty string for %s -- a persisted body is untrusted input', (_label, value) => {
    expect(stripMd(value as unknown as string)).toBe('')
  })

  it.each([
    ['lone glob', 'rm -rf build/*'],
    ['home path', '~/.ssh/id_rsa'],
    ['redirect', 'echo hi > out.log'],
    ['identifier', 'my_file_name'],
    ['language name', 'C#'],
    ['multiple globs', 'cp build/* output/*'],
    ['unpaired markers', 'literal * _ ~ ` # >'],
    ['inline code', '`rm -rf build/*; echo **hi** > my_file_name; ls ~/.ssh`'],
    ['fenced code', '```sh\nrm -rf build/*; echo **hi** > my_file_name; ls ~/.ssh\n```'],
  ])('preserves %s alongside formatted prose', (_label, command) => {
    const literalCommand = command.startsWith('```')
      ? command.slice('```sh\n'.length, -'\n```'.length)
      : command.startsWith('`') ? command.slice(1, -1) : command
    expect(stripMd(`**Source:** agent\n\n${command}\n\ncleaning the build tree`))
      .toBe(`Source: agent · ${literalCommand} · cleaning the build tree`)
  })

  it.each([
    ['**auto/verify-python-package-contains-source** -- Verify a built wheel', 'auto/verify-python-package-contains-source -- Verify a built wheel'],
    ['**Triggers:** after a wheel build', 'Triggers: after a wheel build'],
    ['_Bundles executable scripts -- review them before approving._', 'Bundles executable scripts -- review them before approving.'],
    ['**__**', ''],
    ['**bold** __bold__ *italic* _italic_ ~~deleted~~ `code`', 'bold bold italic italic deleted code'],
    ['# Heading\n> quote\n- item\n+ item\n* item\n1. item', 'Heading quote item item item item'],
    ['* item\n*emphasis* text\n**bold** text\n*lone star', 'item emphasis text bold text *lone star'],
    ['![alt](image.png) [label](https://example.test)', 'alt label'],
    ['```sh\n# comment\necho `cmd` > file_name\n```', '# comment\necho `cmd` > file_name'],
    ['`echo hi` > out.log', 'echo hi > out.log'],
    ['`value` # suffix\n> quote', 'value # suffix quote'],
  ])('flattens supported formatting in %s', (input, expected) => {
    expectLiteralCode(input, expected)
    expect(stripMd(input)).toBe(expected)
  })
})

describe('stripMd paragraphs', () => {
  it.each([
    ['approval label, command and purpose', '**Source:** agent\n\n```\nls ~/.ssh/id_rsa\n```\n\nclearing the build cache', 'Source: agent · ls ~/.ssh/id_rsa · clearing the build cache'],
    ['multi-paragraph skill body', '**Triggers:** after a wheel build\n\nBundles executable scripts.\n\nReview them before approving.', 'Triggers: after a wheel build · Bundles executable scripts. · Review them before approving.'],
    ['single newline is a space', 'first line\nsecond line', 'first line second line'],
    ['blank lines with whitespace and CRLF', 'first\r\n \t\r\n\r\nsecond', 'first · second'],
    ['leading and trailing breaks', '\n\nfirst\n\nsecond\n\n', 'first · second'],
    ['empty paragraphs never double the separator', 'first\n\n\n\n**__**\n\n\n\nsecond', 'first · second'],
    ['empty code between breaks never doubles the separator', 'first\n\n```\n```\n\nsecond', 'first · second'],
    ['code keeps its blank lines literal', '```sh\nrm -rf *cache*\n\necho done\n```', 'rm -rf *cache*\n\necho done'],
    ['breaks around a fence separate, breaks inside do not', 'before\n\n```\none\n\ntwo\n```\n\nafter', 'before · one\n\ntwo · after'],
    ['fence adjacent to prose is not a break', 'before\n```\ncode\n```\nafter', 'before code after'],
  ])('separates %s', (_label, input, expected) => {
    expectLiteralCode(input, expected)
    expect(stripMd(input)).toBe(expected)
  })
})

describe('stripMd fence lengths', () => {
  it.each([
    ['four-backtick fence', '````sh\nrm -rf *cache*; echo ``` _tmp_\n````', 'rm -rf *cache*; echo ``` _tmp_'],
    ['longer closing fence', '````\n~~name~~\n`````', '~~name~~'],
    ['shorter run on its own line', '````\nbefore\n```\n*cache*\n````', 'before\n```\n*cache*'],
    ['five-backtick fence', '`````\necho ```` *cache*\n`````', 'echo ```` *cache*'],
    ['indented CRLF fence', '  ````sh\r\n_tmp_\r\n  `````  \r\n**after**', '_tmp_ after'],
    ['multiple fences and inline code', '````\n*one*\n````\n**middle** `two`\n```\n_three_\n```', '*one* middle two _three_'],
  ])('respects the opening run: %s', (_label, input, expected) => {
    expectLiteralCode(input, expected)
    expect(stripMd(input)).toBe(expected)
  })
})

describe('stripMd literal whitespace', () => {
  it('keeps separate commands on their own lines in an approval body', () => {
    const command = 'echo safe\nrm -rf target'
    const body = '**Source:** agent\n\n```sh\n' + command + '\n```\n\nReview this'
    const code = unified().use(remarkParse).parse(body).children.find(node => node.type === 'code')
    expect(code?.value).toBe(command)
    const preview = stripMd(body)
    expect(preview).toBe('Source: agent · echo safe\nrm -rf target · Review this')
    expect(preview).toContain('echo safe\nrm -rf target')
    expect(preview.split('\n')).toEqual(['Source: agent · echo safe', 'rm -rf target · Review this'])
  })

  it.each([
    '  echo safe  \n\n\trm -rf target  ',
    '\n\necho safe\n\n',
    ' \t ',
    '',
  ])('preserves every code whitespace byte, excluding the fence wrapper: %j', command => {
    const input = '```sh\n' + command + '\n```'
    expect(unified().use(remarkParse).parse(input).children[0])
      .toMatchObject({ type: 'code', value: command })
    expect(stripMd(input)).toBe(command)
    const surrounded = '  **before**  \n\n' + input + '\n\n  **after**  '
    expect(stripMd(surrounded)).toBe(command ? 'before · ' + command + ' · after' : 'before · after')
  })

  it('keeps a single-line inline span literal while collapsing surrounding prose', () => {
    const input = '  Run  `echo  safe\t--flag`  now  '
    const tree = unified().use(remarkParse).parse(input)
    const paragraph = tree.children[0]
    expect(paragraph.type).toBe('paragraph')
    if (paragraph.type !== 'paragraph') throw new Error('Expected a paragraph')
    expect(paragraph.children.find(node => node.type === 'inlineCode'))
      .toMatchObject({ value: 'echo  safe\t--flag' })
    expect(stripMd(input)).toBe('Run echo  safe\t--flag now')
    expect(stripMd('`echo safe`')).toBe('echo safe')
  })
})
