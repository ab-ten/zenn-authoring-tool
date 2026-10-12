# コマンドライン運用ガイド（Phase 0 MVP）

この文書は Zenn authoring tool を **Windows PowerShell + Docker Desktop** から利用するときの手順書です。内部仕様は [architecture.md](architecture.md)・[use-cases.md](use-cases.md) 等を参照してください。Linux / macOS でも同じ container 内コマンドを利用できますが、以下のホスト例は PowerShell 用です。

> **重要:** ツール開発 repository と記事 repository は別物です。Git commit / push / restore はホストでユーザー自身が行います。以下の `C:\work\...` は必ず自分のパスに置き換えてください。

## `invoke.cmd` による日常操作（推奨）

`invoke.cmd` は **ツール開発 repository の直下**に配置します。Windows PowerShell / cmd.exe から実行でき、執筆 repository の **カレントディレクトリ**を記事 root として使用します。内部の Python CLI には常に `--article-root /article-repo` を明示するため、Python CLI の CWD fallback 禁止ルールは変更されません。

**PowerShell 例：**

```powershell
Set-Location 'C:\work\my-zenn-articles'
$Invoke = 'C:\work\zenn-authoring-tool\invoke.cmd'

& $Invoke preview
& $Invoke converter lint --all
& $Invoke converter lint --article articles/example.md
& $Invoke converter convert
& $Invoke checker check
```

**Preview の公開先を変更する場合：**

```powershell
& $Invoke preview --listen 127.0.0.1:8001
& $Invoke preview --listen 0.0.0.0:8000
```

これらはどちらか一方を選んで起動します。前者はローカルのみ、後者はホストの全ネットワークインターフェースに公開する指定です。LAN 等からアクセスできる可能性があるため、後者は必要な場合だけ使用してください。指定したポートが使用中なら Docker が起動を拒否します。

**初期化時（Zenn username が必須）：**

```powershell
& $Invoke checker init ZENN_USERNAME
```

`invoke.cmd` の第1引数は `converter` / `checker` / `preview` のいずれかです。Python CLI が要求する追加引数（`--all`、`--article`、article UUID、手動確認フラグなど）はそのまま指定します。`--article-root` は指定しません。

**PowerShell からのコマンド例：**

```powershell
& $Invoke checker confirm-pending-update ARTICLE_ID `
  --confirmed-deploy-success --confirmed-content-match
```

```powershell
& $Invoke checker discard-all-pendings `
  --confirmed-deploy-terminal --confirmed-remote-safe --confirmed-abandon-all
```

上記の手動確認フラグは、各条件を実際に確認した後にのみ指定してください。破棄コマンドは Git restore を自動実行しません。

- `invoke.cmd` は起動したシェルのカレントディレクトリを使用します。別 directory で実行しないでください。
- `converter lint` は authoring を read-only mount します。
- `converter convert` とすべての `checker` サブコマンドは `_deploy` のみを read-write mount します。
- `preview` は専用 Previewer image を起動し、既定では `http://localhost:8000` で表示します。終了は Ctrl-C です。
- `preview --listen IPv4_ADDRESS:HOST_PORT` は Docker のホスト側公開アドレスとポートだけを変更します。コンテナ内部は `8000` のままです。裸のポート番号だけの指定はできません。
- Publisher / Previewer image は事前にビルドしてください。**Git commit / push / restore は wrapper に含みません。**
- 現行 `invoke.cmd` は通常の Windows ローカルパスを想定し、Docker の `--mount` 引数で特殊な意味を持つカンマ `,` を含むパスには対応しません。

以下は wrapper を使わずに直接 Docker を起動する場合の詳細手順です。異常時に低レベルコマンドで切り分ける用途にも利用できます。

## 共通パス変数と Docker mount

直接 Docker コマンドを組み立てる場合、PowerShell で設定します。両パスは**存在する絶対パス**を指定してください。

