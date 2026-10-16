# コマンドライン運用ガイド（Phase 0 MVP）

この文書は、Zenn authoring tool を **Windows の cmd.exe + Docker Desktop** で利用するための運用手順です。Preview・Lint・Convert・Checker は、すべてツール開発 repository 直下の `invoke.cmd` から実行します。内部仕様は [architecture.md](architecture.md)・[use-cases.md](use-cases.md) 等を参照してください。

> **重要:** ツール開発 repository と記事 repository は別の Git repository です。Git commit / push / restore はホストでユーザー自身が行います。例示する `C:\work\...` は実際のパスに置き換えてください。

## 前提環境と実行場所

- Windows 11 + Docker Desktop（Linux containers モード）を想定します。
- ホスト Git 2.48 以降が必要です（相対 worktree link を使用）。
- Publisher / Previewer の Docker image は事前にビルドしておきます。
- Zenn に接続する GitHub repository と、Zenn username を用意します。

**以下の操作例は、特記しない限り `cmd.exe` で実行します。** 最初に記事 repository のルートへ移動し、ツール開発 repository の `invoke.cmd` を環境変数に設定します。

```bat
cd /d C:\work\my-zenn-articles
set "INVOKE=C:\work\zenn-authoring-tool\invoke.cmd"
```

以後、`"%INVOKE%"` はこの環境変数を参照します。別の `cmd.exe` ウィンドウを開いた場合は、改めて設定してください。

`invoke.cmd` は**起動時のカレントディレクトリ**を記事 repository のルートとして使用します。`_deploy` ディレクトリやツール開発 repository に移動したまま呼び出さないでください。内部の Python CLI には `--article-root /article-repo` が自動で渡されるため、利用者が `--article-root` を指定する必要はありません。

### `invoke.cmd` の担当範囲

| 用途 | 呼び出し |
| --- | --- |
| 記事プレビュー | `"%INVOKE%" preview` |
| 全記事の Lint | `"%INVOKE%" converter lint --all` |
| 指定記事の Lint | `"%INVOKE%" converter lint --article articles/example.md` |
| 公開用の変換 | `"%INVOKE%" converter convert` |
| 管理状態の初期化 | `"%INVOKE%" checker init ZENN_USERNAME` |
| Zenn 反映確認 | `"%INVOKE%" checker check` |
| UPDATE の手動確定 | `"%INVOKE%" checker confirm-pending-update ARTICLE_ID`（確認フラグ必須） |
| 全 pending の破棄 | `"%INVOKE%" checker discard-all-pendings`（確認フラグ必須） |

- `converter lint` と `preview` は記事 repository を read-only で参照します。
- `converter convert` と `checker` の各サブコマンドは、記事 repository を read-only で参照し、`_deploy` のみを書き込み可能にします。
- `invoke.cmd` は Docker の起動と mount 設定を内部で行います。通常の運用で `docker run` や container 内の Python CLI を直接呼び出す必要はありません。
- Git commit / push / restore は `invoke.cmd` の対象外です。ホストの Git を使用してください。
- `invoke.cmd` は、Docker の `--mount` 引数で特殊な意味を持つカンマ `,` を含む Windows パスに対応していません。

### イメージのビルドとテスト（開発作業）

