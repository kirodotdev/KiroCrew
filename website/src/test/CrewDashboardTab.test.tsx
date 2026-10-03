/**
 * The crewmate's Dashboard tab shows the crewmate's own dynamic dashboard and
 * nothing else: no view switch, no crew-log panel.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../pages/members/CrewDynamicDashboard", () => ({
  default: (p: { slug: string; displayName: string }) => (
    <div data-testid="frame-stub">{`${p.slug}/${p.displayName}`}</div>
  ),
}));

import CrewDashboardTab from "../pages/members/CrewDashboardTab";

describe("CrewDashboardTab", () => {
  it("shows only the crewmate's own page", async () => {
    render(<CrewDashboardTab slug="ada" member="Ada" displayName="Ada" />);
    expect((await screen.findByTestId("frame-stub")).textContent).toBe("ada/Ada");
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByTestId("crew-dashboard-log")).toBeNull();
  });

  it("falls back to the member name when no display name is given", async () => {
    render(<CrewDashboardTab slug="ada" member="Ada" displayName="" />);
    expect((await screen.findByTestId("frame-stub")).textContent).toBe("ada/Ada");
  });
});
