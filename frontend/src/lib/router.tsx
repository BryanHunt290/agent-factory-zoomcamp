// A small History API router: five screens do not need a router library.
import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "attention" }
  | { name: "project"; projectId: string }
  | { name: "approval"; projectId: string; approvalId: string }
  | { name: "not_found" };

const PROJECT = "prj-[0-9a-f]{20}";
const APPROVAL = "apr-[0-9a-f]{16}";
const PATTERNS: [RegExp, (m: RegExpMatchArray) => Route][] = [
  [/^\/$/, () => ({ name: "dashboard" })],
  [/^\/attention\/?$/, () => ({ name: "attention" })],
  [new RegExp(`^/projects/(${PROJECT})/?$`), (m) => ({ name: "project", projectId: m[1]! })],
  [
    new RegExp(`^/projects/(${PROJECT})/approvals/(${APPROVAL})/?$`),
    (m) => ({ name: "approval", projectId: m[1]!, approvalId: m[2]! }),
  ],
];

/** The route for ``pathname``. Unknown paths and malformed IDs are not found: IDs are never guessed. */
export function parseRoute(pathname: string): Route {
  for (const [pattern, build] of PATTERNS) {
    const match = pathname.match(pattern);
    if (match) return build(match);
  }
  return { name: "not_found" };
}

export const paths = {
  dashboard: () => "/",
  attention: () => "/attention",
  project: (projectId: string) => `/projects/${projectId}`,
  approval: (projectId: string, approvalId: string) => `/projects/${projectId}/approvals/${approvalId}`,
};

const NAVIGATE = "agent-factory:navigate";

function subscribe(listener: () => void): () => void {
  window.addEventListener("popstate", listener);
  window.addEventListener(NAVIGATE, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(NAVIGATE, listener);
  };
}

export function usePathname(): string {
  return useSyncExternalStore(subscribe, () => window.location.pathname);
}

export function navigate(to: string, options: { replace?: boolean } = {}): void {
  if (to === window.location.pathname) return;
  if (options.replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(NAVIGATE));
  window.scrollTo?.(0, 0);
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { to: string; children: ReactNode };

/** An ordinary link (works with the keyboard, middle-click, and "open in new tab"), routed in place. */
export function Link({ to, children, onClick, ...rest }: LinkProps) {
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  );
}
