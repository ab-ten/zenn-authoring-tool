# Domain Model

## Scope

This document defines the domain concepts and state semantics used by the Zenn authoring tool.

It describes what each domain value means and how the authoring-side intent, confirmed state, and unresolved article transitions relate to one another. Concrete file formats, ledger field layout, validation rules, transition classification, remote checks, recovery procedures, and command workflows are defined outside this document.

## Representation terminology

The following terms distinguish storage-independent content from filesystem representations.

### content

Content denotes information or a value independently of where it is stored.

Content may exist only as an in-memory value and does not imply that a corresponding file exists.

### artifact

An artifact is a file materialized on the filesystem.

A term ending in `artifact` refers to a file rather than to abstract storage-independent content.

### authoring source

An authoring source is an input article file in the authoring worktree.

It may contain authoring-tool management comments in addition to the article content intended for Zenn.

### candidate article content

Candidate article content is a temporary `{article-content}` generated inside the converter from an authoring source.

It exists only while the converter is processing the article and is used for release determination, operation classification, and publish-plan generation.

After it has been materialized as a deploy artifact, it is not treated as a persistent candidate object.

### deploy artifact

A deploy artifact is an article file materialized at `deploy /articles/<slug>.md`.

It is the local article file used when the state checker needs the article content represented by the deploy worktree.

### released snapshot

A released snapshot is the file stored at `/.zenn-authoring/released/<article-id>.md`.

In a completed successful-state transition, it contains the latest confirmed successful `{article-content}` for the logical article.

During interrupted SUCCESS terminal-result application, the released snapshot may temporarily contain pending release content before the corresponding ledger transition has committed.

The pending article transition remains the recovery boundary until successful-state commit completes.

### remote article content

Remote article content is the article content currently provided by the corresponding publication on Zenn.

## Article content and release

### `{article-content}`

`{article-content}` is the storage-independent Markdown content for one article to be deployed to Zenn.

It is generated from the authoring source by removing authoring-tool management comments and contains the Zenn Front Matter and article body.

`{article-content}` describes content rather than a particular filesystem representation.

During conversion, an authoring source produces candidate article content. When that content is materialized into the deploy worktree, it becomes the content of a deploy artifact. The candidate article content does not persist as a separate object after that materialization.

A successful `CREATE`, `UPDATE`, or `RECREATE` establishes the corresponding deploy artifact content as the latest successful `{article-content}`, which is retained in the released snapshot.

### `{release-number}`

`{release-number}` identifies the revision sequence of `{article-content}` for one logical article. In practical terms, it is the version number of the article content.

A `{release-number}`:

- is a natural number starting from `1`;
- increases monotonically for each article;
- never returns to a number that has already been used;
- does not skip numbers; and
- is not a deploy-attempt number.

Redeploying the same `{article-content}` does not increment `{release-number}`.

If the latest release's `{article-content}` is unchanged, the same `{release-number}` is used.

If the `{article-content}` differs from the latest release, the next `{release-number}` is used. This applies even when the new content is identical to an older release: if it differs from the latest release, the number increases by one.

Changes that are not part of `{article-content}` do not increment `{release-number}`. In particular:

- image-only changes do not increment it; and
- changes only to authoring-tool management information do not increment it.

## Pending article transition

`pending_next_state` stores article transitions whose publish result has not yet been established as successful.

A pending article transition represents a change that has already been reflected into the deploy worktree by conversion but whose resulting successful state is still unresolved.

It is not:

- persistent candidate article content;
- a persistent publish-candidate object; or
- the Zenn-side deploy status itself.

At this stage, candidate article content no longer exists as a separate object. Local article content is represented by the deploy artifact.

A pending article transition remains unresolved until one of the following occurs:

- `SUCCESS`: the confirmed successful state is updated and the pending entry is removed;
- `confirm-pending-update`: an eligible UPDATE transition is established as successful from explicit user confirmation, using the same successful-result semantics as normal UPDATE success;
- `COLLISION`: the failed deploy artifact is cleaned up, the previously confirmed successful state is preserved, and the pending entry is removed; or
- `discard-all-pendings`: the user explicitly abandons all unresolved article transitions while preserving the previously confirmed successful state.

The detailed eligibility rules, local validation, remote classification, cleanup procedures, atomic update procedures, and command workflows are defined outside this document.

### `{baseline-observation}`

`{baseline-observation}` is the `{zenn-observation}` used as the comparison baseline for determining whether an UPDATE transition has been reflected remotely.

It is the current `{zenn-observation}` captured when UPDATE preflight succeeds.

Once captured, the `{baseline-observation}` remains fixed throughout that publish workflow.

`{baseline-observation}` does not represent a successful state. It represents the remote comparison point that existed before deployment.

Only an UPDATE transition has a `{baseline-observation}`.

### `{observation-override}`

`{observation-override}` is a one-shot authorization by the user to accept a specific remote drift during UPDATE preflight.

Its value is one `{zenn-observation}` representing the remote state that the user has explicitly reviewed and accepted.

`{observation-override}` is used only when the current remote observation differs from the ledger's successful Zenn observation.

