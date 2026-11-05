import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockApi = vi.hoisted(() => ({
  promoteWorkflowRun: vi.fn(),
}));

vi.mock("../api/client", () => ({ api: mockApi }));
// The read-only source view as plain text. The real highlighter keeps state
// across renders in this file, so once a test has opened a Save dialog a later
// one could read its text as empty, depending on the order the tests ran in.
vi.mock("../pierre", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreCode: ({ file }: { file: { contents: string } }) => <pre>{file.contents}</pre>,
}));

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

  it.each(["{Enter}", " "])(
    "cancels a running workflow from the keyboard (%s) without opening its row",
    async (key) => {
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
      const user = userEvent.setup();
      renderRuns();

      const cancel = await screen.findByRole("button", {
        name: i18nT("apps.workflows.workflowsRuns.cancel"),
      });
      cancel.focus();
      await user.keyboard(key);

      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/workflows/runs/wf_task_plan/cancel",
          { method: "POST", credentials: "same-origin" },
        ),
      );
      // The row's open handler would fetch the run detail.
      expect(fetchMock).not.toHaveBeenCalledWith(
        "/api/workflows/runs/wf_task_plan",
        expect.anything(),
      );
      expect(
        screen.getByText(
          i18nT(
            "apps.workflows.workflowsRuns.select_a_run_to_see_its_phases_agents_and_result",
          ),
        ),
      ).toBeInTheDocument();
      // Cancel must not sit inside another control: a button nested in a
      // role="button" row is invalid and its keys bubble into the row.
      expect(
        cancel.parentElement?.closest('button, [role="button"]'),
      ).toBeNull();
    },
  );

  it("opens a run from the keyboard on its row button", async () => {
    const user = userEvent.setup();
    renderRuns();

    await screen.findByText("Debug plan");
    const row = screen.getByRole("button", { name: /Debug plan/ });
    row.focus();
    await user.keyboard("{Enter}");

    expect(await screen.findByText("taskrunner")).toBeInTheDocument();
  });

  it("surfaces a failed run-detail fetch through the shared error notice", async () => {
    // Graph mode asks for the plan on the same endpoint, so this is the path a plan
    // request takes when it fails. The repo requires every user-facing error to render
    // through ErrorNotice with its agent hand-off, never a hand-written red box -- the
    // hand-off is safe here because the failed operation is a read.
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/workflows/runs") {
        const summary: RunSummary = taskPlanRun;
        return jsonResponse({ runs: [summary] });
      }
      if (url.startsWith("/api/workflows/runs/wf_task_plan")) {
        return Promise.reject(new Error("plan read failed"));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    renderRuns();

    fireEvent.click(await screen.findByText("Debug plan"));

    expect(await screen.findByText("plan read failed")).toBeInTheDocument();
    // The discriminator: a bare div carries no hand-off, so this is what tells
    // ErrorNotice apart from the shape the rule forbids.
    expect(
      await screen.findByRole("button", {
        name: i18nT("components.askAgent.ask_the_agent"),
      }),
    ).toBeInTheDocument();
  });
});

