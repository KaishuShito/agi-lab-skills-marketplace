# DeltaLint

**コードを変えたら、別の場所が静かに壊れた——DeltaLint はその「壊れる場所」を指示なしで見つける。**

テストもCIもレビューも通るのに潜伏するバグ。モジュール間の暗黙の前提が食い違う「構造矛盾」を、LLMで自動検出する Claude Code plugin です。

![DeltaLint Demo](plugins/delta-lint/demo.gif)

## 実績（2026/3/13〜3/19、6日間）

| 対象リポ | Stars | 結果 |
|---------|-------|------|
| bytedance/deer-flow | 31K | PRマージ **3件** |
| promptfoo/promptfoo | 16K | PRマージ **3件** |
| facebook/lexical | 20K | PRマージ 1件 |
| microsoft/playwright | 70K | PRマージ 1件 |
| trpc/trpc | 37K | PRマージ 1件 |
| coder/code-server | 77K | PRマージ 1件 |
| D4Vinci/Scrapling | 30K | PRマージ 1件 |
| abhigyanpatwari/GitNexus | 17K | PRマージ 1件 |

**PRマージ 12件（8リポ）** / Issue起因マージ 2件（dify 133K, hono 29K） / セキュリティ脆弱性報告 3件 / リジェクト 1件（成功率 93%）

## インストール

```bash
/plugin marketplace add karesansui-u/agi-lab-skills-marketplace
/plugin install delta-lint@delta-lint-marketplace
```

Python 3.11+ と git があれば動きます。他の依存は初回実行時に自動インストールされます。

## 使い方

```
delta scan                    # 変更ファイルの構造矛盾を検出
delta scan --files src/a.ts   # 特定ファイルを指定
delta scan --autofix          # 検出 + 修正コード自動生成
```

`delta scan` と打つだけ。何を探すかも、どこを見るかも、エージェントが自分で判断します。

### 自律実行の流れ

```
delta scan（Enter 1回）
  → git diff で変更ファイル特定
  → import 追跡で 1-hop 依存を収集
  → 6パターンの構造矛盾を LLM で検出
  → 負債スコア算出
  → ダッシュボード生成
```

### 検出 → 修正 → PR（フル自律）

```
delta scan --autofix          # 検出 + 自動修正
debt-loop --ids F001,F002     # 選んだ finding を修正 → branch → PR 作成
```

## 6つの検出パターン

| # | パターン | 例 |
|---|---------|---|
| 1 | **Asymmetric Defaults** | `user_id or ""` と `not user_id` の矛盾（dify 133K で実バグ） |
| 2 | **Semantic Mismatch** | 同じ名前が別モジュールで異なる意味を持つ |
| 3 | **External Spec Divergence** | 実装が準拠すべき仕様と食い違う |
| 4 | **Guard Non-Propagation** | あるパスにはガードがあるが並行パスにない |
| 5 | **Paired-Setting Override** | 独立に見える設定が裏で干渉する |
| 6 | **Lifecycle Ordering** | 実行順序の暗黙の前提が壊れる |

## なぜ動くか

同じ LLM でも、バグを「直す」タスク（SWE-bench: 21%）と矛盾を「見つける」タスク（89%）では成功率が4倍違います。試験問題を解くより、答案の間違いを見つける方が簡単なのと同じ原理です。

## License

MIT
