import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { CrewmateExportChoices, CrewmateExportPreview } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'

const memberExportPreview = vi.fn()
const memberExportFile = vi.fn()
const memberExportLessonsDraft = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberExportPreview: (...a: unknown[]) => memberExportPreview(...a),
      memberExportFile: (...a: unknown[]) => memberExportFile(...a),
      memberExportLessonsDraft: (...a: unknown[]) => memberExportLessonsDraft(...a),
    },
  }
})

const downloadBlob = vi.fn()
vi.mock('../../utils/download', () => ({
  downloadBlob: (...a: unknown[]) => downloadBlob(...a),
}))

import CrewExportView from './CrewExportView'

/* The Export confirm view (rfc-crewmate-share.md §5.3): it lists what the file holds,
   crons on and lessons off by default, says what stays home, and sends exactly the
   toggles and the edited lessons text. */

const view = (over: Partial<CrewmateExportPreview> = {}): CrewmateExportPreview => ({
  name: 'Scout',
  filename: 'Scout.crewmate',
  description: 'Watches release branches.',
  model: 'claude-sonnet',
  dashboard: '',
  avatar: 'ghost',
  avatar_digest: 'avatar-shown',
  rules: 'Ask before pushing.',
  goals: { longTerm: 'Ship release notes', keyResults: [{ text: 'Notes out', target: 'weekly' }] },
  crons: [{ name: 'notes', cron_expr: '0 9 * * *', script: 'notes.py:run', source: 'def run():\n    pass\n' }],
  skills: [{ name: 'triage', lessons: false, files: { 'SKILL.md': '# Triage', 'helper.py': 'print(1)' } }],
  mcp_servers: [{ server: 'gh', placeholders: ['env.GITHUB_TOKEN'] }],
  mcp_in_file: false,
  left_out: [
    { kind: 'dashboard', name: '', reason: 'custom_template' },
    { kind: 'cron', name: 'gone', reason: 'script_missing' },
  ],
  fingerprint: 'fp-shown',
  ...over,
})

/** Answer the preview the way the server plans: the first read lists everything, a
 *  read with choices holds exactly what those choices carry. */
const servePlan = (base: CrewmateExportPreview, extraLeftOut: CrewmateExportPreview['left_out'] = []) => {
  memberExportPreview.mockImplementation(async (_s: string, _m: string, c?: CrewmateExportChoices) => {
    if (!c) return base
    return {
      ...base,
      rules: c.rules ? base.rules : null,
      goals: c.goals ? base.goals : null,
      crons: c.crons ? base.crons : [],
      skills: [
        ...(c.lessons ? [{ name: 'scout-lessons', lessons: true, files: { 'SKILL.md': c.lessons } }] : []),
        ...base.skills.filter(s => c.skills?.includes(s.name)),
      ],
      left_out: [...base.left_out, ...extraLeftOut],
    }
  })
}

beforeEach(() => {
  memberExportPreview.mockReset()
  memberExportFile.mockReset()
  memberExportLessonsDraft.mockReset()
  memberExportLessonsDraft.mockResolvedValue({ text: '' })
  downloadBlob.mockReset()
})

