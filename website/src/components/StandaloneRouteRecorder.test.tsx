import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { act } from "react";
import { StandaloneRouteRecorder } from "./StandaloneRouteRecorder";
import { STANDALONE_LAST_ROUTE_KEY } from "../lib/standaloneLastRoute";

function setStandalone(on: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: on && query === "(display-mode: standalone)",
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

let navigateTo: (to: string) => void = () => {};
function Nav() {
  navigateTo = useNavigate();
  return null;
}

describe("StandaloneRouteRecorder", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("records the shown route, including later navigations, when standalone", () => {
    setStandalone(true);
    const { container } = render(
      <MemoryRouter initialEntries={["/members?member=Raven"]}>
        <StandaloneRouteRecorder />
        <Nav />
      </MemoryRouter>,
    );
    expect(container.innerHTML).toBe("");
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBe(
      "/members?member=Raven",
    );
    act(() => navigateTo("/schedule"));
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBe("/schedule");
  });

  it("records nothing in a browser tab", () => {
    setStandalone(false);
    render(
      <MemoryRouter initialEntries={["/members"]}>
        <StandaloneRouteRecorder />
      </MemoryRouter>,
    );
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBeNull();
  });
});
