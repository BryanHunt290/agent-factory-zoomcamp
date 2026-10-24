import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApiClient } from "./api/client";
import { clearToken, readToken } from "./auth/session";
import "./styles.css";

const client = new ApiClient({
  getToken: readToken,
  // Any 401 from the API (an expired or revoked token) ends the session for this tab.
  onUnauthorized: () => clearToken("Your session is no longer valid. Sign in again with a new development token."),
});

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App client={client} />
    </StrictMode>,
  );
}
