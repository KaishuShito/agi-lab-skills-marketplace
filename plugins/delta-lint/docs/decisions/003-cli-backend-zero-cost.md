# ADR-003: CLI バックエンド（$0）をデフォルトにする

> Date: 2026-03-19
> Status: Accepted

## Context

delta-lint は LLM を呼び出すツールだが、API 呼び出しはトークン課金される。
開発者が気軽に使えるようにするには、コストゼロで動作する手段が必要。

2つのバックエンドがある:
1. `cli`: `claude -p` コマンド（Anthropic サブスクリプション内、$0）
2. `api`: Anthropic Python SDK（`ANTHROPIC_API_KEY` 必要、トークン課金）

## Decision

**`cli` をデフォルト**にする。`api` は `--backend api` で明示的に切り替える。

フォールバック挙動:
1. `claude -p` が使えるか確認（`_cli_available()`）
2. 使えなければ `ANTHROPIC_API_KEY` があるか確認
3. どちらもなければエラー

## Consequences

**良い点**:
- サブスク契約済みユーザーは $0 で何度でもスキャンできる
- CI/CD では `--backend api` + `ANTHROPIC_API_KEY` で動作
- GitHub Action は API バックエンド固定（CI 環境に claude CLI がないため）

**悪い点**:
- `claude -p` のレート制限やタイムアウトに影響される
- CLI バックエンドは API より遅い（プロセス起動オーバーヘッド）
- CLI が利用できない環境（Docker 等）では fallback か明示指定が必要

**教訓**:
過去にこの判断をせず API を直接呼んだ結果、$100 以上を無駄にした実績がある。
コスト意識はこのプロジェクトの根幹にある設計制約。
