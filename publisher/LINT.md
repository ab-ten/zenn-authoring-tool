# Authoring source の lint

Publisher の `lint` は、現在の authoring Markdown を読み取り専用で検証します。
dirty、staged、untracked、hold の記事も対象になります。Git index の内容ではなく、現在のファイルの bytes を読み込みます。

Publisher container 内では、次のように実行します。記事 repository は開発 repository と独立したものを明示してください。

```sh
python -m converter lint --article-root /article-repo --all
python -m converter lint --article-root /article-repo \
  --article articles/example.md --article articles/nested/another.md
```

`--article-root` と、`--all` または `--article` のいずれか一方が必須です。
`--all` は `articles/` 以下の `.md` を再帰的に検証します。
`--article` は repository 相対パスで指定します。同一パスの重複指定は一度だけ検証します。
対象指定時は、無関係な記事を解析せず、UUID の重複も検査しません。
絶対パス、空・`.`・`..` のパス要素、バックスラッシュ、NUL、symlink、特殊ファイル、存在しない対象、`.md` 以外の対象を拒否します。
全件列挙時は、拡張子にかかわらず `articles/` 以下の symlink と特殊ファイルを拒否します。

Front Matter（`title` は必須の非空文字列かつ70 UTF-16コード単位以下）、管理コメント、UUID、state、active の `published: true`、`published_at` の書式と実在日時を検証します。

slug と release は省略可能ですが、指定時は既存の slug 文法と正の整数表現を検証します。
observation override は Checker のローカル表現検証を使用します。
`--all` では、解析できた記事の UUID を正規化して重複を検出し、関係する全パスを報告します。
診断は相対パス、同一記事内では診断コードの順に表示します。解析失敗は `SOURCE_PARSE` と parser の説明で報告します。

終了コードは以下のとおりです。

| コード | 意味 |
| --- | --- |
| 0 | 検証完了、検証エラーなし |
| 1 | 検証完了、記事の検証エラーあり（不正な UTF-8 を含みます） |
| 2 | 引数、repository、列挙、読み込みの失敗、または予期しないエラーによる未完了 |
| 130 | 中断 |

検証完了時は範囲、記事数、診断のエラー数を表示します。エラー数は記事数とは異なります。
列挙や読み込みの失敗時は未完了として終了し、完了集計を表示しません。
Python API は `converter.lint.lint_sources()` です。結果と各診断は frozen dataclass で返します。

## 対象外と制約

lint の完了は公開準備が整ったことを意味しません。
ledger、pending state、released snapshot、cleanliness、公開操作の分類、release 遷移、既存公開日時からの遷移、remote slug availability、slug の全体一意性、override の UPDATE 許可条件は検証しません。
将来日時も実在日時であれば、このローカル検証では許容します。remote API、preflight、publish planning、apply は実行しません。

Repository identity は既存の `identify_worktrees()` を使用します。
authoring は `main` または `master`、`_deploy` は同じ repository の `deploy` linked worktree で、相対 worktree link と両 worktree の HEAD commit が必要です。
未初期化 repository、unborn deploy、他の branch には対応しません。
ledger が存在しなくても lint は実行できます。

外部 editor による同時編集に対しては、読み込んだ bytes の best-effort な観測結果を返します。
repository 全体の atomic snapshot や、対象指定時の repository 全体の整合性は保証しません。
自動修復、cleanup、filesystem locking は行いません。
