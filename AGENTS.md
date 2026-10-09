## Development and test environment

This project is developed by Codex running inside a Docker Sandbox.

Treat the Codex sandbox itself as an orchestration and repository-management environment, not as the application's development/runtime environment.

### Environment boundary

* Do not install project runtime, build, test, or library dependencies directly into the Codex sandbox.
* Do not use `apt`, `pip`, `pipx`, `npm`, `cargo install`, or similar package managers in the Codex sandbox to satisfy project dependencies.
* Project dependencies must be declared in the repository and installed inside project Docker images/containers.
* Dockerfiles, Compose configuration, package manifests, and lock files are the source of truth for the executable environment.
* If a dependency is missing, update the appropriate Dockerfile, package manifest, lock file, or Compose configuration and rebuild the image. Do not work around the problem by installing it directly into the Codex sandbox.
* Do not silently fall back to running project commands directly in the Codex sandbox when container execution fails. Report the failure or fix the container definition.

### Running project code

Project executables and development tools must normally run inside project containers.

This includes:

* converter execution
* state-checker execution
* unit tests
* integration tests
* linters
* formatters
* type checkers
* dependency-dependent helper scripts

The Codex sandbox may directly use only tools needed to inspect and orchestrate the repository and containers, such as Git, Docker/Compose, shell utilities, and repository editing tools.

### Tests

Run tests through the repository-defined container workflow.

Prefer stable repository commands such as:

* `docker compose build`
* `docker compose run --rm test`

or thin repository scripts / Make targets that ultimately invoke the same containerized workflow.

Do not run a host/sandbox-installed copy of the test framework as a substitute for the containerized test environment.

All tests that are expected to pass before a change is considered complete must be runnable from a clean Docker image built from the committed repository state.

### Reproducibility

A fresh Docker Sandbox with Docker available must be able to build the project images and run the full test suite without manually installing project-specific packages into the sandbox.

Do not rely on packages or modules that happen to have been installed during an earlier Codex session.

### Credentials and host boundaries

Project containers should not receive Git credentials or other host credentials unless a specific feature explicitly requires them.

Git repository operations and deployment orchestration are separate from the application runtime. Do not add Git push capability to converter, checker, or test containers merely for convenience.
