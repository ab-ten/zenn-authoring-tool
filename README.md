# Zenn authoring tool

Zenn の記事を **authoring** と **deploy** に分離して管理する、ローカル執筆・プレビュー・公開支援ツールです。

- **Previewer** — 執筆中の記事を Zenn CLI で表示します。dirty / untracked / `hold` 記事もプレビューできます。
- **Publisher lint** — 現在の authoring Markdown を read-only で検証します。公開可能性の判定ではありません。
- **Publisher Converter** — コミット済みの公開入力から deploy 記事・画像と保留中の管理状態を生成します。
- **State Checker** — Zenn 上の記事の反映状況を確認し、成功した公開の管理状態を確定します。

**ステータス：Phase 0 MVP の主要機能を実装済み。** 記事を実際に公開する E2E・運用検証は継続中です。正常に lint / preview できても、公開できる保証はありません。

## 最初に読むもの

- **[コマンドライン運用ガイド](docs/cli-guide.md)** — Windows PowerShell / Docker Desktop での起動、記事公開、復旧、確認コマンド。
- [Lint の仕様と制約](publisher/LINT.md)
- [Previewer の使用方法と内部動作](previewer/README.md)
- [User Stories](docs/use-cases.md) / [Architecture](docs/architecture.md) / [Domain](docs/domain.md) / [Decisions](docs/decisions.md)
- [開発・テスト規約](AGENTS.md)

## Repository の分離

この **ツール開発 repository** と、実際の **Zenn 記事 repository** は別の Git repository です。ツール開発 repository を記事入力に使わないでください。

記事 repository の代表的な構成：

```text
my-zenn-articles/                 # authoring worktree: main または master
  articles/
    example.md                    # ファイル名と公開 slug は別
    notes/another.md
  images/                         # プレビュー・deploy mirror の画像元
  _deploy/                        # deploy branch の linked worktree
    articles/<slug>.md            # Converter が生成
    images/                       # Converter が同期
    .zenn-authoring/
      config.json                 # Checker init が作成
      ledger.json                 # Checker init が作成
      released/<article-id>.md    # 成功済み release の snapshot
```

