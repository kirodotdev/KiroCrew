import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { expect, test } from '@playwright/test'

const PROJECT_NAME = 'Launchpad'
const UPDATED_PROJECT_NAME = 'Launchpad Workspace'

function runGit(cwd: string, ...args: string[]) {
  execFileSync('git', args, { cwd, stdio: 'pipe' })
}

function createRepository(root: string, name: string, marker: string): string {
  const repository = join(root, name)
  mkdirSync(repository, { recursive: true })
  runGit(root, 'init', '--initial-branch=main', repository)
  runGit(repository, 'config', 'user.name', 'Kiro Crew E2E')
  runGit(repository, 'config', 'user.email', 'e2e@example.invalid')
  writeFileSync(join(repository, 'README.md'), `${marker}\n`, 'utf8')
  runGit(repository, 'add', 'README.md')
  runGit(repository, 'commit', '-m', 'initial fixture')
  return repository
}

test('adds a multi-repo Project and starts a usable attached session', async ({ page, request }) => {
  const fixtureRoot = mkdtempSync(join(tmpdir(), 'kirocrew-project-e2e-'))
  const projectId = randomUUID()
  const screenshotRoot = process.env.PROJECTS_SCREENSHOT_DIR
  let slotKey = ''
  async function capture(name: string) {
    if (!screenshotRoot) return
    mkdirSync(screenshotRoot, { recursive: true })
    await page.screenshot({ animations: 'disabled', path: join(screenshotRoot, name) })
  }

  async function scrollContentToTop() {
    await page.locator('#main-content').evaluate(element => { element.scrollTop = 0 })
    await page.evaluate(() => window.scrollTo(0, 0))
  }

  try {
    const bundle = join(fixtureRoot, 'bundle')
    mkdirSync(bundle, { recursive: true })
    const webRemote = createRepository(fixtureRoot, 'web-remote', 'launchpad web repository')
    const serviceRemote = createRepository(fixtureRoot, 'service-remote', 'launchpad service repository')
    const docsRemote = createRepository(fixtureRoot, 'docs-remote', 'launchpad docs repository')
    mkdirSync(join(bundle, 'agents'), { recursive: true })
    writeFileSync(join(bundle, 'agents', 'reviewer.json'), JSON.stringify({ name: 'reviewer' }), 'utf8')
    mkdirSync(join(bundle, 'skills', 'launch-review'), { recursive: true })
    writeFileSync(join(bundle, 'skills', 'launch-review', 'SKILL.md'), '# Launch review\n', 'utf8')
    mkdirSync(join(bundle, 'config'), { recursive: true })
    writeFileSync(join(bundle, 'config', 'mcp.json'), JSON.stringify({
      mcpServers: { docs: { url: 'https://example.invalid/mcp' } },
    }), 'utf8')
    writeFileSync(join(bundle, 'project.yaml'), JSON.stringify({
      apiVersion: 'crew.kiro/v1',
      kind: 'Project',
      id: projectId,
      name: PROJECT_NAME,
      description: 'A portable two-repository Project used to verify the complete session flow.',
      workspace: { source: 'web' },
      sources: [
        { id: 'web', type: 'repo', url: webRemote },
        { id: 'service', type: 'repo', url: serviceRemote },
      ],
      context: { agents: [], skills: [] },
    }, null, 2), 'utf8')
    runGit(fixtureRoot, 'init', '--initial-branch=main', bundle)
    runGit(bundle, 'config', 'user.name', 'Kiro Crew E2E')
    runGit(bundle, 'config', 'user.email', 'e2e@example.invalid')
    runGit(bundle, 'add', '.')
    runGit(bundle, 'commit', '-m', 'project bundle fixture')
    const bundleRemote = pathToFileURL(bundle).href

    await page.setViewportSize({ width: 1440, height: 1100 })
    await page.goto('/capabilities?tab=projects', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('#main-content nav').getByRole('button', { name: 'Projects', exact: true })).toBeVisible({ timeout: 10000 })

    // Each retry gets a fresh stable id because the gateway intentionally keeps
    // registered Projects and derived repo clones for its whole lifetime.
    await page.getByRole('button', { name: 'Add project', exact: true }).click()
    await page.getByRole('textbox', { name: 'Folder or Git URL' }).fill(bundleRemote)
    const addResponsePromise = page.waitForResponse(response => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/project-bundles/add'
    ))
    await page.locator('form').getByRole('button', { name: 'Add project', exact: true }).click()
    const addResponse = await addResponsePromise
    if (!addResponse.ok()) {
      throw new Error(
        `Project add request failed (${addResponse.status()}): ${await addResponse.text()}`,
      )
    }

    const currentProject = page.locator(`[data-project-id="${projectId}"]`)
    await expect(currentProject).toHaveAccessibleName(new RegExp(`^Open project ${PROJECT_NAME}`), { timeout: 10000 })
    await capture('01-project-list.png')

    await currentProject.click()
    await expect(page.getByRole('heading', { name: PROJECT_NAME, exact: true })).toBeVisible()
    await expect(page.getByText(webRemote, { exact: true })).toBeVisible()
    await expect(page.getByText(serviceRemote, { exact: true })).toBeVisible()
    await scrollContentToTop()
    await capture('02-project-detail.png')

    writeFileSync(join(bundle, 'project.yaml'), JSON.stringify({
      apiVersion: 'crew.kiro/v1',
      kind: 'Project',
      id: projectId,
      name: UPDATED_PROJECT_NAME,
      description: 'One focused Project for launch work across code, services, and docs.',
      workspace: { source: 'service' },
      sources: [
        { id: 'web', type: 'repo', url: webRemote, default_branch: 'main' },
        { id: 'service', type: 'repo', url: serviceRemote, default_branch: 'main' },
        { id: 'docs', type: 'repo', url: docsRemote, default_branch: 'main' },
      ],
      context: {
        agents: ['agents/*.json'],
        skills: ['skills/'],
        mcp: 'config/mcp.json',
      },
    }, null, 2), 'utf8')
    runGit(bundle, 'add', 'project.yaml')
    runGit(bundle, 'commit', '-m', 'update project context')
    await page.getByRole('button', { name: 'Sync project', exact: true }).click()

    await expect(page.getByRole('heading', { name: UPDATED_PROJECT_NAME, exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(docsRemote, { exact: true })).toBeVisible()
    await expect(page.getByText('agents/*.json', { exact: true })).toBeVisible()
    await expect(page.getByText('skills', { exact: true })).toBeVisible()
    await expect(page.getByText('config/mcp.json', { exact: true })).toBeVisible()
    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: UPDATED_PROJECT_NAME, exact: true })).toBeVisible({ timeout: 10000 })

    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto(`/capabilities?tab=projects&project=${projectId}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: UPDATED_PROJECT_NAME, exact: true })).toBeVisible()
    await expect(page.getByText(docsRemote, { exact: true })).toBeVisible()
    await page.setViewportSize({ width: 1440, height: 1100 })

    await page.getByRole('button', { name: 'Trust and activate', exact: true }).click()
    await page.getByRole('dialog', { name: 'Trust and activate this project?', exact: true })
      .getByRole('button', { name: 'Trust and activate', exact: true })
      .click()
    await expect(page.getByRole('button', { name: 'Deactivate capabilities', exact: true })).toBeVisible({ timeout: 15000 })

    const projectResponse = await request.get(`/api/project-bundles/${projectId}`)
    if (!projectResponse.ok()) {
      throw new Error(
        `Project detail request failed (${projectResponse.status()}): ${await projectResponse.text()}`,
      )
    }
    const activated = await projectResponse.json() as {
      capabilities: { active: boolean; agents: number; skills: number; mcp_servers: number; repos: number }
    }
    expect(activated.capabilities).toMatchObject({ active: true, agents: 1, skills: 1, mcp_servers: 1, repos: 3 })
    await page.getByRole('heading', { name: 'Included capabilities', exact: true }).scrollIntoViewIfNeeded()
    await capture('05-project-active.png')

    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
    const startingSlotKey = new URL(page.url()).searchParams.get('sid') || ''
    const createMenu = page.locator('[data-create-menu]')
    await expect(createMenu.getByRole('button', { name: 'More create options' })).toBeVisible({ timeout: 10000 })
    await createMenu.getByRole('button', { name: 'More create options' }).click()
    await expect(page.getByRole('menuitem', { name: new RegExp(`^${UPDATED_PROJECT_NAME}`) })).toBeVisible()
    await capture('06-project-new-session-menu.png')
    await page.getByRole('menuitem', { name: new RegExp(`^${UPDATED_PROJECT_NAME}`) }).click()
    await expect.poll(
      () => new URL(page.url()).searchParams.get('sid') || '',
      { timeout: 15000 },
    ).not.toBe(startingSlotKey)
    slotKey = new URL(page.url()).searchParams.get('sid') || ''
    expect(slotKey).not.toBe('')
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })

    const slotsResponse = await request.get('/api/chat/slots')
    expect(slotsResponse.ok()).toBe(true)
    const slots = await slotsResponse.json() as Array<{ key: string; project?: string; project_id?: string }>
    const slot = slots.find(candidate => candidate.key === slotKey)
    expect(slot?.project_id).toBe(projectId)
    expect(slot?.project).toBeTruthy()
    const workspace = slot?.project as string
    expect(workspace.endsWith(join('sources', 'service'))).toBe(true)
    const web = join(dirname(workspace), 'web')
    const docs = join(dirname(workspace), 'docs')
    expect(existsSync(join(workspace, 'README.md'))).toBe(true)
    expect(existsSync(join(web, 'README.md'))).toBe(true)
    expect(existsSync(join(docs, 'README.md'))).toBe(true)
    expect(readFileSync(join(docs, 'README.md'), 'utf8')).toContain('launchpad docs repository')

    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('Confirm this Project session is ready.')
    await page.keyboard.press('Enter')
    await expect(page.locator('.msg-content').getByText('pong from the fake ACP backend', { exact: false }).first()).toBeVisible({ timeout: 15000 })
    await expect(page.getByRole('button', { name: `Project: ${UPDATED_PROJECT_NAME}`, exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: /Copy branch name/ })).toHaveCount(0)
    await capture('06-project-session.png')

    await page.goto('/capabilities?tab=projects', { waitUntil: 'domcontentloaded' })
    await expect(currentProject).toHaveAccessibleName(new RegExp(`^Open project ${UPDATED_PROJECT_NAME}`), { timeout: 10000 })
    await currentProject.click()
    await expect(page.getByRole('heading', { name: /^Sessions/ })).toBeVisible()
    await expect(page.locator(`a[href="/chat?sid=${slotKey}"]`)).toBeVisible()
    await page.getByRole('button', { name: 'Remove from Kiro Crew', exact: true }).click()
    await expect(page.getByRole('dialog', { name: `Remove ${UPDATED_PROJECT_NAME} from Kiro Crew?`, exact: true })).toBeVisible()
    await expect(page.getByText('Folders you added stay on disk. Kiro Crew removes only storage it created for this project.', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Remove project', exact: true }).click()
    await expect(currentProject).toHaveCount(0)
    await expect(page.getByTestId('project-bundles-empty')).toBeVisible()
    expect(existsSync(join(bundle, 'project.yaml'))).toBe(true)
    await capture('07-project-removed.png')
  } finally {
    if (slotKey) await request.delete(`/api/chat/slots/${encodeURIComponent(slotKey)}`).catch(() => {})
    await request.delete(`/api/project-bundles/${encodeURIComponent(projectId)}/activate`).catch(() => {})
    await request.delete(`/api/project-bundles/${encodeURIComponent(projectId)}`).catch(() => {})
    rmSync(fixtureRoot, { recursive: true, force: true })
  }
})
