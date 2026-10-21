import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ServerMessage } from "../api/types";
import { json, stubFetch, task } from "../test/fetch";
import { UserPage } from "./UserPage";

const socket = vi.hoisted(() => ({ push: null as ((message: ServerMessage) => void) | null }));

vi.mock("../api/socket", () => ({
  openSocket: (onMessage: (message: ServerMessage) => void) => {
    socket.push = onMessage;
    return () => {
      socket.push = null;
    };
  },
}));

const ID = task().task_id;
const system = { type: "system", running: 1, waiting: 0, new_image_ready_in_s: 600 } as const;

function renderPage(role = "user") {
  render(
    <MemoryRouter>
      <UserPage user={{ email: "ana@example.com", role }} onLogOut={() => undefined} />
    </MemoryRouter>,
  );
}

async function push(message: ServerMessage) {
  await waitFor(() => expect(socket.push).not.toBeNull());
  act(() => socket.push?.(message));
}

async function submit(prompt = "a red fox in fresh snow") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Prompt"), prompt);
  await user.click(screen.getByRole("button", { name: "Generate" }));
}

describe("submitting", () => {
  it("refuses an empty prompt without sending anything", async () => {
    const fetch = stubFetch({ "GET /api/tasks/active": () => json(null) });
    renderPage();
    await userEvent.setup().click(screen.getByRole("button", { name: "Generate" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Write what you want to see first.");
    expect(fetch.mock.calls.some(([path]) => path === "/api/tasks")).toBe(false);
  });

  it("says so when the user already has an image in progress", async () => {
    stubFetch({
      "GET /api/tasks/active": () => json(null),
      "POST /api/tasks": () => json({ detail: "You already have an image in progress" }, 429),
    });
    renderPage();
    await submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("You already have an image in progress.");
  });

  it("says when to try again when the queue is full", async () => {
    stubFetch({
      "GET /api/tasks/active": () => json(null),
      "POST /api/tasks": () => json({ detail: "The queue is full", retry_in_s: 840 }, 503),
    });
    renderPage();
    await submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The queue is full right now. Try again in about 14 min.",
    );
  });

  it("retries once with the same idempotency key after a network error", async () => {
    let calls = 0;
    const fetch = stubFetch({
      "GET /api/tasks/active": () => json(null),
      "POST /api/tasks": () => {
        calls += 1;
        if (calls === 1) throw new TypeError("Failed to fetch");
        return json(task(), 202);
      },
    });
    renderPage();
    await submit();
    expect(await screen.findByText("Position 1 of 1 · ready in about 10 min")).toBeInTheDocument();
    const keys = fetch.mock.calls
      .filter(([path]) => path === "/api/tasks")
      .map(([, init]) => (init?.headers as Record<string, string>)["Idempotency-Key"]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("gives up after the retry fails too", async () => {
    stubFetch({
      "GET /api/tasks/active": () => json(null),
      "POST /api/tasks": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    renderPage();
    await submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Can't reach the server. Check your connection and try again.",
    );
  });
});

describe("following a task", () => {
  it("shows the percentage and what the worker is doing", async () => {
    stubFetch({ "GET /api/tasks/active": () => json(null) });
    renderPage();
    await push({
      type: "snapshot",
      system,
      task: task({ status: "running", progress: 48, ready_in_s: 230 }),
    });
    await push({ type: "task", id: ID, status: "running", progress: 52, stage: "step 12 of 25" });
    expect(screen.getByText("52%")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "52");
    expect(screen.getByText("step 12 of 25 · about 4 min left")).toBeInTheDocument();
    expect(screen.getByText("1 generating · 0 waiting · about 10 min for a new image")).toBeInTheDocument();
  });

  it("returns to the form with the prompt kept after cancelling", async () => {
    stubFetch({
      "GET /api/tasks/active": () => json(task({ size: 384 })),
      [`DELETE /api/tasks/${ID}`]: () => json({ status: "cancelled" }),
    });
    renderPage();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByLabelText("Prompt")).toHaveValue("a red fox in fresh snow");
    expect(screen.getByLabelText("Size")).toHaveValue("384");
  });

  it.each([
    ["time limit exceeded", "This image took longer than the time limit allows."],
    ["the worker stopped unexpectedly", "This image couldn't be finished because its worker stopped."],
    ["out of memory", "This image couldn't be finished."],
  ])("explains a failure from %j, and Try again restores the form", async (errorMessage, sentence) => {
    const failed = task({ status: "failed", size: 768, error_message: errorMessage });
    stubFetch({
      "GET /api/tasks/active": () => json(null),
      [`GET /api/tasks/${ID}`]: () => json(failed),
    });
    renderPage();
    await push({ type: "snapshot", system, task: task({ status: "running", size: 768, progress: 40 }) });
    await push({ type: "task_final", id: ID, status: "failed" });
    expect(await screen.findByText(sentence)).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByLabelText("Prompt")).toHaveValue("a red fox in fresh snow");
    expect(screen.getByLabelText("Size")).toHaveValue("768");
  });
});

describe("a task that ended while the socket was down", () => {
  it("is fetched again until the request works, then shows its result", async () => {
    let attempts = 0;
    stubFetch({
      "GET /api/tasks/active": () => json(null),
      [`GET /api/tasks/${ID}`]: () => {
        attempts += 1;
        return attempts === 1
          ? json({ detail: "Something went wrong" }, 500)
          : json(task({ status: "done" }));
      },
    });
    renderPage();
    await push({ type: "snapshot", system, task: task({ status: "running", progress: 80 }) });
    await push({ type: "snapshot", system, task: null });
    expect(
      await screen.findByRole("button", { name: "Generate another" }, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(attempts).toBe(2);
  });
});

describe("links", () => {
  it("shows Admin to admins", () => {
    stubFetch({ "GET /api/tasks/active": () => json(null) });
    renderPage("admin");
    expect(screen.getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin");
  });

  it("hides Admin from everyone else", () => {
    stubFetch({ "GET /api/tasks/active": () => json(null) });
    renderPage("user");
    expect(screen.queryByRole("link", { name: "Admin" })).toBeNull();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });
});
