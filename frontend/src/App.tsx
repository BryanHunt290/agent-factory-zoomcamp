// The shell: LOCAL DEVELOPMENT identity, system status, the attention count, and the five screens.
// It keeps no workflow state: every screen reconstructs what it shows from the API.
import { useCallback, useState, useSyncExternalStore } from "react";
import type { ApiClient } from "./api/client";
import type { AttentionPage, Readiness } from "./api/types";
import { ClientContext, SharedContext, useClient } from "./app/context";
import { clearToken, readSignOutReason, readToken, subscribeToken } from "./auth/session";
import { SystemStatus } from "./components/SystemStatus";
import { usePolling } from "./lib/polling";
import { Link, parseRoute, paths, usePathname } from "./lib/router";
import { AttentionInbox } from "./screens/AttentionInbox";
import { ApprovalDetail } from "./screens/approval/ApprovalDetail";
import { Dashboard } from "./screens/Dashboard";
import { ProjectDetail } from "./screens/ProjectDetail";
import { SignIn } from "./screens/SignIn";

const ATTENTION_MS = 5_000;
const READINESS_MS = 10_000;

export function App({ client }: { client: ApiClient }) {
  const token = useSyncExternalStore(subscribeToken, readToken);
  const reason = useSyncExternalStore(subscribeToken, readSignOutReason);
  return (
    <ClientContext.Provider value={client}>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      {token ? (
        <SignedIn />
      ) : (
        <>
          <header className="app-header">
            <div className="brand-row">
              <span className="brand">AGENT FACTORY</span>
              <span className="env-label">LOCAL DEVELOPMENT</span>
            </div>
          </header>
          <SignIn reason={reason} />
        </>
      )}
    </ClientContext.Provider>
  );
}

function SignedIn() {
  const client = useClient();
  const [windowHours, setWindowHours] = useState(12);
  const attention = usePolling<AttentionPage>(
    "attention",
    useCallback((signal) => client.listAttention({ limit: 100 }, { signal }), [client]),
    { intervalMs: ATTENTION_MS },
  );
  const readiness = usePolling<Readiness>(
    "readiness",
    useCallback((signal) => client.readiness({ signal }), [client]),
    { intervalMs: READINESS_MS },
  );
  const route = parseRoute(usePathname());
  const count = attention.data?.items.length ?? 0;

  return (
    <SharedContext.Provider value={{ attention, readiness, windowHours, setWindowHours }}>
      <header className="app-header">
        <div className="brand-row">
          <Link to={paths.dashboard()} className="brand">
            AGENT FACTORY
          </Link>
          <span className="env-label">LOCAL DEVELOPMENT</span>
        </div>
        <SystemStatus readiness={readiness} signedIn={attention} />
        <nav aria-label="Main" className="main-nav">
          <Link to={paths.dashboard()} aria-current={route.name === "dashboard" ? "page" : undefined}>
            Dashboard
          </Link>
          <Link
            to={paths.attention()}
            aria-current={route.name === "attention" ? "page" : undefined}
            className={count ? "nav-attention has-items" : "nav-attention"}
          >
            Needs attention <span className="count-pill">{attention.data ? count : "…"}</span>
          </Link>
          <button type="button" className="link-button" onClick={() => clearToken()}>
            Sign out
          </button>
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        {route.name === "dashboard" ? <Dashboard /> : null}
        {route.name === "attention" ? <AttentionInbox /> : null}
        {route.name === "project" ? <ProjectDetail key={route.projectId} projectId={route.projectId} /> : null}
        {route.name === "approval" ? (
          <ApprovalDetail key={route.approvalId} projectId={route.projectId} approvalId={route.approvalId} />
        ) : null}
        {route.name === "not_found" ? (
          <>
            <h1>Not found</h1>
            <p>
              There is nothing at this address. <Link to={paths.dashboard()}>Go to the dashboard</Link>.
            </p>
          </>
        ) : null}
      </main>
      <footer className="app-footer">
        <p>
          Local development dashboard. Everything shown comes from the Agent Factory API; this browser keeps only this
          tab's sign-in.
        </p>
      </footer>
    </SharedContext.Provider>
  );
}