```powershell
$ToolRoot = (Resolve-Path 'C:\work\zenn-authoring-tool').Path
$ArticleRoot = (Resolve-Path 'C:\work\my-zenn-articles').Path
$DeployRoot = Join-Path $ArticleRoot '_deploy'

$PublisherMount = "type=bind,source=$ToolRoot\publisher,target=/src,readonly"
$AuthoringMount = "type=bind,source=$ArticleRoot,target=/article-repo,readonly"
$DeployMount = "type=bind,source=$DeployRoot,target=/article-repo/_deploy"
$PreviewMount = "type=bind,source=$ArticleRoot,target=/authoring,readonly"
```

- `PublisherMount`: Python ソースコード（read-only）。Publisher image の中にアプリケーションソースをコピーする方式ではありません。
- `AuthoringMount`: 実際の記事 repository（read-only）。Git 情報もコンテナから参照できます。
- `DeployMount`: `_deploy` を個別に read-write mount。**Converter / Checker init / check / 手動確定 / 破棄のときのみ使用**します。
- `PreviewMount`: Previewer 専用の read-only mount。`_deploy` を別途 mount しません。

`--mount` は指定元のディレクトリが存在しなければ失敗します。`:readonly` の付け忘れに注意してください。Docker Desktop は Linux containers モードで使用します。

### イメージのビルド

開発 repository のルートで実行します。`make build` は Publisher だけをビルドします。

```powershell
Set-Location $ToolRoot
make build-publisher
make build-previewer
```

```powershell
make test
```

上記 `make test` は Publisher の全テストです。Previewer の Node テスト：

```powershell
docker run --rm --entrypoint node zenn-authoring-previewer `
  --test /opt/previewer/tests/generate-links.test.mjs
```

Make が利用できない場合は、リポジトリの `Makefile` に記載された `docker build` / `docker run` と同等のコマンドを実行してください。ランタイム依存パッケージをホストへ別途インストールしないでください。

## 初回セットアップ

### 記事 repository の Git 構成

対象は新規または管理用の **記事 repository** です。開発 repository ではありません。

1. Authoring branch は `main` または `master`。`_deploy/` を `.gitignore` に記載し、必要な初期 commit を作成します。
2. ホストで `worktree.useRelativePaths = true` に設定します。
3. `deploy` branch の linked worktree を `_deploy` に作成します。
4. `_deploy/.zenn-authoring` を作成します。
5. `checker init` で Zenn user ID・管理ファイルを作成します。
6. 初期管理ファイルを目視確認し、`[skip ci]` 付きで deploy を commit / push します。

既存 Git repository のブランチ構成を無条件に変更しないでください。新規構築時の worktree 操作例：

```powershell
git -C "$ArticleRoot" config worktree.useRelativePaths true
git -C "$ArticleRoot" worktree add --orphan -b deploy _deploy
New-Item -ItemType Directory -Force -Path (Join-Path $DeployRoot '.zenn-authoring')
```

`git worktree add` の前に authoring の初期 commit が必要です。Git はホスト・コンテナともに 2.48 以降が前提です。GitHub に Zenn 連携先の `deploy` branch を設定する作業はホスト / Zenn 側で別途実施します。

### Checker init

`USERNAME` を実際の Zenn username に置き換えます。この操作は Zenn user API を参照し、`_deploy/.zenn-authoring/config.json` と `ledger.json` を作成する可能性があります。

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  --mount "$DeployMount" -w /src zenn-authoring-publisher `
  python -m checker init USERNAME --article-root /article-repo
