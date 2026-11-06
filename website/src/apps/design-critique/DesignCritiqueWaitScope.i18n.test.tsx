/**
 * Design Critique's waiting screen and scoping picker under a non-English language.
 *
 * The wait title glued a screen count to English ("Uploading 3 screens…"), the
 * footer and placeholder were English, and the picker glued its found, picked
 * and flow counts to English nouns. These mount the real components in Russian
 * and read what a user would read; each expected value comes from the catalog
 * and is checked against English first, so a missing key cannot pass by falling
 * back.
 */
import fs from 'node:fs'
import path from 'node:path'
import { afterAll, afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../../i18n/all'
import WaitingScreen from './WaitingScreen'
import ScopingPicker from './ScopingPicker'
import type { Scope, Screen } from './types'

const K = 'apps.designCritique.'
const SRC = path.resolve(__dirname, '..', '..')

/** The `lng` value of `key`, guarded so a missing translation fails here. */
function tr(lng: string, key: string, opts?: Record<string, unknown>): string {
  const en = i18next.getFixedT('en')(K + key, opts) as string
  const value = i18next.getFixedT(lng)(K + key, opts) as string
  expect(value).not.toBe(en)
  expect(value).not.toBe(K + key)
  return value
}

const shots = (n: number): Screen[] =>
  Array.from({ length: n }, (_, i) => ({ step: i + 1, label: 'S' + i, url: '/api/file-raw?path=%2Ftmp%2F' + i + '.png' }))

const noop = () => {}

function wait(phase: 'uploading' | 'analyzing', n: number, elapsed = 0) {
  return render(
    <WaitingScreen phase={phase} elapsed={elapsed} writing={false} reduceMotion screens={shots(n)} pendingKind={null} onCancel={noop} />,
  )
}

function picker(picked: string[]) {
  const scope = {
    framework: '',
    note: '',
    flows: [{ label: '', screenIds: ['a', 'b'] }],
    screens: [
      { id: 'a', label: 'Home', canSee: true },
      { id: 'b', label: 'Cart', canSee: true },
      { id: 'c', label: 'Admin', canSee: false },
      { id: 'd', label: 'Pay', canSee: true },
      { id: 'e', label: 'Done', canSee: true },
    ],
  } as unknown as Scope
  return render(
    <ScopingPicker scope={scope} picked={picked} refBrief="" dragId={null} togglePick={noop} dropPickAt={noop}
      movePick={noop} useFlow={noop} setDragId={noop} setRefBrief={noop} runScoped={noop} onStartOver={noop} />,
  )
}

afterEach(async () => {
  cleanup()
  await i18next.changeLanguage('en')
})
afterAll(async () => { await i18next.changeLanguage('en') })

describe('Design Critique waiting screen and scoping picker text', () => {
  it('puts the screen count inside the Russian wait title', async () => {
    await i18next.changeLanguage('ru')
    wait('uploading', 5)
    expect(screen.getByText('Загружаю 5 экранов…')).toBeInTheDocument()
    cleanup()
    wait('analyzing', 3)
    expect(screen.getByText('Читаю сценарий · 3 экрана')).toBeInTheDocument()
    expect(screen.getByText(tr('ru', 'waitingScreen.foot_navigate_away'))).toBeInTheDocument()
  })

  it('counts the picker screens with the Russian plural forms', async () => {
    await i18next.changeLanguage('ru')
    picker(['a', 'b'])
    expect(screen.getByText('Найдено 5 экранов · отрисовать могу 4')).toBeInTheDocument()
    expect(screen.getByText('· выбрано 2')).toBeInTheDocument()
    expect(screen.getByText(tr('ru', 'scopingPicker.flow_n', { n: '1' }))).toBeInTheDocument()
    expect(screen.getByText(tr('ru', 'scopingPicker.hint_order'))).toBeInTheDocument()
  })

  it('has every new line in each shipped language', () => {
    const keys = ['waitingScreen.getting_screens_ready', 'waitingScreen.title_scanning', 'waitingScreen.title_uploading_one',
      'waitingScreen.title_reading_screen', 'waitingScreen.title_getting_pixels', 'waitingScreen.foot_still_going',
      'waitingScreen.foot_navigate_away', 'scopingPicker.hint_order', 'scopingPicker.hint_pick',
      'scopingPicker.cant_render_title', 'scopingPicker.cant_render_short']
    for (const lng of ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']) {
      for (const key of keys) tr(lng, key)
      tr(lng, 'scopingPicker.flow_n', { n: '2' })
      tr(lng, 'waitingScreen.title_uploading_screens', { count: 3, n: '3' })
      tr(lng, 'scopingPicker.screens_found_count', { count: 3, n: '3', m: '2' })
    }
  })

  it.each([
    ['apps/design-critique/WaitingScreen.tsx', "'Uploading ' + screens.length"],
    ['apps/design-critique/WaitingScreen.tsx', "'Reading your flow · '"],
    ['apps/design-critique/WaitingScreen.tsx', "'You can navigate away — the critique keeps running.'"],
    ['apps/design-critique/WaitingScreen.tsx', "'Getting the screens ready…'"],
    ['apps/design-critique/ScopingPicker.tsx', "' screens found · '"],
    ['apps/design-critique/ScopingPicker.tsx', "' picked'"],
    ['apps/design-critique/ScopingPicker.tsx', "'Flow ' + (i + 1)"],
    ['apps/design-critique/ScopingPicker.tsx', "'Click a screen to pick it."],
    ['apps/design-critique/ScopingPicker.tsx', "'can’t render'"],
  ])('%s takes its text from the catalog (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
