import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { json, stubFetch } from "./test/fetch";
import { overview } from "./test/overview";

vi.mock("./api/socket", () => ({ openSocket: () => () => undefined }));

function visit(path: string, role: string) {
  stubFetch({
    "GET /api/auth/me": () => json({ email: "ana@example.com", role }),
    "GET /api/tasks/active": () => json(null),
    "GET /api/admin/overview": () => json(overview()),
  });
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("/admin", () => {
  it("opens for an admin", async () => {
    visit("/admin", "admin");
    expect(await screen.findByRole("heading", { name: "Admin" })).toBeInTheDocument();
  });

  it("sends anyone else to the generator", async () => {
    visit("/admin", "user");
    expect(await screen.findByRole("heading", { name: "Generate" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Admin" })).toBeNull();
  });
});
