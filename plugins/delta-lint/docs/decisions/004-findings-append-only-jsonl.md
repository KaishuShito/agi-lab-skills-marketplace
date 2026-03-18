# ADR-004: Findings は append-only JSONL

> Date: 2026-03-19
> Status: Accepted

## Context

LLM は非決定的。同じコードを2回スキャンしても、findings の表現（タイトル、説明）が
微妙に変わる。これを素朴に保存すると:
- 重複 finding が増殖する
- 過去の finding が消える（上書き方式の場合）
- チーム間でビューが一致しない

## Decision

**append-only JSONL** を採用する。

```
.delta-lint/findings/{repo-name}.jsonl
```

- 1行 = 1 JSON オブジェクト（1 finding または 1 status 更新）
- 同一 `id` の行が複数あれば、最新行が現在状態（イベントログ方式）
- finding の `id` は `SHA256(repo:file_a:file_b:pattern)` で構造ベース（LLM の表現に依存しない）

## Consequences

**良い点**:
- 一度検出された finding は消えない（ストック型蓄積）
- `status: merged` にすれば debt_score は 0 になるが、履歴は残る
- 誰がいつスキャンしても、蓄積結果は同じビューで見える
- git で差分管理しやすい（append のみなので conflict しにくい）

**悪い点**:
- ファイルサイズが単調増加する（数千件規模までは問題ない）
- 最新状態を得るには全行を読んで同一 id をグループ化する必要がある
- JSONL は直接ブラウザで見にくい（→ ダッシュボード HTML で解決）

**ID 生成の設計**:
- 構造ベース ID: `SHA256(repo:sorted(file_a, file_b):pattern)` → LLM の表現揺れに強い
- title ベース ID: `SHA256(repo:file:title)` → 手動追加時の fallback（file_b がない場合）
- 同じ2ファイル間の同じパターンは同一 finding として扱う
