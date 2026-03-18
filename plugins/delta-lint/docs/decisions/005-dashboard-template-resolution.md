# ADR-005: ダッシュボードテンプレートの 3段解決

> Date: 2026-03-19
> Status: Accepted

## Context

findings ダッシュボードの HTML テンプレートをチームがカスタマイズしたい場合がある
（ブランディング、追加カラム、レイアウト変更等）。
しかし大半のユーザーは組み込みテンプレートで十分。

## Decision

3段階のテンプレート解決を採用:

```
1. policy.dashboard_template（profile で明示指定）
2. .delta-lint/templates/findings_dashboard.html（リポローカル）
3. scripts/templates/findings_dashboard.html（組み込み）
```

上から順に探索し、最初に見つかったものを使う。

## Consequences

**良い点**:
- デフォルトはゼロ設定で動く（組み込みテンプレート）
- リポにテンプレートを置くだけでカスタマイズできる（設定不要）
- profile で完全制御も可能（チーム別テンプレート）
- 明示パスが存在しない場合は組み込みにフォールバック（エラーにならない）

**悪い点**:
- 3段あるので「どのテンプレートが使われたか」がわかりにくい
  → `--verbose` で解決パスをログ出力する
- リポローカルテンプレートが意図せず優先される可能性
  → `.delta-lint/templates/` は明示的に作る必要があるので、事故は起きにくい

**policy に置く理由**:
- テンプレートは「出力をどう表示するか」の制御であり、エンジンパラメータ（config）ではない
- `prompt_append`, `disabled_patterns` と同じ「ランタイム挙動制御」に分類される
