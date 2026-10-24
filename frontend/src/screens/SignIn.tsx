// LOCAL DEVELOPMENT sign-in: the development issuer's token, pasted or read from its file.
// It is not the production sign-in, which waits for a configured OpenID Connect provider.
import { useState, type ChangeEvent, type FormEvent } from "react";
import { isSignedOut } from "../api/errors";
import { useClient } from "../app/context";
import { checkTokenFormat, saveToken, TokenFormatError } from "../auth/session";

export function SignIn({ reason }: { reason?: string | null }) {
  const client = useClient();
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function readFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 16_384) {
      setProblem("That file is too large to be a token file.");
      return;
    }
    setValue((await file.text()).trim());
    setProblem(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setProblem(null);
    let token: string;
    try {
      token = checkTokenFormat(value);
    } catch (error) {
      setProblem(error instanceof TokenFormatError ? error.message : "That token could not be used.");
      return;
    }
    setBusy(true);
    try {
      await client.checkToken(token); // the API verifies the signature, issuer, audience, and expiry
    } catch (error) {
      if (isSignedOut(error)) {
        setBusy(false);
        setProblem("That token was not accepted. It may have expired, or its subject is not enrolled.");
        return;
      }
      // Anything else (a missing permission, or Agent Factory unreachable right now) is explained by the
      // dashboard, which keeps trying: the token itself was not refused.
    }
    setValue("");
    saveToken(token); // the shell takes over from here
  }

  return (
    <main id="main" className="signin">
      <h1>Sign in</h1>
      <p className="env-banner" role="note">
        <strong>LOCAL DEVELOPMENT.</strong> This sign-in uses Agent Factory's development token issuer. It is not
        production sign-in.
      </p>
      {reason ? (
        <div className="notice notice-error" role="alert">
          <p className="notice-title">{reason}</p>
        </div>
      ) : null}
      <form onSubmit={submit} className="signin-form" noValidate>
        <p>Create a token on this machine, then paste it here or choose its file:</p>
        <pre className="command">python -m agent_factory auth dev-issuer token --subject YOU --out .local\dev-token.txt</pre>
        <label htmlFor="token">Development access token</label>
        <input
          id="token"
          name="token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          aria-describedby="token-help"
          required
        />
        <p id="token-help" className="help">
          It is kept in this browser tab only, and removed when the tab closes. It is never shown or logged.
        </p>
        <label htmlFor="token-file">Or choose the token file</label>
        <input id="token-file" type="file" accept=".txt,text/plain" onChange={readFile} />
        {problem ? (
          <p className="field-error" role="alert">
            {problem}
          </p>
        ) : null}
        <button type="submit" className="button primary" disabled={busy || !value.trim()}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
