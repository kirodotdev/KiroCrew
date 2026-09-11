import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import en from "../i18n/locales/en.manual.json";
import de from "../i18n/locales/de.json";

const template = readFileSync(
  resolve(__dirname, "../../../src/kiro_crew/agent_panel_templates/tasks.html"),
  "utf8",
);
const publishedAt = "2026-09-11T15:30:00+00:00";

function render(data: unknown, locale = "en", labels = en.memberTaskPanel) {
  const parsed = new DOMParser().parseFromString(template, "text/html");
  const scripts = [...parsed.querySelectorAll("script")];
  expect(scripts).toHaveLength(1);
  const root = parsed.getElementById("kt-root")!;
  const islands = [
    ["kirocrew-panel-data", data],
    ["kirocrew-context", { locale, labels, publishedAt }],
  ].map(([id, value]) => {
    const holder = document.createElement("script");
    holder.type = "application/json";
    holder.id = id as string;
    holder.textContent = JSON.stringify(value);
    return holder;
  });
  document.body.replaceChildren(...islands, root);
  new Function(scripts[0].textContent || "")();
  return root;
}

const task = (item_id: string, extra: Record<string, unknown> = {}) => ({
  item_id,
  title: item_id,
  state: "open",
  status: null,
  has_worker: false,
  acceptance: { kind: "human_approval", description: "Owner approves the evidence" },
  ...extra,
});

beforeEach(() => document.body.replaceChildren());

describe("published task template", () => {
  it("derives lanes from the ledger and waits for acceptance before Done", () => {
    const root = render({
      items: [
        task("queued"),
        task("running", { has_worker: true, status: "progress" }),
        task("question", { has_worker: true, status: "question" }),
        task("finished", { has_worker: true, status: "done" }),
        task("retry", { has_worker: true, status: "done", verdict: "fail" }),
        task("accepted", { state: "accepted", status: "done", verdict: "pass" }),
        task("rejected", { state: "rejected", status: "done", verdict: "fail" }),
      ],
    });
    const titles = (lane: string) =>
      [...root.querySelectorAll(`[aria-label="${lane}"] summary`)].map(
        (element) => element.firstChild?.textContent,
      );
    expect(titles(en.memberTaskPanel.todo)).toEqual(["queued"]);
    expect(titles(en.memberTaskPanel.progress)).toEqual(["running", "retry"]);
    expect(titles(en.memberTaskPanel.blocked)).toEqual(["question"]);
    expect(titles(en.memberTaskPanel.review)).toEqual(["finished"]);
    expect(titles(en.memberTaskPanel.done)).toEqual(["accepted"]);
    expect(titles(en.memberTaskPanel.closed)).toEqual(["rejected"]);
  });

  it("shows its publication time, snapshot limitation, and chat instructions", () => {
    const root = render({ items: [task("queued")] }, "de", de.memberTaskPanel);
    expect(document.documentElement.lang).toBe("de");
    expect(root.textContent).toContain(de.memberTaskPanel.chat_hint);
    expect(root.textContent).toContain(de.memberTaskPanel.snapshot_hint);
    expect(root.querySelector("time")?.getAttribute("datetime")).toBe(publishedAt);
    expect(root.querySelector("time")?.textContent).toBe(
      new Intl.DateTimeFormat("de", { dateStyle: "medium", timeStyle: "short" })
        .format(new Date(publishedAt)),
    );
    expect(root.querySelector("button, form, input")).toBeNull();
  });

  it("keeps reports, criteria, decisions and evidence as text", () => {
    const hostile = '</script><img src=x onerror="alert(1)">';
    const root = render({
      summary: hostile,
      items: [
        task(hostile, {
          summary: hostile,
          decision: hostile,
          acceptance: { description: hostile },
          artifacts: { proof: hostile },
          pr: "https://github.com/example/project/pull/1",
          has_worker: true,
          status: "blocked",
          stale: true,
        }),
      ],
    });
    expect(root.querySelector("img, script, a")).toBeNull();
    expect(root.querySelector("summary")?.textContent).toContain(hostile);
    expect(root.querySelector("dl")?.textContent).toContain(hostile);
    expect(root.querySelector("dl")?.textContent).toContain("https://github.com/example/project/pull/1");
    expect([...root.querySelectorAll("dt")].filter(
      (heading) => heading.textContent === en.memberTaskPanel.evidence,
    )).toHaveLength(1);
    expect(root.querySelector("dl")?.textContent).toContain(en.memberTaskPanel.pull_request);
    expect(root.querySelector(".kt-report")?.textContent).toBe(hostile);
    expect(root.textContent).toContain(en.memberTaskPanel.stale);
  });

  it("distinguishes an empty snapshot from missing task data", () => {
    expect(render({ items: [] }).textContent).toContain(en.memberTaskPanel.empty);
    expect(render({}).textContent).toContain(en.memberTaskPanel.unavailable);
  });

  it("bounds cards and evidence and makes truncation visible", () => {
    const artifacts = Object.fromEntries(
      Array.from({ length: 100 }, (_, i) => [`evidence${i}`, "proof"]),
    );
    const root = render({
      items: Array.from({ length: 1000 }, (_, i) => task(`task${i}`, { artifacts })),
    });
    expect(root.querySelectorAll("article")).toHaveLength(32);
    expect(root.querySelectorAll("li")).toHaveLength(32 * 20);
    expect(root.textContent).toContain(en.memberTaskPanel.truncated);
  });
});
