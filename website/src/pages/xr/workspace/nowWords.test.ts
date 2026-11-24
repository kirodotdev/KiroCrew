import { describe, expect, it } from 'vitest'
import { nowWords } from './useSays'

const m = (role: string, content: string, ts = '2026-11-23T14:02:00Z') => ({ role, content, cls: '', ts }) as never

describe('a working Bot\'s own words (M44)', () => {
  it('takes the newest assistant row of the running turn, as prose', () => {
    expect(nowWords([m('user', 'Look at checkout.'), m('assistant', 'Yes.'), m('assistant', 'Reading the incident timeline now.')]).own).toBe('Reading the incident timeline now.')
  })
  it('leaves out a code fence, a heading, a table and an options line', () => {
    const fence = nowWords([m('user', 'Go.'), m('assistant', '```bash\nrm -rf build\n```\nCleaning the build folder.')])
    expect(fence.own).toBe('Cleaning the build folder.')
    const heading = nowWords([m('user', 'Go.'), m('assistant', '## Plan\nFirst I read the logs.')])
    expect(heading.own.startsWith('#')).toBe(false)
    expect(heading.own).toContain('First I read the logs.')
    const table = nowWords([m('user', 'Go.'), m('assistant', '| a | b |\n|---|---|\n| 1 | 2 |\n\nTwo rows differ.')])
    expect(table.own).toBe('Two rows differ.')
    const options = nowWords([m('user', 'Go.'), m('assistant', 'Which region goes first?\n[OPTIONS: Oregon | Tokyo]')])
    expect(options.own).not.toContain('[OPTIONS')
  })
  it('splits the body off after the first sentence, with a mark or an underscore inside it (M48)', () => {
    const a = nowWords([m('user', 'Go.'), m('assistant', 'I ran work_report first. Then the tests.')]).own.split('\n')
    expect(a).toEqual(['I ran work_report first.', 'Then the tests.'])
    const b = nowWords([m('user', 'Go.'), m('assistant', 'The *second* shard is red. I am reading its log.')]).own.split('\n')
    expect(b).toEqual(['The second shard is red.', 'I am reading its log.'])
    const long = `${'word '.repeat(40).trim()}. After it.`
    const c = nowWords([m('user', 'Go.'), m('assistant', long)]).own.split('\n')
    expect([...c[0]].length).toBeLessThanOrEqual(141)
    expect(c[1]).toBe('After it.')
  })
  it('says what started the turn only for plain words with a time that reads', () => {
    expect(nowWords([m('user', 'Can you look at checkout?')]).started).toEqual({ ts: '2026-11-23T14:02:00Z', line: 'Can you look at checkout?' })
    expect(nowWords([m('user', '[Cron notification from "nightly"] run the sweep')]).started).toBeNull()
    expect(nowWords([m('user', 'Look.', 'not a time')]).started).toBeNull()
    expect(nowWords([m('user', 'Look.', '')]).started).toBeNull()
  })
})
