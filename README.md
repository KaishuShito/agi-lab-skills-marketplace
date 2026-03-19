# DeltaLint

## リグレッションバグの検出を、エンジニアリングする

**機能改修したら、関係ない場所でバグが発生しました。**<br>
**—— DeltaLint はその「壊れる場所」を特定します。**

テストもCIもレビューも通るのにいつの間にか潜伏するバグ。そんな悩みを一網打尽。

これまでのバグ検出ツールと違った「**構造矛盾(情報損失)**」という概念を使って自動検出します。

![DeltaLint Demo](plugins/delta-lint/demo.gif)

## 実績（2026/3/13〜3/20、7日間の実績）

| 対象リポ | Stars | 結果 |
|---------|-------|------|
| getsentry/sentry | 43K | PRマージ 1件 — [#110504](https://github.com/getsentry/sentry/pull/110504) |
| coder/code-server | 77K | PRマージ 1件 — [#7709](https://github.com/coder/code-server/pull/7709) |
| microsoft/playwright | 70K | PRマージ 1件 — [#39744](https://github.com/microsoft/playwright/pull/39744) |
| trpc/trpc | 37K | PRマージ 1件 — [#7262](https://github.com/trpc/trpc/pull/7262) |
| bytedance/deer-flow | 31K | PRマージ 3件 — [#1161](https://github.com/bytedance/deer-flow/pull/1161) [#1162](https://github.com/bytedance/deer-flow/pull/1162) [#1163](https://github.com/bytedance/deer-flow/pull/1163) |
| D4Vinci/Scrapling | 30K | PRマージ 1件 — [#201](https://github.com/D4Vinci/Scrapling/pull/201) |
| facebook/lexical | 20K | PRマージ 1件 — [#8235](https://github.com/facebook/lexical/pull/8235) |
| abhigyanpatwari/GitNexus | 17K | PRマージ 1件 — [#350](https://github.com/abhigyanpatwari/GitNexus/pull/350) |
| promptfoo/promptfoo | 16K | PRマージ 3件 — [#8163](https://github.com/promptfoo/promptfoo/pull/8163) [#8165](https://github.com/promptfoo/promptfoo/pull/8165) [#8182](https://github.com/promptfoo/promptfoo/pull/8182) |

**PRマージ 13件（9リポ）** / Issue起因マージ 2件（dify 133K, hono 29K） / セキュリティ脆弱性報告 4件 / リジェクト 1件（成功率 93%）

## インストール

```bash
/plugin marketplace add karesansui-u/agi-lab-skills-marketplace
/plugin install delta-lint@delta-lint-marketplace
```

## 使い方

```
delta scan                    # 直近3ヶ月の変更ファイルを自動スキャン
delta scan --since 1year      # 直近1年分の変更をスキャン
delta scan --scope pr         # PR全体の変更をスキャン（ベースブランチから自動検出）
delta scan --scope smart      # git履歴から高リスクファイルを優先スキャン
delta scan --scope wide       # 全ファイルをバッチ分割スキャン
delta scan --autofix          # 検出 + 修正コード自動生成
delta view                    # ダッシュボードを表示
```

`delta scan` と打つだけ。何を探すかも、どこを見るかも、エージェントが自分で判断します。
「PRレビューして」と言えば自動で `--scope pr` が選択されます。

### 自律実行の流れ

```
delta scan（Enter 1回）
  → 直近3ヶ月の git history から変更ファイル特定（--since で期間変更可）
  → import 追跡で 1-hop 依存を収集
  → 6パターンの構造矛盾を LLM で検出
  → 負債スコア算出
  → ダッシュボード生成（バッチ完了ごとにリアルタイム更新）

delta scan --scope pr
  → merge-base 算出でPR全体の差分を取得
  → バッチ分割 → プログレスバー付き逐次実行
  → 各バッチ完了時にダッシュボード + findings 自動反映
```

### 検出 → 修正 → PR（フル自律）

```
delta scan --autofix          # 検出 + 自動修正
debt-loop --ids F001,F002     # 選んだ finding を修正 → branch → PR 作成
```

## 6つの検出パターン

| # | パターン | 例 | なぜ起きる |
|---|---------|---|-----------|
| ① | **非対称デフォルト** | 会員登録で名前を空欄にするとプロフィール画面に「null」と表示される | 保存と表示で「空」の扱いが違う<br>*← 入力パスと出力パスでデフォルト値が非対称* |
| ② | **意味的不一致** | 注文ステータス「0」が画面Aでは「未処理」、画面Bでは「キャンセル済」になる | 同じ名前が場所によって別の意味<br>*← 共有名の意味がモジュール間で暗黙に分岐* |
| ③ | **仕様乖離** | 「全APIに認証必須」と書いてあるが認証なしで叩けるAPIがある | ドキュメントと実装がズレている<br>*← 仕様と実装の同期が手動依存で維持されない* |
| ④ | **ガード欠落** | 新規投稿にはXSS対策があるが編集画面にはない | 片方のパスだけチェックが抜けている<br>*← 並行パスへのガード伝搬が保証されない* |
| ⑤ | **設定干渉** | セッション有効期限30分 × 自動保存間隔40分で下書きが毎回消える | 独立に見える設定が裏で矛盾する<br>*← 設定間の暗黙の制約が文書化されていない* |
| ⑥ | **順序依存** | ログイン直後だけ「あなたへのおすすめ」が他人のデータで表示される | 特定の条件で実行順が入れ替わる<br>*← 実行順序の前提がコードに明示されていない* |

共通する原因：**開発者はスコープを絞って作業する**。機能Aを修正してテストが通れば完了。しかし機能Aと暗黙の前提を共有する機能Bが壊れていないかは、誰もチェックできていない可能性がある。DeltaLint はこの「スコープ外」を狙って検出する技術。

## なぜこれがこんなに機能するのか

この技術は、数学的に証明した構造崩壊理論をベースに作っています。(私の理論、失礼)<br>
[Structural Collapse as Information Loss: The Exponential Decay Mechanism under Accumulating Constraints](https://zenodo.org/records/18943286)（[PDF](Information_loss.pdf)） <br>
※東大松尾研OB(学際発表経験有)の方に査読してもらいました<br>

特定の条件を満たす構造は、構造矛盾によって急激に崩壊するという理論です。<br>
ソフトウェア本体を構造物と捉えて、矛盾があったときにバグとして炙り出てくるという理論応用になります。

LLM 11モデル・5ベンダーでの実験と、SAT問題での数学的検証により、構造矛盾が蓄積するとシステムの健全性は**指数関数的に**崩壊する（足し算ではなく掛け算）ことを確認しています。

DeltaLint はこの理論を使って、コードの中から「設計図の食い違い」を自動検出します。だから普通のリンターやテストでは見つからないバグが見つかります。

## License

MIT
