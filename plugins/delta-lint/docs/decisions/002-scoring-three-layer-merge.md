# ADR-002: スコアリングは 3層マージ

> Date: 2026-03-19
> Status: Accepted

## Context

チームごとにスコアリングの重みをカスタマイズしたい。例えば:
- セキュリティチーム: ④ Guard Non-Propagation の重みを上げたい
- レガシーチーム: ⑦ Dead Code の重みを下げたい（意図的に残している）

しかし全キーを毎回指定させるのは非現実的。

## Decision

3層マージ方式を採用:
```
defaults (scoring.py のハードコード)  ←  config.json  ←  profile policy
```

- 各層は「指定したキーだけ」上書きする（deep merge）
- 未指定キーはデフォルト値がそのまま残る
- `load_scoring_config(repo_path)` が config.json を読み、呼び出し側で profile を重ねる

## Consequences

**良い点**:
- チームは変えたいキーだけ書けばいい（最小限の設定）
- profile を切り替えるだけでスコアリングが変わる
- デフォルト値は scoring.py に集約（single source of truth）

**悪い点**:
- マージ順序を理解しないと「なぜこの重みになったか」がわかりにくい
- デバッグ時は `--verbose` で最終マージ結果を出力する必要がある

**スケール設計の理由**:
- `DEBT_SCALE=1000`, `ROI_SCALE=100`, `INFO_SCALE=100`
- 大きい数字のほうが直感的（「負債 600」「解消価値 3500」）
- debt_score は 0〜1000、roi_score/info_score は 0〜数千のレンジ
