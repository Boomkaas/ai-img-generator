import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ServerMessage } from "../api/types";
import { json, stubFetch } from "../test/fetch";
import { overview } from "../test/overview";
import { AdminPage } from "./AdminPage";

const socket = vi.hoisted(() => ({ push: null as ((message: ServerMessage) => void) | null }));

vi.mock("../api/socket", () => ({
  openSocket: (onMessage: (message: ServerMessage) => void) => {
    socket.push = onMessage;
    return () => {
      socket.push = null;
    };
  },
}));

afterEach(() => {
  vi.useRealTimers();
});

function renderPage() {
  render(
    <MemoryRouter>
      <AdminPage />
    </MemoryRouter>,
  );
}

const table = (name: string) => screen.getByRole("table", { name });
const bodyRows = (name: string) => within(table(name)).getAllByRole("row").slice(1);

describe("first paint", () => {
  it("renders all four sections from the overview", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();

    const summary = await screen.findByRole("region", { name: "Autoscaler" });
    expect(summary).toHaveTextContent("Workers3 ready1 starting4 maximum");
    expect(summary).toHaveTextContent("Added a worker (3 → 4) at 21:41");
    const alerts = within(summary).getAllByRole("listitem");
    expect(alerts[0]).toHaveTextContent("A newer alert");
    expect(alerts[1]).toHaveTextContent("no app machine has enough free memory");

    expect(screen.getByText("2 running · 2 waiting")).toBeInTheDocument();
    expect(bodyRows("Workers")).toHaveLength(3);
    expect(bodyRows("Machines")).toHaveLength(3);
    expect(screen.getByRole("link", { name: "Back to generator" })).toHaveAttribute("href", "/");
  });

  it("lists running tasks before waiting ones, in queue order", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByRole("table", { name: "Tasks" });

    const rows = bodyRows("Tasks").map((row) =>
      within(row)
        .getAllByRole("cell")
        .slice(0, 6)
        .map((cell) => cell.textContent),
    );
    expect(rows).toEqual([
      ["3f2b", "ana@example.com", "512 px", "nodeB-worker-1", "63%", "5 min running"],
      ["77aa", "dan@example.com", "384 px", "nodeC-worker-1", "12%", "<1 min running"],
      ["c0de", "cleo@example.com", "256 px", "#1 in queue", "—", "4 min waiting"],
      ["9a8b", "ben@example.com", "768 px", "#2 in queue", "—", "1 min waiting"],
    ]);
  });

  it("shows each worker's state, task and memory", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByRole("table", { name: "Workers" });

    const [busy, , loading] = bodyRows("Workers");
    expect(within(busy!).getByText("busy")).toHaveClass("pill-accent");
    expect(busy).toHaveTextContent("3f2b63%5.9 GB");
    expect(within(loading!).getByText("loading")).toHaveClass("pill-warn");
    expect(loading).toHaveTextContent("——0.9 GB");
  });

  it("shows each machine's load and API server", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByRole("table", { name: "Machines" });

    const [a, b, c] = bodyRows("Machines");
    expect(a).toHaveTextContent("6 sockets · 41 req/min");
    expect(b).toHaveTextContent("1 socket · 17 req/min");
    expect(within(c!).getAllByRole("cell")[4]).toHaveTextContent("—");
  });
});

describe("meters", () => {
  it("turn the warn colour at 85% and above", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByRole("table", { name: "Machines" });

    const [a, b, c] = bodyRows("Machines").map((row) => within(row).getAllByRole("cell"));
    expect(within(a![2]!).getByText("24%")).not.toHaveClass("meter-warn");
    expect(within(b![2]!).getByText("97%")).toHaveClass("meter-warn");
    expect(within(b![3]!).getByText("85%")).not.toHaveClass("meter-warn"); // 84.9
    expect(within(c![3]!).getByText("85%")).toHaveClass("meter-warn");
  });

  it("don't warn about task progress", async () => {
    const tasks = [{ ...overview().tasks[1]!, progress: 96 }];
    stubFetch({ "GET /api/admin/overview": () => json(overview({ tasks })) });
    renderPage();
    expect(await screen.findByText("96%")).not.toHaveClass("meter-warn");
  });
});

