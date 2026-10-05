# Design Decisions

## Scope

This document records the rationale behind major design choices in the Zenn authoring tool.

It does not redefine domain semantics or operational behavior. Normative definitions and workflows remain in:

- [`domain.md`](domain.md) for domain values and lifecycle semantics;
- [`architecture.md`](architecture.md) for representation, validation, publish planning, state checking, and recovery; and
- [`use-cases.md`](use-cases.md) for user-facing workflows and observable outcomes.

The purpose of this document is to preserve why the current design was chosen, especially where simpler-looking alternatives would weaken safety, recovery, or practical usability.

## Separate authoring and deploy branches with Git worktrees

### Decision

Use one Git repository with:

- `main` or `master` as the authoring branch; and
- `deploy` as the Zenn-connected deployment and management branch.

The deploy branch is checked out as a linked worktree at `/_deploy` directly under the authoring worktree.

The authoring branch ignores `/_deploy/`.

Use relative worktree links with:

`worktree.useRelativePaths = true`

so that the same repository/worktree relationship can be used from both the host and container even when their absolute filesystem paths differ.

The host and container require Git 2.48 or later.

### Rationale

Authoring commits should not automatically become Zenn deployment inputs. Drafts and in-progress work need to be commit-able without causing unnecessary deployment.

Using worktrees also allows the authoring branch and deploy branch to be accessed at the same time without repeatedly switching the checked-out branch.

Keeping the deploy worktree at a stable relative location also allows the host and container to observe the same repository layout without embedding host-specific absolute worktree paths into Git metadata.

### Consequences

The deploy branch is treated as a generated and managed representation of the authoring state rather than as the primary editing surface.

Deployment requires an explicit conversion, diff review, commit, and push.