**ビルドとテストは現行 `invoke.cmd` の対象外です。** ツール開発 repository の [Makefile](../Makefile) と [README](../README.md#ビルドとテスト) に従って、Publisher / Previewer image を準備してください。GNU Make が利用できる開発環境での例は次のとおりです。

```bat
cd /d C:\work\zenn-authoring-tool
make build-publisher
make build-previewer
make test
```

`make test` は Publisher のテストです。Previewer の Node テストについては [README](../README.md#ビルドとテスト) を参照してください。`make` がない環境でビルド・テストを行う場合も、ここに記載した `invoke.cmd` の実行コマンドでは代用できません。ホストに Publisher / Previewer のランタイム依存パッケージを別途インストールしないでください。

ビルド作業後、日常操作に戻るときは記事 repository のルートへ移動します。

```bat
cd /d C:\work\my-zenn-articles
```

## 初回セットアップ

### 記事 repository の Git 構成

対象は実際の記事 repository です。ツール開発 repository を記事入力には使用しません。既存 Git repository のブランチ構成を無条件に変更しないでください。

1. Authoring branch は `main` または `master` を使用します。新規 repository の場合はホストで Git を初期化します。
2. Authoring branch の `.gitignore` に `/_deploy/` を記載し、必要な初期ファイルを commit して authoring worktree を clean にします。
3. 相対 worktree link を設定して、`deploy` branch の linked worktree を `_deploy` に作成します。
4. `.zenn-authoring/`、`released/`、`books/` の各ディレクトリを準備します。**この時点では `books/.gitkeep` を作成しません。**
5. `checker init` により Zenn user ID、`ledger.json`、`config.json` を初期化します。
6. 初期化に成功したら `books/.gitkeep` を作成し、管理ファイルとともに確認・commit / push します。

新規 Git repository の場合だけ、必要に応じて実行します。

```bat
git init -b main
```

Authoring branch の初期 commit が完了した状態で、記事 repository のルートから次を実行します。

```bat
git config worktree.useRelativePaths true
git worktree add --orphan -b deploy _deploy
mkdir _deploy\.zenn-authoring\released
mkdir _deploy\books
```

`mkdir _deploy\.zenn-authoring\released` は親の `.zenn-authoring` も準備します。これらの空ディレクトリは Git の未追跡ファイルにならないため、`checker init` の clean worktree 検査を妨げません。

`git worktree add` の前に authoring の初期 commit が必要です。GitHub の deploy branch と Zenn の接続設定はホスト / Zenn 側で別途実施します。

### Checker init

次の `ZENN_USERNAME` を実際の Zenn username に置き換えます。

```bat
"%INVOKE%" checker init ZENN_USERNAME
```

Checker は Zenn user API から数値の `{zenn-user-id}` を取得し、`_deploy/.zenn-authoring/ledger.json` と `config.json` を作成します。初期化時に `deploy` branch が unborn であっても、正しい構成なら受け付けます。

**初期化成功後**に、Git に `books/` ディレクトリを保持するための空ファイルを作成します。

```bat
type nul > _deploy\books\.gitkeep
```

ここより前に `.gitkeep` を作ると、未追跡ファイルとして `checker init` の cleanliness 検査に失敗する可能性があります。

初期管理ファイルと `books/.gitkeep` を確認し、`[skip ci]` 付きで初回 commit します。

```bat
git -C _deploy status --short
git -C _deploy diff -- .zenn-authoring
git -C _deploy add -- .zenn-authoring/config.json .zenn-authoring/ledger.json books/.gitkeep
git -C _deploy diff --cached
git -C _deploy commit -m "Initialize Zenn management state [skip ci]"
git -C _deploy push -u origin deploy
```

`init` は管理状態の初期化であり、記事の公開確認ではありません。`[skip ci]` を付けた初回 commit では Zenn deployment がスキップされます。`books/` を Zenn 側に反映するには、その後の通常の deploy-triggering commit が必要です。

## 執筆中の記事を Preview

記事 repository のルートで実行します。

```bat
"%INVOKE%" preview
```

既定のアクセス先は <http://localhost:8000> です。別のポートでローカル公開する場合は、上記の Preview を終了してから次を実行します。

```bat
"%INVOKE%" preview --listen 127.0.0.1:8001
```

ブラウザから <http://localhost:8001> を開きます。LAN 等へ公開する必要がある場合は、`--listen 0.0.0.0:8000` のように指定できますが、すべてのホストネットワークインターフェースで受け付けるため、必要な場合だけ使用してください。ポートが使用中の場合は Docker が起動を拒否します。コンテナ内部のポートは常に `8000` です。

`articles/` と `images/` は必要で、後者は空でも構いません。

- `#id` がある記事を Previewer が対象にします。`#slug` が未設定なら `new-<normalized-uuid>` がプレビュー用 filename になります。
- Previewer は `#id` / `#slug` のみを解釈し、他の管理コメントを検証しません。管理情報の妥当性は Publisher Lint を使用します。
- Windows の変更通知には依存せず、内部では `zenn preview --no-watch` を使用します。本文編集後はブラウザを更新してください。
- 記事の追加・削除・改名や `#slug` 変更後は、Previewer を終了して再起動してください。
- Previewer の終了は Ctrl-C です。内部では `--init` を指定して終了を安定させます。
- 記事の UUID や仮 slug が衝突すると、プレビュー起動を拒否します。

詳細は [Previewer の README](../previewer/README.md) を参照してください。

## Commit 前に Lint

全記事を検証します。

```bat
"%INVOKE%" converter lint --all
```

指定した記事だけを検証する場合は `--article` を使用します。複数記事も指定できます。

```bat
"%INVOKE%" converter lint --article articles/example.md --article articles/notes/another.md
```

対象には `hold`、untracked、staged、unstaged の記事を含められます。現在のファイル内容を読み、Git cleanliness / ledger / remote / publish readiness は判定しません。`--article` は指定記事だけを検証するため、他記事との UUID 重複は検出しません。

| exit code | 意味 |
| --- | --- |
| 0 | 対象範囲のローカル検証が完了、エラーなし |
| 1 | 記事内容の診断エラーあり |
| 2 | root / 対象 / filesystem 等のエラーで検証未完了 |
| 130 | 中断 |

`lint = 0` は「公開準備 OK」を意味しません。release 遷移、slug の全体一意性、remote collision、画像参照、未来の `published_at` に対する publish 可否などは別の責務です。詳しくは [Publisher Lint](../publisher/LINT.md) を参照してください。

## Authoring source の基本書式

`articles/` 以下なら、人間が分かりやすい任意の `.md` filename（サブディレクトリ可）を使えます。ファイル名と公開 slug は別です。

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

- `active` / `erased` の publish input は **Git HEAD に tracked かつ staged / unstaged 差分なし**にする必要があります。`hold` は dirty で構いません。
- authoring の `images/` は deploy への authoritative mirror です。画像ファイルはすべて clean / tracked にします。untracked / ignored の画像は Converter に拒否されます。
- deploy worktree 全体が clean であり、ledger の pending が空である必要があります。`books/.gitkeep` を追加した場合も commit が済んでいなければ Converter は開始できません。
- 画像拡張子は `.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` の小文字のみ。ファイルサイズは 3,000,000 bytes 以下です。
- Markdown 内画像参照の解決可否は Converter の検証外です。Previewer と人間による確認が必要です。

作者側の state と予定 release の変更を commit するのは、ユーザーによるホスト Git 操作です。新規は release `1`、既存記事の更新は通常 successful release `N` から `N+1` へ進めます。ただし内容が変わらない場合は `N` のままです。`erased` 後の再公開、削除、日時・slug の制約は [use-cases.md](use-cases.md) に従ってください。

## Converter: deploy 計画の適用

```bat
"%INVOKE%" converter convert
```

Converter は local planning → preflight（remote 照会あり）→ apply の順に実行し、全体の preflight が OK なら `_deploy` の記事・画像および必要な pending state を更新します。**dry-run コマンドではありません。** Converter の成功だけでは Zenn 上の公開完了を意味しません。

| exit code | 意味 |
| --- | --- |
| 0 | ローカル変換完了（`NO_CHANGES` も含む） |
| 1 | preflight の COLLISION |
| 2 | 変換 / preflight / apply 等の ERROR |
| 130 | 中断 |

成功後は、ホストで deploy worktree の変更点を確認します。

```bat
git -C _deploy status --short
git -C _deploy diff
git -C _deploy diff --check
```

**意図したファイルだけを stage し、`git diff --cached` で確認してから**通常の deploy commit を行います。stage 対象は実際の変換結果に応じて選択し、新規 artifact や画像も含めてください。記事操作の pending state を勝手に削除したり、Converter の変更を他の操作で上書きしないでください。

次は stage が済んでいることを前提とした例です。

```bat
git -C _deploy diff --cached
git -C _deploy commit -m "Publish article"
git -C _deploy push origin deploy
```

通常の **deploy-triggering commit には `[skip ci]` を付けない**でください。画像だけの変更も Converter で mirror してから同じように deploy します。画像の公開結果は Zenn Dashboard のデプロイ履歴で確認します。

## Checker: Zenn 反映状況の確認

Zenn の deployment 実行後、次を呼び出します。Checker は一度だけ API を照会し、結果に応じて `_deploy` の ledger / released snapshot / cleanup を変更する可能性があります。

```bat
"%INVOKE%" checker check
```

| exit code | 意味 |
| --- | --- |
| 0 | `COMMIT_READY` |
| 1 | `NOT_READY` |
| 2 | Checker ERROR（ready を確定できない） |
| 130 | 中断 |

`SUCCESS` は各記事の意味的成功、`COMMIT_READY` は**管理変更を commit できる全体状態**です。記事の SUCCESS だけを見て管理状態を commit しないでください。

- **COMMIT_READY:** `_deploy` の変更を確認し、管理・cleanup commit を `[skip ci]` 付きで commit / push します。
- **NOT_READY:** 管理変更を commit / push しません。`PENDING` なら新しい deploy を発生させず、必要に応じて `check` を再実行します。
- **ERROR:** ledger・snapshot・deploy artifact・Git の状態を調べ、復旧手順を確認します。結果を成功とみなしません。
- **画像だけ:** article ledger を進めません。画像の成功・失敗は Zenn Dashboard で確認します。

`COMMIT_READY` の場合のホスト操作例です。変更内容を確認した後、実際の管理・cleanup 対象だけを stage してください。新規作成された released snapshot は通常の `git diff` に表示されないので、`git status --short` でも確認します。

```bat
git -C _deploy status --short
git -C _deploy diff
git -C _deploy diff --cached
git -C _deploy commit -m "Confirm Zenn management state [skip ci]"
git -C _deploy push origin deploy
```

上記の `commit` は **必要なファイルの `git add` が済んだ後**に実行してください。`[skip ci]` は管理状態や cleanup だけを commit / push する場合に使用し、Zenn の通常の記事・画像変更には付けません。

## 例外対応コマンド（通常は使わない）

### Pending UPDATE の手動確定

`checker check` で PENDING のまま残った UPDATE に対し、ユーザーが **Zenn Dashboard で対象 deploy の成功**と、**現時点の remote content が deploy artifact に一致すること**の両方を確認した場合にだけ利用できます。Checker 自身は内容一致を検証しません。

```bat
"%INVOKE%" checker confirm-pending-update ARTICLE_ID ^
  --confirmed-deploy-success --confirmed-content-match
```

成功終了コード `0` は `COMMIT_READY` ではありません。**必ず続けて通常の `checker check` を実行**し、結果を確認してください。

### 全 pending の破棄（復旧操作）

これは管理状態を実際に変更する操作です。対象 deploy が terminal で後から適用されないこと、全 pending の operation 別 remote 整合性、および全 pending を破棄する意思を確認した場合にだけ使用してください。Git HEAD と直前履歴・対象 artifact 等の preflight 条件も必要です。

```bat
"%INVOKE%" checker discard-all-pendings ^
  --confirmed-deploy-terminal --confirmed-remote-safe --confirmed-abandon-all
```

このコマンドは**ローカルの pending を clear するだけ**で、Git restore は実行しません。表示された `git restore ...` の計画をホストで確認し、必要な対象に対してのみ実行してください。復旧中は未完了状態を commit / push しません。restore 後に `checker check` で `COMMIT_READY` を確認してから、管理・cleanup commit に `[skip ci]` を使用します。

中断やエラーによって pending がすでに空になっていた場合、**discard を再実行せず**、Git 履歴、ledger、記事状態を調査して手動復旧してください。

## 操作別の早見表

| やりたいこと | 操作 |
| --- | --- |
| 執筆途中を表示する | `"%INVOKE%" preview` を起動し、ブラウザで更新 |
| dirty / hold の記事を検証する | `"%INVOKE%" converter lint --all` または `--article` |
| 新規公開する | slug・release 1・active・published true → authoring commit → `converter convert` → deploy commit/push → `checker check` |
| 公開済みを改稿する | 一度 hold で執筆 → preview/lint → active と適切な release → authoring commit → convert → deploy commit/push → check |
| 記事を消す | state erased・release 維持 → authoring commit → convert → deploy commit/push → Dashboard で削除を確認 → check |
| 消した記事を再公開する | 同じ ID、state active、適切な release と slug → authoring commit → convert → deploy commit/push → check。slug 変更には制限あり |
| 画像だけ更新する | authoring images を commit → convert → deploy commit/push → Dashboard の画像 deploy を確認 |
| check が PENDING | 新規 deploy を開始せず再 check。条件を満たす UPDATE のみ手動確定可能 |
| pending をあきらめて復旧する | 厳格な条件を確認 → `checker discard-all-pendings` → ホスト restore → `checker check` |

表中の `converter` / `checker` は、すべて上記の `"%INVOKE%"` を付けて `cmd.exe` から実行します。

## 制約と注意点

- 実 Zenn への deploy / US-10・画像失敗 retry 等は運用実測で最終確認する必要があります。
- 予約公開（未来の `published_at`）は Phase 0 MVP で非対応です。
- Checker の成功は image deployment 成功の証明になりません。
- `check` が `COMMIT_READY` でも、**CREATE SUCCESS の snapshot 更新後・ledger 更新前の中断で孤立 snapshot を見落とす可能性**が既知です。実運用前の安全性レビュー対象です。
- 各コマンドの詳細条件は 4 仕様文書を優先します。引数と help は `"%INVOKE%" converter --help`、`"%INVOKE%" checker --help`、または `"%INVOKE%" converter lint --help` などで確認できます。

## 外部資料

- [Zenn 公式：Zenn CLI で記事・本を管理する方法](https://zenn.dev/zenn/articles/zenn-cli-guide)