describe("WorkflowsRuns — run-scoped outcomes", () => {
  const second: RunDetail = {
    ...taskPlanRun,
    run_id: "wf_second",
    name: "Second run",
    events: [{ ...taskPlanRun.events[0], run_id: "wf_second" }],
  };

  function serve(runs: RunDetail[], extra: (url: string) => Promise<Response> | null) {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/workflows/runs") return jsonResponse({ runs });
      const handled = extra(url);
      if (handled) return handled;
      const run = runs.find((r) => url === `/api/workflows/runs/${r.run_id}`);
      if (run) return jsonResponse(run);
      throw new Error(`Unexpected request: ${url}`);
    });
  }

  it("a failed cancel is shown on its own run, and not on the next run opened", async () => {
    const runningA = { ...taskPlanRun, status: "running" as const };
    const runningB = { ...second, status: "running" as const };
    serve([runningA, runningB], (url) =>
      url === "/api/workflows/runs/wf_task_plan/cancel"
        ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({}) } as Response)
        : null,
    );
    renderRuns();

    // Open B, then cancel A from its row.
    fireEvent.click(await screen.findByText("Second run"));
    const cancels = await screen.findAllByRole("button", {
      name: i18nT("apps.workflows.workflowsRuns.cancel"),
    });
    fireEvent.click(cancels[0]);
    // The failure brings A up with its error.
    expect(await screen.findByText(/cancel → 409/)).toBeInTheDocument();
    // Opening B again does not carry A's failure over to it.
    fireEvent.click(screen.getByText("Second run"));
    await waitFor(() => expect(screen.queryByText(/cancel → 409/)).not.toBeInTheDocument());
  });

  it("two cancels in flight each keep their own busy state and their own failure", async () => {
    const runningA = { ...taskPlanRun, status: "running" as const };
    const runningB = { ...second, status: "running" as const };
    const settle: Record<string, (r: Response) => void> = {};
    serve([runningA, runningB], (url) => {
      const m = /^\/api\/workflows\/runs\/(wf_[a-z_]+)\/cancel$/.exec(url);
      return m ? new Promise<Response>((resolve) => { settle[m[1]] = resolve; }) : null;
    });
    renderRuns();
    const cancelName = i18nT("apps.workflows.workflowsRuns.cancel");
    const cancellingName = i18nT("apps.workflows.workflowsRuns.cancelling");
    const [cancelA, cancelB] = await screen.findAllByRole("button", { name: cancelName });
    fireEvent.click(cancelA);
    fireEvent.click(cancelB);
    await waitFor(() => expect(Object.keys(settle)).toHaveLength(2));
    // Both rows say they are cancelling, not only the latest one.
    expect(cancelA).toBeDisabled();
    expect(cancelB).toBeDisabled();
    expect(cancelA).toHaveTextContent(cancellingName);
    // A fails after B was pressed; B then succeeds.
    await act(async () => {
      settle.wf_task_plan({ ok: false, status: 409, json: () => Promise.resolve({}) } as Response);
    });
    await act(async () => {
      settle.wf_second({ ok: true, status: 200, json: () => Promise.resolve({ cancelled: true }) } as Response);
    });
    // A is brought up with its own failure.
    expect(await screen.findByText(/wf_task_plan\/cancel → 409/)).toBeInTheDocument();
  });

  it("two saves in flight each label their own run", async () => {
    const finish: ((value: unknown) => void)[] = [];
    mockApi.promoteWorkflowRun.mockImplementation(
      () => new Promise((resolve) => { finish.push(resolve); }),
    );
    serve([taskPlanRun, second], () => null);
    renderRuns();
    const saveFrom = async (name: string) => {
      fireEvent.click(await screen.findByText(name));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.save_workflow") })).toBeInTheDocument(),
      );
      fireEvent.click(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.save_workflow") }));
      fireEvent.click(screen.getByRole("button", { name: i18nT("pages.overview.workflowLibrary.save_to_library") }));
      fireEvent.click(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.cancel") }));
    };
    await saveFrom("Debug plan");
    await saveFrom("Second run");
    await waitFor(() => expect(finish).toHaveLength(2));
    await act(async () => { finish[1]({ ok: true, definition: { slug: "second-run" } }); });
    await act(async () => { finish[0]({ ok: true, definition: { slug: "debug-plan" } }); });
    expect(await screen.findByText("/workflow second-run")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Debug plan"));
    expect(await screen.findByText("/workflow debug-plan")).toBeInTheDocument();
  });

  it("a cancel that fails while a Save dialog is open does not retarget the save", async () => {
    const runningA = { ...second, status: "running" as const };
    let refuse!: (r: Response) => void;
    serve([runningA, taskPlanRun], (url) =>
      url === "/api/workflows/runs/wf_second/cancel"
        ? new Promise<Response>((resolve) => { refuse = resolve; })
        : null,
    );
    renderRuns();
    fireEvent.click(await screen.findByRole("button", { name: i18nT("apps.workflows.workflowsRuns.cancel") }));
    await waitFor(() => expect(refuse).toBeDefined());
    // Open the paused run and its Save dialog while the cancel of the other is in flight.
    fireEvent.click(screen.getByText("Debug plan"));
    await screen.findByText("taskrunner");
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.save_workflow") }));
    await act(async () => {
      refuse({ ok: false, status: 409, json: () => Promise.resolve({}) } as Response);
    });
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.overview.workflowLibrary.save_to_library") }));
    await waitFor(() => expect(mockApi.promoteWorkflowRun).toHaveBeenCalled());
    expect(mockApi.promoteWorkflowRun.mock.calls[0][0]).toBe("wf_task_plan");
  });

  it("a save that fails after its dialog was closed still says so on its run", async () => {
    let fail!: (e: Error) => void;
    mockApi.promoteWorkflowRun.mockImplementation(
      () => new Promise((_, reject) => { fail = reject; }),
    );
    serve([taskPlanRun], () => null);
    renderRuns();
    fireEvent.click(await screen.findByText("Debug plan"));
    await screen.findByText("taskrunner");
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.save_workflow") }));
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.overview.workflowLibrary.save_to_library") }));
    await waitFor(() => expect(mockApi.promoteWorkflowRun).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.cancel") }));
    await act(async () => { fail(new Error("promote refused")); });
    expect(await screen.findByText(i18nT("pages.overview.workflowLibrary.request_failed"))).toBeInTheDocument();
  });

  it("a save that lands after another run is opened labels only the run it saved", async () => {
    let finish!: (value: unknown) => void;
    mockApi.promoteWorkflowRun.mockImplementation(
      () => new Promise((resolve) => { finish = resolve; }),
    );
    serve([taskPlanRun, second], () => null);
    renderRuns();

    fireEvent.click(await screen.findByText("Debug plan"));
    await screen.findByText("taskrunner");
    fireEvent.click(
      screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.save_workflow") }),
    );
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.overview.workflowLibrary.save_to_library") }));
    await waitFor(() => expect(mockApi.promoteWorkflowRun).toHaveBeenCalledWith("wf_task_plan", expect.anything()));
    // Leave the dialog while the save is in flight and open the other run.
    fireEvent.click(screen.getByRole("button", { name: i18nT("pages.chat.workflowRunCard.cancel") }));
    fireEvent.click(screen.getByText("Second run"));
    await act(async () => { finish({ ok: true, definition: { slug: "debug-plan" } }); });
    expect(screen.queryByText("/workflow debug-plan")).not.toBeInTheDocument();
    // Back on the saved run, its slash name shows.
    fireEvent.click(screen.getByText("Debug plan"));
    expect(await screen.findByText("/workflow debug-plan")).toBeInTheDocument();
  });
});