`_deploy/` は authoring 側の `.gitignore` に登録します。Git worktree は相対リンクを使用し、authoring は `main` / `master`、deploy は `deploy` branch とします。記事 repository の作成・初期化手順は [運用ガイド](docs/cli-guide.md#初回セットアップ) を参照してください。

## 前提環境

- Windows 11 Pro + Docker Desktop（Linux containers）を主な使用例とします。
- Host Git 2.48 以降（worktree の相対リンクが必要）。
- Publisher: Python 3.13 の Docker image（内部 Git 2.51.1）。
- Previewer: Node.js 24.21.0 + Zenn CLI 0.5.6 の独立した Docker image。
- Zenn と接続済みの GitHub repository。**Git commit / push / restore はホストで明示的に行います。**

アプリケーション依存パッケージをホストや Codex sandbox にインストールせず、Docker image 内で実行します。`AGENTS.md` を参照してください。

## ビルドとテスト

開発 repository のルートで：

```sh
make build-publisher
make build-previewer
make test
```

`make build` は `make build-publisher` の互換エイリアスです。`make test` は Publisher の全テストを実行します。Previewer の Node テストは別コマンドです：

```sh
docker run --rm --entrypoint node zenn-authoring-previewer --test /opt/previewer/tests/generate-links.test.mjs
```

### 実行時のマウント方針

日常操作には、開発 repository 直下の [`invoke.cmd`](invoke.cmd) を使用します。**Windows PowerShell / cmd.exe で、記事 repository のルートをカレントディレクトリにして起動してください。** Wrapper が明示的な `--article-root /article-repo` を Python CLI に渡します。既存 Python CLI 自体は CWD fallback を行いません。

- Preview / Lint: authoring repository は read-only mount。
- Convert / Checker: authoring repository 全体を read-only mount し、`_deploy` だけ別途 read-write mount。
- Git 操作: ホストで確認・実行。Docker に Git 認証情報を渡す必要はありません。
- Publisher image は実行時に開発 repository の `publisher/` を `/src` に read-only mount します。

直接 `docker run ... python -m ...` を使用する場合の引数とマウント指定は [運用ガイド](docs/cli-guide.md#共通パス変数と-docker-mount) を参照してください。

## Quick Start（日常作業）

最初に **記事 repository** へ移動し、`invoke.cmd` のパスを指定します。

```powershell
Set-Location 'C:\work\my-zenn-articles'
$Invoke = 'C:\work\zenn-authoring-tool\invoke.cmd'
```

### 1. Preview

```powershell
& $Invoke preview
& $Invoke preview --listen 127.0.0.1:8001
```

既定のアクセス先は <http://localhost:8000> です。`--listen IPv4_ADDRESS:HOST_PORT` を指定すると Docker のホスト側公開アドレスを変更できます（例の2つのコマンドは同時ではなく選択して実行）。上の例では <http://localhost:8001> にアクセスします。`0.0.0.0:8000` は LAN 等にも公開する可能性があるため、必要な場合だけ明示指定してください。コンテナ内部のポートは常に8000です。

編集後はブラウザ更新、記事の追加・改名や slug の変更後は Preview コンテナを再起動してください。

### 2. Lint（コミット前も可）

```powershell
& $Invoke converter lint --all
& $Invoke converter lint --article articles/example.md
```

`lint` は Git cleanliness / ledger / remote / publish readiness を判定しません。`--article` の場合は指定記事だけを検証し、他記事との UUID 重複も調べません。

### 3. 公開準備

新規公開なら `#state:active`、`#slug`、`#release:1` と Front Matter `published: true` を設定します。更新時は成功済み release と candidate article content の関係に従って release を決めます。

公開に参加する active / erased 記事と images は、ホスト Git でコミット済み・clean にします。`hold` は dirty で構いません。

### 4. Converter（deploy を変更）

```powershell
& $Invoke converter convert
```

成功後、`_deploy` の Git status / diff をホストで確認し、意図した変更だけ commit / push します。**Converter の成功は Zenn 公開完了ではありません。**

### 5. State Checker（Zenn 反映後）

```powershell
& $Invoke checker check
```

`COMMIT_READY` の場合のみ、管理・cleanup 差分を確認して **`[skip ci]` 付き commit** と push を行います。`NOT_READY` なら管理・cleanup の commit / push はしません。

画像のみの変更は Zenn Dashboard でデプロイ結果を確認します。Checker の記事判定では画像公開成功を保証しません。

初回の `checker init ZENN_USERNAME`、手動 UPDATE 確定、pending 破棄、詳細な Git 復旧操作は [運用ガイド](docs/cli-guide.md) にまとめています。

## 運用上の重要な制約

- Converter / Checker は Zenn への push、Git commit、Git restore を自動実行しません。
- `lint` / `preview` の成功は、公開可能・公開成功・`COMMIT_READY` の保証ではありません。
- `check` は一回限りの確認です。`PENDING` の場合は新しい deploy を発生させず、必要に応じて改めて実行します。
- UPDATE 手動確定と pending の一括破棄には、Zenn Dashboard や remote の状態を**ユーザー自身が確認**する必要があります。
- 予約公開は Phase 0 MVP の対象外です。画像だけのデプロイ失敗後、差分が残っていない場合の再試行も未確定です。
- **既知の安全性課題：** CREATE SUCCESS 適用中に snapshot 更新後・ledger 更新前で中断した場合、孤立 snapshot を通常の `COMMIT_READY` 判定が検出できない可能性があります。実運用前の安全性レビュー対象です。

### 検証状況

Publisher: 直近の報告で **941 tests PASS**（手元で再実行済みと報告）。Previewer: 直近の報告で **15 tests PASS**、Windows + Docker Desktop で記事プレビューを表示確認済み。実 Zenn deployment・image-only workflow・US-10 等の運用検証は今後の作業です。

本 README は利用ガイドです。状態遷移・安全性・例外条件の仕様については、凍結済みの `docs/domain.md`・`docs/architecture.md`・`docs/decisions.md`・`docs/use-cases.md` を正本として参照してください。
