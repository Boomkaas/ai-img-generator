import { Link } from "react-router-dom";

export function AdminPage() {
  return (
    <main className="column">
      <h1>Admin</h1>
      <nav className="links">
        <Link to="/">Generate</Link>
      </nav>
    </main>
  );
}