It is not a successful state and does not by itself change:

- the ledger confirmed state;
- the released snapshot;
- the successful release; or
- the successful Zenn observation.

An `{observation-override}` is valid only for an UPDATE in which the source declares the next release.

For the override to authorize that UPDATE, the current `{zenn-observation}` must exactly equal the `{observation-override}`. If they differ, the override no longer authorizes the current remote state.

When UPDATE preflight succeeds using an `{observation-override}`, the current `{zenn-observation}` becomes the `{baseline-observation}` for that UPDATE transition.

An `{observation-override}` is one-shot authorization rather than history. It is removed after the corresponding successful release is confirmed and is not carried forward to later releases.

## Publish operation

### `{operation}`

`{operation}` is the authoring tool's classification of the change that a publish workflow intends to make to a Zenn article.

The defined operations are `CREATE`, `UPDATE`, `DELETE`, and `RECREATE`.

#### `CREATE`

`CREATE` creates a Zenn publication for an unpublished logical article.

#### `UPDATE`

`UPDATE` updates the `{article-content}` of an active publication.

#### `DELETE`

`DELETE` removes an active publication from Zenn.

#### `RECREATE`

`RECREATE` creates a Zenn publication again for an erased logical article.

## Article identity and publication

### `{article-id}`

`{article-id}` identifies one logical article managed by the authoring tool.

It remains unchanged throughout the lifecycle of the same logical article and is independent of:

- the source filename;
- `{slug}`;
- `{release-number}`;
- `{article-state}`; and
- `{article-content}`.

An article keeps the same `{article-id}` when it is republished after being erased.

Different managed articles must not share the same `{article-id}`.

`{article-id}` is an authoring-tool concept and is distinct from a Zenn slug or any identifier exposed by the Zenn API.

### `{slug}`

`{slug}` identifies an article publication on Zenn and forms part of its public URL.

A valid `{slug}` uses only:

- `a-z`;
- `0-9`;
- `-`; and
- `_`.

Its length is 12 to 50 characters, and it must be unique as a Zenn article slug across the site.

`{slug}` and `{article-id}` have different roles:

- `{article-id}` identifies the logical article inside the authoring tool;
- `{slug}` identifies the corresponding publication on Zenn.

Once a publication has been created on Zenn, the `{slug}` associated with that publication cannot be changed.

The current `{slug}` of a logical article may nevertheless change when an erased article is published again as a new Zenn publication. This is not treated as changing the slug of the old publication. It is a new association between the same `{article-id}` and a new publication.

### `{zenn-user-id}`

`{zenn-user-id}` identifies the Zenn user account that owns publications managed by this authoring repository.

Its concrete value is the numeric `user.id` obtained from Zenn user information.

`{zenn-user-id}` is the canonical identity used when determining whether a remotely observed publication belongs to the configured user.

It is distinct from:

- the Zenn username;
- the Zenn display name;
- `{article-id}`; and
- `{slug}`.

A username is used to resolve the account during repository initialization, but username equality is not used as the publication ownership criterion.

A remote publication is an own-user publication when its observed `user.id` equals the configured `{zenn-user-id}`.

## Article lifecycle

### `{article-state}`

`{article-state}` is the intended lifecycle state of a logical article managed by the authoring tool.

The defined states are `active`, `hold`, and `erased`.

#### `active`

`active` means that the logical article is intended to be maintained as a publication on Zenn.

#### `hold`

`hold` means that the logical article is kept outside publish transitions.

This state allows the article to remain in a working state while it is being written or revised.

#### `erased`

`erased` means that the logical article is intended to have no publication present on Zenn.

### Source state

The source state is the `{article-state}` declared by the authoring source and represents the state the user intends next.

Source state is an intended state. It does not by itself mean that the same state has already been established on Zenn.

### Ledger confirmed state

The ledger confirmed state is the `{article-state}` whose establishment on Zenn has been confirmed by the authoring tool.

A confirmed state is either:

- `active`; or
- `erased`.

`hold` is not a confirmed state because it is a source-side working state rather than a state established on Zenn.

Source state and ledger confirmed state represent different stages of confirmation and need not be equal.

## Remote observation

### `{zenn-observation}`

`{zenn-observation}` is a snapshot of remote metadata obtained at a particular time for one Zenn publication.

It is a comparable value used to detect updates to that publication.

A `{zenn-observation}` is not the identity of the publication itself. In particular, it is distinct from `{article-id}` and `{slug}`.

Its concrete representation is composed of metadata that can be obtained from the Zenn API and used for update detection.

Two observations are treated as the same `{zenn-observation}` when all metadata used for update detection are equal.

### Successful Zenn observation

The ledger's successful Zenn observation is the `{zenn-observation}` of the user's own publication from the most recently confirmed successful publication result.

It records successful remote state rather than merely observed remote state.

For successful `CREATE`, `UPDATE`, and `RECREATE`, it is updated to the current `{zenn-observation}` associated with that success.

A successful `DELETE` does not replace it, because no current publication observation exists after deletion.

Observations of publications owned by other users are not stored as the successful Zenn observation.

