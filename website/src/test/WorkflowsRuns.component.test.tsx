import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockApi = vi.hoisted(() => ({
  promoteWorkflowRun: vi.fn(),
}));

vi.mock("../api/client", () => ({ api: mockApi }));

import WorkflowsRuns, {
  type RunDetail,
  type RunSummary,
} from "../apps/workflows/WorkflowsRuns";
import { i18nT } from "../i18n/t";

const TASK_PLAN_SOURCE = "agents:\n  verify:\n    prompt: run tests\n";

const taskPlanRun: RunDetail = {
  run_id: "wf_task_plan",
  name: "Debug plan",
  status: "paused",
  result: null,
  error: null,
  author: "taskrunner",
  session_key: "dashboard:test",
  event_count: 1,
  source: TASK_PLAN_SOURCE,
  source_format: "task-plan",
  driver: "taskrunner",
  task_id: "plan_1",
  capabilities: ["save"],
  events: [
    {
      run_id: "wf_task_plan",
      seq: 1,
      ts: "2026-08-26T20:00:00Z",
      type: "run_started",
      data: { name: "Debug plan" },
    },
  ],
};

const jsonResponse = (body: unknown) =>
  Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  }) as Promise<Response>;

function renderRuns() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowsRuns />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockApi.promoteWorkflowRun.mockReset();
  mockApi.promoteWorkflowRun.mockResolvedValue({
    ok: true,
    definition: { slug: "debug-plan" },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/workflows/runs") {
        const summary: RunSummary = taskPlanRun;
        return jsonResponse({ runs: [summary] });
      }
      if (url === "/api/workflows/runs/wf_task_plan") {
        return jsonResponse(taskPlanRun);
      }
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WorkflowsRuns", () => {
  it("saves a paused TaskRunner plan from unified run history", async () => {
    renderRuns();

    fireEvent.click(await screen.findByText("Debug plan"));

    expect(await screen.findByText("taskrunner")).toBeInTheDocument();
    expect(screen.getByText("task-plan")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: i18nT("pages.chat.workflowRunCard.save_workflow"),
      }),
    );

    expect(
      screen.getByLabelText(i18nT("pages.overview.workflowLibrary.name")),
    ).toHaveValue("Debug plan");
    expect(
      screen.getByLabelText(i18nT("pages.overview.workflowLibrary.slug")),
    ).toHaveValue("debug-plan");
    expect(
      screen.getByLabelText(i18nT("pages.overview.workflowLibrary.source")),
    ).toHaveTextContent("agents: verify: prompt: run tests");

    fireEvent.change(
      screen.getByLabelText(
        i18nT("pages.overview.workflowLibrary.workflow_description"),
      ),
      { target: { value: "Reusable debugging steps" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: i18nT("pages.overview.workflowLibrary.save_to_library"),
      }),
    );

    await waitFor(() =>
      expect(mockApi.promoteWorkflowRun).toHaveBeenCalledWith("wf_task_plan", {
        name: "Debug plan",
        description: "Reusable debugging steps",
        slug: "debug-plan",
      }),
    );
    expect(await screen.findByText("/workflow debug-plan")).toBeInTheDocument();
  });

  it("cancels a running workflow without selecting its row", async () => {
    const running = { ...taskPlanRun, status: "running" as const };
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/workflows/runs") {
        return jsonResponse({ runs: [running] });
      }
      if (url === "/api/workflows/runs/wf_task_plan/cancel") {
        return jsonResponse({ run_id: "wf_task_plan", cancelled: true });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    renderRuns();

    fireEvent.click(
      await screen.findByRole("button", {
        name: i18nT("apps.workflows.workflowsRuns.cancel"),
      }),
    );

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/workflows/runs/wf_task_plan/cancel",
        { method: "POST", credentials: "same-origin" },
      ),
    );
    expect(
      screen.getByText(
        i18nT(
          "apps.workflows.workflowsRuns.select_a_run_to_see_its_phases_agents_and_result",
        ),
      ),
    ).toBeInTheDocument();
  });
});