describe('CrewExportView', () => {
  it('lists every part that leaves, in full, with crons on and lessons off', async () => {
    servePlan(view())
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    expect(memberExportPreview).toHaveBeenCalledWith('scout', 'scout')
    const crons = screen.getByTestId('crew-export-crons')
    expect(within(crons).getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    expect(within(crons).getByTestId('crew-export-cron-script').textContent).toBe('def run():\n    pass\n')
    const lessons = screen.getByTestId('crew-export-lessons')
    expect(within(lessons).getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByTestId('crew-export-lessons-editor')).toBeNull()
    expect(screen.getByTestId('crew-export-skill').textContent).toContain('print(1)')
    expect(screen.getByTestId('crew-export-dashboard').textContent).toContain('Not included (custom template)')
    expect(screen.getByTestId('crew-export-mcp').textContent).toContain('env.GITHUB_TOKEN')
    expect(screen.getByTestId('crew-export-left-out').textContent).toContain('script missing')
    expect(screen.getByTestId('crew-export-not-included').textContent).toBe(
      'Not included: memory, history, credentials and approval settings.',
    )
  })

  it('sends the toggles and only the lessons text left in the editor', async () => {
    servePlan(view())
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-rules')).getByRole('switch'))
    fireEvent.click(within(screen.getByTestId('crew-export-skill')).getByRole('switch'))
    fireEvent.click(within(screen.getByTestId('crew-export-lessons')).getByRole('switch'))
    fireEvent.change(screen.getByTestId('crew-export-lessons-editor'), { target: { value: '# Lessons\n- check first' } })
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(memberExportFile).toHaveBeenCalled())
    expect(memberExportFile).toHaveBeenCalledWith('scout', 'scout', {
      rules: false, goals: true, crons: true, skills: [], lessons: '# Lessons\n- check first', confirm: 'fp-shown',
    })
  })

  it('hands the file to the shared download helper so the desktop shell saves it', async () => {
    servePlan(view())
    const file = new Blob(['PK'])
    memberExportFile.mockResolvedValue(file)
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledTimes(1))
    expect(downloadBlob.mock.calls[0][0]).toBeInstanceOf(Blob)
    expect(downloadBlob).toHaveBeenCalledWith(file, 'Scout.crewmate')
  })

  it('sends no lessons when the toggle is on but the editor is empty', async () => {
    servePlan(view({ skills: [] }))
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-lessons')).getByRole('switch'))
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(memberExportFile).toHaveBeenCalled())
    expect(memberExportFile.mock.calls[0][2].lessons).toBeNull()
  })

  it('turning lessons on asks the crewmate for one draft, which the owner edits', async () => {
    servePlan(view({ skills: [] }))
    memberExportLessonsDraft.mockResolvedValue({ text: '# Lessons\n- drafted' })
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-lessons')).getByRole('switch'))
    await waitFor(() => expect((screen.getByTestId('crew-export-lessons-editor') as HTMLTextAreaElement).value).toBe('# Lessons\n- drafted'))
    expect(memberExportLessonsDraft).toHaveBeenCalledTimes(1)
    fireEvent.change(screen.getByTestId('crew-export-lessons-editor'), { target: { value: '# Lessons\n- edited' } })
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(memberExportFile).toHaveBeenCalled())
    expect(memberExportFile.mock.calls[0][2].lessons).toBe('# Lessons\n- edited')
  })

  it('says so when the crewmate has no memory of its own, and keeps the editor', async () => {
    servePlan(view({ skills: [] }))
    memberExportLessonsDraft.mockRejectedValue(Object.assign(new Error('x'), { status: 409 }))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-lessons')).getByRole('switch'))
    expect((await screen.findByTestId('crew-export-lessons-draft-error')).textContent).toContain('no memory of its own')
    expect(screen.getByTestId('crew-export-lessons-editor')).not.toBeDisabled()
  })

  it('shows each schedule prompt that leaves', async () => {
    servePlan(view({ crons: [{ name: 'daily', cron_expr: '0 9 * * *', message: 'Summarise my inbox', timezone: 'Europe/Paris' }] }))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    const cron = await screen.findByTestId('crew-export-cron')
    expect(within(cron).getByTestId('crew-export-cron-message').textContent).toBe('Summarise my inbox')
    expect(cron.textContent).toContain('Europe/Paris')
  })

  it('stops the press when the plan for these choices no longer matches the list, then goes on', async () => {
    // With lessons on, the skill that took the last room is left out of the file.
    const base = view()
    memberExportPreview.mockImplementation(async (_s: string, _m: string, c?: CrewmateExportChoices) => (
      !c ? base : {
        ...base,
        skills: c.lessons ? [{ name: 'scout-lessons', lessons: true, files: {} }] : base.skills,
        left_out: c.lessons ? [...base.left_out, { kind: 'skill', name: 'triage', reason: 'bundle_full' }] : base.left_out,
      }
    ))
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-lessons')).getByRole('switch'))
    fireEvent.change(screen.getByTestId('crew-export-lessons-editor'), { target: { value: '# Lessons' } })
    fireEvent.click(screen.getByTestId('crew-export-download'))
    expect(await screen.findByTestId('crew-export-plan-changed')).toBeInTheDocument()
    expect(screen.getByTestId('crew-export-left-out').textContent).toContain('No room left in the file')
    expect(memberExportFile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(memberExportFile).toHaveBeenCalledTimes(1))
  })

  it('stops the press when a file changed after the view loaded', async () => {
    const base = view()
    const edited = { ...base, skills: [{ ...base.skills[0], files: { ...base.skills[0].files, 'helper.py': 'print(2)' } }] }
    memberExportPreview.mockImplementation(async (_s: string, _m: string, c?: CrewmateExportChoices) => (c ? edited : base))
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(screen.getByTestId('crew-export-download'))
    expect(await screen.findByTestId('crew-export-plan-changed')).toBeInTheDocument()
    expect(memberExportFile).not.toHaveBeenCalled()
    // The view now lists the changed file, and the next press ships exactly that.
    expect(screen.getByTestId('crew-export-skill').textContent).toContain('print(2)')
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(memberExportFile).toHaveBeenCalledTimes(1))
  })

  it.each(['image', 'ghost'] as const)('stops the press when the %s avatar content changed', async avatar => {
    const base = view({ avatar })
    const edited = { ...base, avatar_digest: 'avatar-edited', fingerprint: 'fp-edited' }
    memberExportPreview.mockImplementation(async (_s: string, _m: string, c?: CrewmateExportChoices) => (c ? edited : base))
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(screen.getByTestId('crew-export-download'))
    expect(await screen.findByTestId('crew-export-plan-changed')).toBeInTheDocument()
    expect(memberExportFile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('crew-export-download'))
    await waitFor(() => expect(memberExportFile).toHaveBeenCalledTimes(1))
    expect(memberExportFile.mock.calls[0][2].confirm).toBe('fp-edited')
  })

  it('binds the download to the checked plan and shows the new one when the server says it moved', async () => {
    servePlan(view())
    memberExportFile
      .mockRejectedValueOnce(Object.assign(new Error('plan_changed'), { status: 409 }))
      .mockResolvedValue(new Blob(['PK']))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(screen.getByTestId('crew-export-download'))
    expect(await screen.findByTestId('crew-export-plan-changed')).toBeInTheDocument()
    expect(memberExportFile.mock.calls[0][2].confirm).toBe('fp-shown')
    expect(screen.queryByTestId('crew-export-error')).toBeNull()
  })

  it('reports dirty while the lessons editor holds text', async () => {
    servePlan(view({ skills: [] }))
    const onDirtyChange = vi.fn()
    renderWithProviders(<CrewExportView slug="scout" member="scout" onDirtyChange={onDirtyChange} />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-lessons')).getByRole('switch'))
    fireEvent.change(screen.getByTestId('crew-export-lessons-editor'), { target: { value: 'x' } })
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true))
  })

  it('still reports dirty after the lessons toggle turns off, since the view keeps the text', async () => {
    servePlan(view({ skills: [] }))
    const onDirtyChange = vi.fn()
    renderWithProviders(<CrewExportView slug="scout" member="scout" onDirtyChange={onDirtyChange} />)
    await screen.findByTestId('crew-export-view')
    const toggle = () => within(screen.getByTestId('crew-export-lessons')).getByRole('switch')
    fireEvent.click(toggle())
    fireEvent.change(screen.getByTestId('crew-export-lessons-editor'), { target: { value: 'x' } })
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true))
    fireEvent.click(toggle())
    await waitFor(() => expect(screen.queryByTestId('crew-export-lessons-editor')).toBeNull())
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
  })

  it.each([
    ['lessons_credential', 'pages.membersPage.export_lessons_refused'],
    ['lessons_exfil_url', 'pages.membersPage.export_lessons_exfil_url'],
    ['lessons_too_large', 'pages.membersPage.export_lessons_too_large'],
    ['invalid_name', 'pages.membersPage.export_failed'],
  ])('words a %s refusal on the press as its own cause', async (code, key) => {
    servePlan(view())
    memberExportFile.mockRejectedValue(new ApiError(400, 'no', JSON.stringify({ error: 'no', code })))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    await screen.findByTestId('crew-export-view')
    fireEvent.click(screen.getByTestId('crew-export-download'))
    expect((await screen.findByTestId('crew-export-error')).textContent).toContain(i18nT(key))
  })

  it('shows a built-in dashboard id as is', async () => {
    servePlan(view({ dashboard: 'builtin:release-board', left_out: [] }))
    renderWithProviders(<CrewExportView slug="scout" member="scout" />)
    expect((await screen.findByTestId('crew-export-dashboard')).textContent).toContain('builtin:release-board')
    expect(screen.queryByTestId('crew-export-left-out')).toBeNull()
  })
})
