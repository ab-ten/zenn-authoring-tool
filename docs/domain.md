# Domain Model

## Scope

This document defines the domain concepts and state semantics used by the Zenn authoring tool.

It describes what each domain value means and how the source, confirmed state, and unresolved publish candidate relate to one another. Concrete file formats, ledger field layout, validation rules, transition classification, remote checks, recovery procedures, and command workflows are defined outside this document.

## Notation

### `{concept-name}`

A value written as `{concept-name}` denotes a value whose semantics are defined as a domain concept in this document.

The braces are meta notation and are not part of the actual value.

## Article artifact and release

### `{article-artifact}`

`{article-artifact}` is the Markdown artifact for one article that is deployed to Zenn.

It is generated from the authoring source by removing the authoring-tool management comments. It contains the Zenn Front Matter and the article body.

### `{release-number}`

`{release-number}` identifies the revision sequence of `{article-artifact}` for one logical article. In practical terms, it is the version number of the article artifact.

A `{release-number}`:

- is a natural number starting from `1`;
- increases monotonically for each article;
- never returns to a number that has already been used;
- does not skip numbers; and
- is not a deploy-attempt number.

Redeploying the same revision does not increment `{release-number}`.

If the latest `{article-artifact}` is unchanged, the same `{release-number}` is used.

If the latest `{article-artifact}` has changed, the next `{release-number}` is used. This applies even when the new artifact is identical to an older release: if it differs from the latest release, the number increases by one.

Changes that are not part of `{article-artifact}` do not increment `{release-number}`. In particular:

- image-only changes do not increment it; and
- changes only to authoring-tool management information do not increment it.

## Article identity and publication

### `{article-id}`

`{article-id}` identifies one logical article managed by the authoring tool.

It remains unchanged throughout the lifecycle of the same logical article and is independent of:

- the source filename;
- `{slug}`;
- `{release-number}`;
- `{article-state}`; and
- `{article-artifact}`.

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

### Confirmed state

The confirmed state is the `{article-state}` whose establishment on Zenn has been confirmed by the authoring tool.

A confirmed state is either:

- `active`; or
- `erased`.

`hold` is not a confirmed state because it is a source-side working state rather than a state established on Zenn.

Source state and confirmed state therefore represent different stages of certainty and may temporarily differ while a publish transition is in progress.

## Unresolved publish candidate

A publish candidate may exist after conversion even though its result has not yet been confirmed as successful.

Such a candidate represents an article transition whose resulting successful state is still unresolved. It is distinct from both:

- the source state, which expresses the user's next intended state; and
- the confirmed state, which records a state already confirmed to exist on Zenn.

The ledger representation named `pending_next_state` stores only article transitions whose result is still unresolved. It represents the authoring tool's unresolved publish candidates and is not itself the Zenn-side deploy status.

The concrete fields of `pending_next_state`, the rules that guard operations while it exists, and the procedures that resolve or discard candidates are outside the scope of this document.

## Remote observation

### `{zenn-observation}`

`{zenn-observation}` is a snapshot of remote metadata obtained at a particular time for one Zenn publication.

It is a comparable value used to detect updates to that publication.

A `{zenn-observation}` is not the identity of the publication itself. In particular, it is distinct from `{article-id}` and `{slug}`.

Its concrete representation is composed of metadata that can be obtained from the Zenn API and used for update detection.

Two observations are treated as the same `{zenn-observation}` when all metadata used for update detection are equal.

### `{baseline-observation}`

`{baseline-observation}` is the `{zenn-observation}` used as the comparison baseline for determining whether an UPDATE candidate has been reflected remotely.

It is the current `{zenn-observation}` captured when UPDATE preflight succeeds.

Once the publish candidate has been generated, the `{baseline-observation}` is not changed.

`{baseline-observation}` does not represent a successful state. It represents the remote comparison point that existed before deployment.

Only an UPDATE candidate has a `{baseline-observation}`.

## Publish operation

### `{operation}`

`{operation}` is the authoring tool's classification of the change that a publish candidate intends to make to a Zenn article.

The defined operations are `CREATE`, `UPDATE`, `DELETE`, and `RECREATE`.

#### `CREATE`

`CREATE` creates a Zenn publication for a logical article that has not previously been published.

#### `UPDATE`

`UPDATE` updates the `{article-artifact}` of an active publication.

#### `DELETE`

`DELETE` removes an active publication from Zenn.

#### `RECREATE`

`RECREATE` creates a Zenn publication again for a logical article whose confirmed state is erased.