```

初期化結果と `_deploy` の差分をレビューして、管理ファイルだけを stage / commit / push します。初期化時の `deploy` が unborn であっても、正しい構成なら `init` は受け付けます。初回 commit は必ず `[skip ci]` を使用します。

```powershell
git -C "$DeployRoot" status --short
git -C "$DeployRoot" diff -- .zenn-authoring
git -C "$DeployRoot" add -- .zenn-authoring/config.json .zenn-authoring/ledger.json
git -C "$DeployRoot" diff --cached
git -C "$DeployRoot" commit -m 'Initialize Zenn management state [skip ci]'
git -C "$DeployRoot" push -u origin deploy
```

`init` は管理状態の初期化であり、記事の公開確認ではありません。

## 執筆中の記事を Preview

```powershell
docker run --init --rm -it --publish 127.0.0.1:8000:8000 `
  --mount "$PreviewMount" zenn-authoring-previewer
```

<http://localhost:8000> を開きます。`articles/` と `images/` は必要で、後者は空でも構いません。

- `#id` がある記事を Previewer が対象にします。`#slug` が未設定なら `new-<normalized-uuid>` がプレビュー用 filename になります。
- Previewer は `#id` / `#slug` のみを解釈し、他の管理コメントを検証しません。管理情報の広範な妥当性は Publisher lint を使用します。
- Windows の変更通知には依存せず、`zenn preview --no-watch` を使用します。本文編集後はブラウザを更新してください。
- 記事の追加・削除・改名・`#slug` 変更時は、コンテナを終了して再起動します。
- `--init` を指定して Ctrl-C による終了を安定させます。
- 記事の UUID や仮 slug の衝突があるとプレビュー起動を拒否します。

`previewer/README.md` に生成 symlink の詳細があります。

## Commit 前に Lint

全記事：

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  -w /src zenn-authoring-publisher `
  python -m converter lint --article-root /article-repo --all
```

指定した記事だけ：

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  -w /src zenn-authoring-publisher `
  python -m converter lint --article-root /article-repo `
  --article articles/example.md --article articles/notes/another.md
```

対象に `hold`、untracked、staged、unstaged の記事を含められます。現在のファイル内容を読み、Git の cleanliness や ledger・remote 状態を検査しません。`--article` は選択記事のみ、`--all` は全記事の UUID 重複を検出します。

| exit code | 意味 |
| --- | --- |
| 0 | 対象範囲のローカル検証が完了、エラーなし |
| 1 | 記事内容の診断エラーあり |
| 2 | root / 対象 / filesystem 等のエラーで検証未完了 |
| 130 | 中断 |

`lint = 0` であっても「公開準備 OK」ではありません。release 遷移、slug の全体一意性、remote collision、画像参照、future published_at の publish 許可などは別の責務です。詳しくは `publisher/LINT.md` を参照してください。

## Authoring source の基本書式

`articles/` 以下なら、任意の人間に分かりやすい `.md` filename（サブディレクトリ可）を使えます。ファイル名は slug ではありません。

初期執筆中の `hold` 記事の例：

```markdown
---
title: "Example article"
emoji: "✨"
type: tech
topics: [python]
published: false
---
<!-- #id:6a5c05e8-401a-4e2b-bc92-2c9aa61be305 -->
<!-- #state:hold -->

# Example

記事本文です。
```

`#id` は UUID、`#state` は `active` / `hold` / `erased`。公開前は `#slug` と `#release` を省略できます。Front Matter 直後の管理コメントは、Converter が deploy 記事を生成するときに除去します。

新規公開時は少なくとも、管理コメントに `#slug` と `#release:1` を追加し、`#state:active`、`published: true` にします。slug は小文字英数字・`_`・`-` の12〜50文字です。`published_at` は省略可能ですが、設定する場合は許容形式を守り、予約公開は使用しません。

## 公開操作前の安全確認

- `active` / `erased` の publish input は **Git HEAD に tracked、staged/unstaged 差分なし**にする必要があります。`hold` は dirty 可。
- authoring の `images/` は deploy への authoritative mirror であり、すべての対象ファイルを clean・tracked にします。untracked / ignored 画像は converter に拒否されます。
- deploy worktree 全体が clean、ledger の pending が空であること。Converter は既存の pending があると新たな publish を開始しません。
- 画像拡張子は `.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` の小文字のみ。ファイルサイズは 3,000,000 bytes 以下です。
- Markdown 内画像参照の解決可否は Converter の検証外です。Previewer と人間による確認が必要です。

