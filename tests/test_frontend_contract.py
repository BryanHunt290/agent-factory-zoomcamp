"""The dashboard's contract and safety rules, checked from its source (no Node.js needed).

    frontend/      the React + TypeScript + Vite dashboard (docs/frontend.md)

* The TypeScript types were generated from the current openapi.yaml: the generated file records the
  contract's SHA-256, so a contract change without regenerating fails here (and `npm run
  contract:check` regenerates and compares byte for byte).
* Dependencies are exactly the owner-approved set, pinned exactly, with a lockfile.
* The source never renders HTML from data, never evaluates code, never uses localStorage, never logs,
  never reads build-time environment variables into the bundle, and loads nothing from another origin.
* The dev and preview servers proxy only to a loopback API, and serve a strict Content Security Policy.

The behaviour itself is tested by Vitest (frontend/src/**/*.test.ts[x]) and, against the real API, by
the browser suites (tests/test_dashboard_*.py, marker browser).
"""

import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
SRC = FRONTEND / "src"

#: The owner's approval (Dashboard v1): nothing else may be added without a new decision.
APPROVED_RUNTIME = {"react", "react-dom"}
APPROVED_DEV = {
    "vite", "typescript", "@types/react", "@types/react-dom", "openapi-typescript", "vitest", "jsdom",
    "@testing-library/react", "@testing-library/dom", "axe-core",
}  # fmt: skip
EXACT = re.compile(r"^\d+\.\d+\.\d+$")


def app_sources():
    """The dashboard's own runtime code: everything under src/ except tests, test helpers, and generated types."""
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in {".ts", ".tsx", ".css"} or path.name == "schema.ts":
            continue
        if ".test." in path.name or "test" in path.relative_to(SRC).parts[:1]:
            continue
        yield path


def code_only(text: str) -> str:
    """``text`` without // and /* */ comments; string and template literals are kept intact."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != chr(10):
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def test_the_generated_types_come_from_the_current_contract():
    schema = (SRC / "api" / "schema.ts").read_text(encoding="utf-8")
    digest = hashlib.sha256((REPO / "openapi.yaml").read_bytes()).hexdigest()
    assert f"// openapi.yaml sha256: {digest}" in schema.splitlines()[1], "run: cd frontend && npm run contract:generate"
    assert schema.startswith("// GENERATED FILE: do not edit.")


def test_dependencies_are_exactly_the_approved_ones_pinned_exactly():
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    assert set(package["dependencies"]) == APPROVED_RUNTIME
    assert set(package["devDependencies"]) == APPROVED_DEV
    for name, version in {**package["dependencies"], **package["devDependencies"]}.items():
        assert EXACT.match(version), f"{name} is not pinned exactly: {version}"
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] >= 3
    root = lock["packages"][""]
    assert root["dependencies"] == package["dependencies"] and root["devDependencies"] == package["devDependencies"]
    for path, entry in lock["packages"].items():
        if path:
            assert entry.get("resolved", "").startswith("https://registry.npmjs.org/"), path
            assert entry.get("integrity", "").startswith("sha512-"), path


def test_browser_test_dependencies_are_pinned_exactly():
    lines = [line.strip() for line in (REPO / "requirements-browser.txt").read_text(encoding="utf-8").splitlines()]
    pins = [line for line in lines if line and not line.startswith("#")]
    assert pins and all(re.fullmatch(r"[A-Za-z0-9_.-]+==\d+(\.\d+)+", line) for line in pins), pins
    assert any(line.startswith("playwright==") for line in pins)


def test_the_source_never_renders_html_from_data_or_evaluates_code():
    forbidden = {
        "dangerouslySetInnerHTML": "renders HTML from data",
        ".innerHTML": "writes HTML",
        ".outerHTML": "writes HTML",
        "insertAdjacentHTML": "writes HTML",
        "document.write": "writes HTML",
        "eval(": "evaluates code",
        "new Function": "evaluates code",
        "localStorage": "would outlive the tab (the token belongs in sessionStorage)",
        "console.": "logs (the dashboard never logs, so it cannot log a token)",
        "import.meta.env": "would bake build-time environment variables into the bundle",
    }  # fmt: skip
    offenders = [
        f"{path.relative_to(FRONTEND)}: {needle} ({why})"
        for path in app_sources()
        for needle, why in forbidden.items()
        if needle in code_only(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_nothing_is_loaded_from_another_origin():
    offenders = []
    for path in [*app_sources(), FRONTEND / "index.html"]:
        for url in re.findall(r"""https?://[^\s"'`)<>]+""", path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(FRONTEND)}: {url}")
    assert offenders == []
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html) == []  # no inline scripts
    assert "<style" not in html


def test_the_token_goes_only_into_the_authorization_header_and_session_storage():
    client = (SRC / "api" / "client.ts").read_text(encoding="utf-8")
    assert "Authorization: `Bearer ${token}`" in client
    session = (SRC / "auth" / "session.ts").read_text(encoding="utf-8")
    assert "window.sessionStorage" in session
    users = [path.relative_to(SRC).as_posix() for path in app_sources() if "Authorization" in path.read_text(encoding="utf-8")]
    assert users == ["api/client.ts"]
    fetchers = [path.relative_to(SRC).as_posix() for path in app_sources() if re.search(r"\bfetch\(", path.read_text(encoding="utf-8"))]
    assert fetchers == ["api/client.ts"], "components must call the API client, never fetch() directly"


def test_the_dev_servers_proxy_only_to_loopback_and_send_a_strict_csp():
    config = code_only((FRONTEND / "vite.config.js").read_text(encoding="utf-8"))
    assert 'new Set(["127.0.0.1", "localhost", "[::1]"])' in config
    assert 'host: "127.0.0.1"' in config and "cors" not in config.lower().replace("cross-origin", "")
    assert "\"script-src 'self'\"" in config and "unsafe-eval" not in config
    csp = re.search(r"const CSP = \[(.*?)\]", config, re.S).group(1)
    assert "'unsafe-inline'" not in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