describe("live updates", () => {
  it("replaces the data with each admin message", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByText("2 running · 2 waiting");

    act(() => socket.push?.(overview({ tasks: overview().tasks.slice(1, 2) })));
    expect(screen.getByText("1 running · 0 waiting")).toBeInTheDocument();
    expect(bodyRows("Tasks")).toHaveLength(1);
  });

  it("keeps a newer socket message over a first paint that answers late", async () => {
    let answer: (response: Response) => void = () => undefined;
    stubFetch({ "GET /api/admin/overview": () => new Promise((resolve) => (answer = resolve)) });
    renderPage();
    await waitFor(() => expect(socket.push).not.toBeNull());

    act(() => socket.push?.(overview({ tasks: [] })));
    await act(async () => answer(json(overview())));
    expect(screen.getByText("0 running · 0 waiting")).toBeInTheDocument();
  });

  it("ignores the user page's messages", async () => {
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByText("2 running · 2 waiting");

    act(() => socket.push?.({ type: "system", running: 0, waiting: 0, new_image_ready_in_s: 0 }));
    expect(screen.getByText("2 running · 2 waiting")).toBeInTheDocument();
  });

  it("says live data is paused after 15 s without a message, until the next one", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    act(() => socket.push?.(overview()));

    act(() => vi.advanceTimersByTime(14_999));
    expect(screen.queryByText("Live data paused — reconnecting")).toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByText("Live data paused — reconnecting")).toBeInTheDocument();

    act(() => socket.push?.(overview()));
    expect(screen.queryByText("Live data paused — reconnecting")).toBeNull();
  });
});

describe("cancelling", () => {
  it("asks inline, and No puts the row back", async () => {
    const fetch = stubFetch({ "GET /api/admin/overview": () => json(overview()) });
    renderPage();
    await screen.findByRole("table", { name: "Tasks" });
    const user = userEvent.setup();
    const row = bodyRows("Tasks")[0]!;

    await user.click(within(row).getByRole("button", { name: "Cancel" }));
    expect(row).toHaveTextContent("Cancel this task?");
    expect(within(row).getByRole("button", { name: "No" })).toHaveFocus();

    await user.click(within(row).getByRole("button", { name: "No" }));
    expect(row).not.toHaveTextContent("Cancel this task?");
    expect(within(row).getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(fetch.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("Yes sends the admin cancel for that task", async () => {
    const fetch = stubFetch({
      "GET /api/admin/overview": () => json(overview()),
      "DELETE /api/admin/tasks/c0de0000-0000-4000-8000-000000000000": () => json({ status: "cancelled" }),
    });
    renderPage();
    await screen.findByRole("table", { name: "Tasks" });
    const user = userEvent.setup();
    const row = bodyRows("Tasks")[2]!;

    await user.click(within(row).getByRole("button", { name: "Cancel" }));
    await user.click(within(row).getByRole("button", { name: "Yes" }));

    expect(await within(row).findByText("Cancelling…")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      "/api/admin/tasks/c0de0000-0000-4000-8000-000000000000",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("says why when the cancel fails", async () => {
    stubFetch({
      "GET /api/admin/overview": () => json(overview()),
      "DELETE /api/admin/tasks/3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c": () =>
        json({ detail: "Something went wrong on our side." }, 500),
    });
    renderPage();
    await screen.findByRole("table", { name: "Tasks" });
    const user = userEvent.setup();
    const row = bodyRows("Tasks")[0]!;

    await user.click(within(row).getByRole("button", { name: "Cancel" }));
    await user.click(within(row).getByRole("button", { name: "Yes" }));

    expect(await within(row).findByRole("alert")).toHaveTextContent("Something went wrong on our side.");
    expect(within(row).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });
});