作者側の状態と予定 release の変更をコミットするのは、ユーザーによるホスト Git 操作です。新規は release `1`、既存の記事更新は通常 successful release `N` から `N+1` へ進めます。ただし内容が変わらない場合は N のままです。`erased` 後の再公開、削除、日時・slug の制約は `docs/use-cases.md` に従ってください。

## Converter: deploy 計画の適用

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  --mount "$DeployMount" -w /src zenn-authoring-publisher `
  python -m converter convert --article-root /article-repo
```

Converter は local planning → preflight（remote 照会あり）→ apply の順に実行し、全体の preflight が OK なら `_deploy` の記事・画像および必要な pending state を更新します。**dry-run コマンドではありません**。成功しただけでは Zenn 上での公開を意味しません。

| exit code | 意味 |
| --- | --- |
| 0 | ローカル変換完了（`NO_CHANGES` も含む） |
| 1 | preflight の COLLISION |
| 2 | 変換 / preflight / apply 等の ERROR |
| 130 | 中断 |

成功したら、ホストの deploy worktree で変更点を確認します。

```powershell
git -C "$DeployRoot" status --short
git -C "$DeployRoot" diff
git -C "$DeployRoot" diff --check
```

**必ず意図した変更だけを stage し、`git diff --cached` で確認してから**通常の deploy commit を行います。記事操作の pending state を勝手に削除したり、converter による変更を別操作で上書きしないでください。

```powershell
git -C "$DeployRoot" diff --cached
git -C "$DeployRoot" commit -m 'Publish article'
git -C "$DeployRoot" push origin deploy
```

この `commit` の前に、意図したファイルの `git add` をホストで実行してください。上記は stage 済みであることを前提とします。通常の **deploy-triggering commit に `[skip ci]` を付けない**でください。

画像だけの変更も Converter で mirror してから同じように deploy します。画像の公開結果は Zenn Dashboard のデプロイ履歴で確認します。

## Checker: Zenn 反映状況の確認

Zenn の deploy が実行されたあと、次を呼び出します。Checker は一度だけ API を照会し、状態に応じて `_deploy` の ledger / released snapshot / cleanup を変更する可能性があります。

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  --mount "$DeployMount" -w /src zenn-authoring-publisher `
  python -m checker check --article-root /article-repo
```

| exit code | 意味 |
| --- | --- |
| 0 | `COMMIT_READY` |
| 1 | `NOT_READY` |
| 2 | Checker ERROR（ready を確定できない） |
| 130 | 中断 |

`SUCCESS` は各記事の意味的成功、`COMMIT_READY` は **管理変更を commit できる全体状態**です。記事の SUCCESS だけを見て管理ファイルを commit しないでください。

- **COMMIT_READY**: `_deploy` の変更を確認し、管理・cleanup commit を `[skip ci]` 付きで commit / push。
- **NOT_READY**: 管理変更を commit / push しない。`PENDING` なら新しい deploy を発生させず、必要に応じて後で `check` を再実行。
- **ERROR**: ledger・snapshot・deploy artifact・Git の状態を調べ、復旧手順を確認。結果を成功とみなさない。
- **画像だけ**: article ledger を進めない。画像の成功・失敗は Zenn Dashboard を確認。

`COMMIT_READY` の場合のホスト操作例（変更内容を確認し、目的のファイルを stage した後）：

```powershell
git -C "$DeployRoot" status --short
git -C "$DeployRoot" diff
git -C "$DeployRoot" diff --cached
git -C "$DeployRoot" commit -m 'Confirm Zenn management state [skip ci]'
git -C "$DeployRoot" push origin deploy
```

`[skip ci]` を付けるのは **管理状態や cleanup だけ**を commit / push する場合です。Zenn がデプロイ対象とする記事・画像の通常変更に付けないでください。

