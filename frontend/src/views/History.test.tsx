import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { json, stubFetch, task } from "../test/fetch";
import { History } from "./History";

const done = task({
  task_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  status: "done",
  prompt: "a lighthouse at dusk",
  finished_at: "2026-10-17T09:20:11Z",
});
const cancelled = task({
  task_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  status: "cancelled",
  prompt: "a cat",
});
const failed = task({ task_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", status: "failed", prompt: "a dog" });

describe("History", () => {
  it("expands a done entry to its image; cancelled and failed entries aren't buttons", async () => {
    stubFetch({ "GET /api/tasks": () => json({ items: [done, cancelled, failed], next_before: null }) });
    render(<History onClose={() => undefined} />);

    const entry = await screen.findByRole("button", { name: /a lighthouse at dusk/ });
    expect(entry).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("img")).toBeNull();
    await userEvent.setup().click(entry);
    expect(screen.getByRole("img", { name: "a lighthouse at dusk" })).toHaveAttribute(
      "src",
      `/api/tasks/${done.task_id}/image`,
    );

    expect(screen.getByText("a cat")).toBeInTheDocument();
    expect(screen.getByText("a dog")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /a cat/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /a dog/ })).toBeNull();
  });

  it("says where images will appear when there are none", async () => {
    stubFetch({ "GET /api/tasks": () => json({ items: [], next_before: null }) });
    render(<History onClose={() => undefined} />);
    expect(await screen.findByText("Images you generate will appear here.")).toBeInTheDocument();
  });

  it("loads the next page from next_before", async () => {
    const cursor = "2026-10-17T09:12:03Z";
    stubFetch({
      "GET /api/tasks": () => json({ items: [done], next_before: cursor }),
      [`GET /api/tasks?before=${encodeURIComponent(cursor)}`]: () =>
        json({ items: [failed], next_before: null }),
    });
    render(<History onClose={() => undefined} />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("a dog")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("closes on Escape, on Close and on the backdrop", async () => {
    stubFetch({ "GET /api/tasks": () => json({ items: [], next_before: null }) });
    const onClose = vi.fn();
    const { container } = render(<History onClose={onClose} />);
    const user = userEvent.setup();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(container.querySelector(".backdrop")!);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("asks for the next page once however often Load more is clicked", async () => {
    const cursor = "2026-10-17T09:12:03Z";
    let release = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const fetch = stubFetch({
      "GET /api/tasks": () => json({ items: [done], next_before: cursor }),
      [`GET /api/tasks?before=${encodeURIComponent(cursor)}`]: async () => {
        await gate;
        return json({ items: [failed], next_before: null });
      },
    });
    render(<History onClose={() => undefined} />);
    const user = userEvent.setup();
    const more = await screen.findByRole("button", { name: "Load more" });
    await user.click(more);
    await user.click(more);
    release();
    expect(await screen.findByText("a dog")).toBeInTheDocument();
    expect(screen.getAllByText("a dog")).toHaveLength(1);
    expect(fetch.mock.calls.filter(([path]) => path.includes("before="))).toHaveLength(1);
  });

  it("keeps Tab inside the drawer", async () => {
    stubFetch({ "GET /api/tasks": () => json({ items: [done], next_before: null }) });
    render(
      <>
        <button type="button">Behind</button>
        <History onClose={() => undefined} />
      </>,
    );
    const entry = await screen.findByRole("button", { name: /a lighthouse at dusk/ });
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();
    const user = userEvent.setup();
    await user.tab();
    expect(entry).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(entry).toHaveFocus();
  });
});
