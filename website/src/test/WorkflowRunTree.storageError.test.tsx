import { act, fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunTree from '../apps/workflows/WorkflowRunTree'
import { i18next, initI18n } from '../i18n/all'
import { i18nT } from '../i18n/t'
import { consumeChatHandoff, installSoftNavigate, __resetNavSeamForTests } from '../utils/errorReport'
import { renderWithProviders } from './helpers'

const RAW = `Workflow checkpoint could not be saved. ${'diagnostic detail '.repeat(25)}DIAGNOSTIC_TAIL`
const KEY = 'apps.workflows.workflowRunTree.checkpoint_save_failed'

afterEach(async () => {
  await i18next.changeLanguage('en')
  __resetNavSeamForTests()
  sessionStorage.clear()
})

describe('WorkflowRunTree checkpoint errors', () => {
  it.each([
    ['en', 'running'], ['en', 'finished'], ['zh-CN', 'running'], ['zh-CN', 'finished'],
  ] as const)('localizes %s warning beside a %s execution and retains its full diagnostic', async (language, status) => {
    await initI18n()
    await i18next.changeLanguage(language)
    renderWithProviders(<WorkflowRunTree events={[]} status={status} result={{ kept: true }} errorCode="workflow_checkpoint_failed" error={RAW} />)
    const notice = screen.getByTestId('workflow-run-tree-error')
    expect(notice).toHaveTextContent(i18nT(KEY))
    expect(notice).not.toHaveTextContent('Workflow checkpoint could not be saved')
    if (language === 'zh-CN') expect(notice).toHaveTextContent('进度未能保存')
    const details = screen.getByTestId('workflow-checkpoint-diagnostic')
    expect(details).not.toHaveAttribute('open')
    expect(details.querySelector('pre')?.textContent).toBe(RAW)
    fireEvent.click(within(details).getByText(i18nT('memoryV2.view_details')))
    expect(details).toHaveAttribute('open')
    expect(details).toHaveTextContent('DIAGNOSTIC_TAIL')
    if (status === 'finished') expect(screen.getByText(/"kept": true/)).toBeInTheDocument()
  })

  it('hands off this warning with its own diagnostic even beside another identical summary', async () => {
    await initI18n()
    installSoftNavigate(() => {})
    renderWithProviders(<>
      <WorkflowRunTree events={[]} status="finished" errorCode="workflow_checkpoint_failed" error={RAW} />
      <WorkflowRunTree events={[]} status="running" errorCode="workflow_checkpoint_failed" error="OTHER_DIAGNOSTIC" />
    </>)
    await userEvent.click(within(screen.getAllByTestId('workflow-run-tree-error')[0]).getByRole('button'))
    const prompt = consumeChatHandoff()
    expect(prompt).toContain('workflow_checkpoint_failed')
    expect(prompt).toContain('DIAGNOSTIC_TAIL')
    expect(prompt).not.toContain('OTHER_DIAGNOSTIC')
  })

  it('redacts diagnostic credentials without interpreting markup', () => {
    renderWithProviders(<WorkflowRunTree events={[]} status="running" errorCode="workflow_checkpoint_failed" error={'<script>bad()</script> token=SECRET_SENTINEL'} />)
    const details = screen.getByTestId('workflow-checkpoint-diagnostic')
    expect(details.querySelector('script')).toBeNull()
    expect(details).not.toHaveTextContent('SECRET_SENTINEL')
  })

  it('updates the localized summary without resetting the open diagnostic', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunTree events={[]} status="finished" errorCode="workflow_checkpoint_failed" error={RAW} />)
    const details = screen.getByTestId('workflow-checkpoint-diagnostic')
    fireEvent.click(within(details).getByText(i18nT('memoryV2.view_details')))
    await act(async () => { await i18next.changeLanguage('zh-CN') })
    expect(screen.getByTestId('workflow-run-tree-error')).toHaveTextContent('进度未能保存')
    expect(details).toHaveAttribute('open')
  })

  it('does not classify an execution failure as a checkpoint warning', () => {
    renderWithProviders(<WorkflowRunTree events={[]} status="failed" error="execution rejected" />)
    expect(screen.getByTestId('workflow-run-tree-error')).toHaveTextContent('execution rejected')
    expect(screen.queryByTestId('workflow-checkpoint-diagnostic')).not.toBeInTheDocument()
  })
})


it.each([undefined, 'future_code'])('does not infer checkpoint failure from status or English prose: %s', errorCode => {
  renderWithProviders(<WorkflowRunTree events={[]} status="cancelled" error="Workflow checkpoint could not be saved. Cancelled by operator." errorCode={errorCode} />)
  expect(screen.getByTestId('workflow-run-tree-error')).toHaveTextContent('Cancelled by operator')
  expect(screen.queryByTestId('workflow-checkpoint-diagnostic')).not.toBeInTheDocument()
})
