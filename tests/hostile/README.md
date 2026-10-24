# Hostile fixtures (adversarial acceptance only)

These programs are deliberately hostile. They use the `.hpy` extension so
they can never be imported or collected as Python on the host.

`tests/test_sandbox_adversarial.py` copies their text into a workspace. It
runs them only through `SandboxManager` -> `DockerBackend` (isolation
CONTAINER), after a guard verifies the isolation level. While those tests
run, the host may launch only the `docker` CLI. **Never run these files
directly.**