See [`architecture.md`](architecture.md#repository-and-worktree-layout).

## Run authoring tools inside Docker with a narrow trust boundary

### Decision

Run the converter, state checker, preview environment, and their dependencies inside Docker.

Expose only files required for Zenn authoring and deployment to the container.

### Rationale

The toolchain depends on large Node.js and Python dependency trees. Keeping those dependencies inside the container limits their direct access to the host filesystem and narrows the set of host files exposed to the toolchain.

The container does not need unrestricted access to unrelated host files.

### Consequences

The authoring worktree is exposed read-only to the container.

The deploy worktree at `/_deploy` is exposed read-write because converter and state-checker operations must update generated and management files there.

Git metadata for both worktrees is exposed read-only.

The container may inspect Git state but does not modify Git metadata.

Operations that require broader host control remain outside the container.

See [`architecture.md`](architecture.md#execution-environment-and-responsibility-boundary).

## Keep Git history operations on the host

### Decision

Do not let the converter or state checker perform Git history operations such as commit, push, or history rollback.

The user performs those operations on the host after reviewing the tool-produced worktree changes or recovery instructions.

Git commands executed inside the container are limited to read-only inspection. Git status inspection uses `--no-optional-locks`, and operations that modify refs, indexes, worktree metadata, or repository history remain host-side responsibilities.

### Rationale

Keeping Git history operations on the host preserves an explicit review boundary between tool-produced worktree changes and repository history changes.

It also keeps recovery inspectable: the tool can prepare or describe required worktree changes without itself performing history rollback.


### Consequences

The normal workflow deliberately includes user-visible `git diff`, commit, and push steps.

Recovery commands such as restore operations are presented for host-side execution rather than being performed automatically inside the container.

See [`architecture.md`](architecture.md#execution-environment-and-responsibility-boundary).

## Use an explicit sequential, make-like conversion model

### Decision

Assume explicit, sequential converter execution by a single user.

Concurrent conversions and modification of participating authoring sources while conversion is running are unsupported operations.

Do not add converter-side locking or mid-run source-change detection for the current design.

### Rationale

The converter is a local generation tool, not a concurrent service.

For the intended single-user workflow, the deploy worktree's dirty state and explicit `git diff` review provide a simpler recovery boundary for interrupted or unexpected conversions.

Adding concurrency control would increase implementation complexity without addressing a supported workflow.

### Consequences

Users must not run multiple conversions concurrently or edit participating sources during conversion.

Unexpected partial results are detected and recovered through the deploy worktree state and Git review.

See [`architecture.md`](architecture.md#execution-model).

## Classify each article operation once

### Decision

Determine each article's `{operation}` once from source state, ledger confirmed state, and artifact comparison during converter planning.

Later validation, preflight, and apply phases must not reclassify that operation.

### Rationale

Release checks, slug checks, remote preflight, and deploy application must all refer to the same intended transition.

Reclassifying later could make validation or remote checks describe a different transition from the one eventually applied.

### Consequences

Operation-specific checks validate the already selected operation rather than inferring another one.

The state checker receives the operation as part of the publish plan.

See [`architecture.md`](architecture.md#article-state-and-operation-classification).

## Finalize one immutable publish plan before preflight and apply

### Decision

Build the complete publish plan before modifying the deploy worktree.

After article entries and image mirror differences have been collected, treat the final publish plan as immutable for the remainder of conversion.

Pass exactly the same publish plan to state-checker preflight and to deploy application.

### Rationale

The remote checks and the local changes must describe the same candidate deployment.

Using one fixed plan prevents preflight from approving one set of operations while apply performs another.

### Consequences

If preflight fails, the plan is not applied.

`pending_next_state` is derived from the same fixed plan after deploy-worktree changes succeed.

See [`architecture.md`](architecture.md#publish-plan).

## Treat deploy `/articles` and `/images` as tool-managed outputs

### Decision

Treat deploy `/articles` and deploy `/images` as generated or synchronized outputs.

Users do not directly edit them.

### Rationale

The deploy worktree needs a clear ownership boundary so that converter output, state-checker cleanup, diff review, and recovery can be interpreted reliably.

Direct manual edits would blur whether a difference came from the authoring source, the converter, recovery, or an unintended local change.

### Consequences

Authoritative article edits occur in the authoring source.

Authoritative image edits occur in authoring `/images`.

User interaction with generated deploy content is limited to diff review, Git history operations, and explicitly instructed recovery operations.

See [`architecture.md`](architecture.md#deploy-worktree-invariants).

## Keep image lifecycle independent from article lifecycle

### Decision

Synchronize authoring `/images` to deploy `/images` as a mirror without giving images article-style state, release numbers, ledger entries, released snapshots, or pending state.

Evaluate image deployment results independently from article semantic results.

### Rationale

Image assets do not need the article publication lifecycle to satisfy the current workflow.

For example, replacing bytes at the same image path should not create a new article release when the article Markdown itself is unchanged.

A mirror model keeps image handling simple while allowing preview and deployed articles to use the same `/images/...` paths.

### Consequences

Image-only changes do not change article release or ledger state and do not create `pending_next_state`.

Unused images are not automatically removed. Deletion is explicit through the authoritative authoring image tree.

If article Markdown changes an image reference, that Markdown change follows normal article release rules.

Image deployment failure has no fixed automatic recovery procedure; the user corrects the authoritative inputs and retries the normal workflow.

See [`architecture.md`](architecture.md#image-workflow).

## Use the non-public Zenn article API behind a small abstraction

### Decision

Use the non-public Zenn article endpoint needed by the state checker for publication existence, ownership, collision, and observation checks.

Keep API-dependent interpretation behind small, isolated functions.

### Rationale

The authoring tool needs to distinguish:

- no publication for a slug;
- the user's own publication;
- another user's publication; and
- changes to an existing remote publication.

Without remote observation, several safety checks would have to depend on manual Dashboard inspection and could not be incorporated into preflight and state checking.

The interface is non-public and its exact behavior may change, so broad coupling to its response format would make maintenance unnecessarily expensive.

### Consequences

The design accepts dependency on an undocumented and potentially changing interface.

API-dependent behavior must be empirically verified, and changes should be isolated to the API interpretation layer where possible.

Background discussion considered by the design is recorded in `zenn-dev/zenn-community` issue `#209`.

See [`architecture.md`](architecture.md#zenn-article-api).

## Keep the initial state checker one-shot

### Decision

Have `state checker check` perform one remote observation per relevant article and then exit.

If another observation is needed, the user reruns `check`.

### Rationale

A one-shot checker keeps the initial implementation simple and predictable and avoids turning the checker into a long-running polling process.

The user remains in control of when another remote observation is performed.

### Consequences

A result may remain `PENDING` across multiple explicit invocations.

Bounded repeated polling may be considered later, but it is not part of the initial behavior.

See [`architecture.md`](architecture.md#state-checker).

## Serialize unresolved article deployments with `pending_next_state`

### Decision

Represent unresolved article deployment candidates in `pending_next_state`.

While any pending article exists, do not allow another deployable publish candidate to be applied or persisted.

Temporary candidate artifacts and publish-plan entries may still be constructed as in-memory planning data. Preflight enforces the pending guard before any such plan is applied to the deploy worktree.

Image-only changes do not create pending article state.

### Rationale

Remote observations must be attributable to one known article candidate and, for UPDATE, to one known `{baseline-observation}`.

Allowing a later deploy-triggering commit while the earlier result is unresolved would make it unsafe to determine which deployment caused a remote change.

### Consequences

Pending article state must be resolved to terminal results, or the unresolved candidate set must be explicitly discarded through `discard-all-pendings`, before another deploy-triggering workflow can begin.

`SUCCESS` and `COLLISION` remove terminal pending entries according to their operation-specific rules; unresolved results retain them.

Image-only changes do not create pending article state, but an unresolved article deployment still prevents starting another deploy-triggering workflow.

See [`architecture.md`](architecture.md#pending_next_state).

## Use `[skip ci]` for management-only commits

### Decision

Commit and push management-only or cleanup changes on the deploy branch using `[skip ci]`.

### Rationale

Those commits persist local management state after a deployment result has already been determined. They must not trigger another Zenn deployment.

### Consequences

Normal deploy-triggering commits do not use this marker.

Management and cleanup commits after `COMMIT_READY` do use it.

See [`architecture.md`](architecture.md#deploy-branch).

## Adopt intentional out-of-band remote edits explicitly

### Decision

Provide `state checker import --adopt-remote` for intentional article changes made outside the authoring workflow, such as an emergency Zenn Web UI edit.

Adopt such a change explicitly as a new successful release after the authoring source has been reconciled with the current remote content.

### Rationale

An intentional remote content change is different from remote metadata drift.

Silently updating only the remote observation would lose the content change from authoring history, while treating it as an ordinary pending deployment would misrepresent how the change was created.

Explicit adoption preserves the article's managed history while making the already-established remote content the next successful release.

### Consequences

Adoption is an explicit user action with strict preconditions.

It does not create `pending_next_state`, run normal preflight, or perform image mirror synchronization.

After adoption, normal commit-readiness checking is used and the management changes are committed with `[skip ci]`.

See [`architecture.md`](architecture.md#import---adopt-remote).