## 例外対応コマンド（通常は使わない）

### Pending UPDATE の手動確定

`check` で PENDING のまま残った UPDATE に対し、ユーザーが **Zenn Dashboard で対象 deploy の成功**と、**現時点の remote content が deploy artifact に一致**することを確認した場合にだけ使用できます。Checker 自身は内容一致を検証しません。

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  --mount "$DeployMount" -w /src zenn-authoring-publisher `
  python -m checker confirm-pending-update ARTICLE_ID `
  --article-root /article-repo `
  --confirmed-deploy-success --confirmed-content-match
```

成功終了コード `0` は `COMMIT_READY` ではありません。**必ず続けて通常の `checker check` を実行**し、結果を確認します。

### 全 pending の破棄（復旧操作）

次のすべてを確認した場合に限る、管理状態を実際に変更する操作です：対象 deploy が terminal で後から適用されないこと、全 pending について operation ごとの remote 整合性、全 pending を破棄する意思。さらに Git HEAD と直前履歴・対象 artifact 等の preflight 条件も必要です。

```powershell
docker run --rm --mount "$PublisherMount" --mount "$AuthoringMount" `
  --mount "$DeployMount" -w /src zenn-authoring-publisher `
  python -m checker discard-all-pendings --article-root /article-repo `
  --confirmed-deploy-terminal --confirmed-remote-safe --confirmed-abandon-all
```

このコマンドは **pending をローカルで clear するだけ**で、Git restore は実行しません。実行後に表示される `git restore ...` の計画をホストで確認し、必要な対象に対してのみ実行してください。復旧中は未完了状態を commit / push しないでください。restore 後に `checker check` を行い `COMMIT_READY` を確認してから、復旧の管理・cleanup commit に `[skip ci]` を使用します。

中断やエラーによって pending がすでに空になっていた場合、**discard の再実行を試みず**、Git 履歴と ledger と記事状態から手動復旧してください。

## 操作別の早見表

| やりたいこと | 操作 |
| --- | --- |
| 執筆途中を表示する | Previewer を起動、ブラウザで更新 |
| dirty / hold の記事を検証する | `converter lint --all` または `--article` |
| 新規公開する | slug・release 1・active・published true → authoring commit → convert → deploy commit/push → check |
| 公開済みを改稿する | 一度 hold にして執筆 → preview/lint → active、正しい次 release → authoring commit → convert → check |
| 記事を消す | state erased・release 維持 → authoring commit → convert → deploy commit/push → Dashboard で削除 → check |
| 消した記事を再公開する | 同じ ID、状態 active、適切な release と slug → convert → check。slug 変更には制限あり |
| 画像だけ更新する | authoring images を commit → convert → deploy commit/push → Dashboard の画像 deploy を確認 |
| check が PENDING | 新規 deploy を開始せず再 check。条件を満たす UPDATE のみ手動確定可能 |
| pending をあきらめて復旧する | 厳格な条件を確認 → discard-all-pendings → ホスト restore → check |

## 制約と注意点

- 実 Zenn への deploy / US-10・画像失敗 retry 等は運用実測で最終確認する必要があります。
- 予約公開（未来の `published_at`）は Phase 0 MVP で非対応です。
- Checker の成功は image deployment 成功の証明になりません。
- `check` が `COMMIT_READY` でも、**CREATE SUCCESS の snapshot 更新後・ledger 更新前の中断で孤立 snapshot を見落とす可能性**が既知です。実運用前の安全性レビュー対象です。
- 各コマンドの詳細条件は 4仕様文書を優先します。コマンドの実引数は `python -m converter --help`、`python -m checker --help`、各 subcommand の `--help` でも確認できます（Publisher container 内で実行）。

## 外部資料

- [Zenn 公式：Zenn CLI で記事・本を管理する方法](https://zenn.dev/zenn/articles/zenn-cli-guide)
- [Docker 公式：Bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)
