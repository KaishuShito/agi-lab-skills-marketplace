# ADR-006: ドキュメントを契約面として扱う（docs adapter）

> Date: 2026-03-19
> Status: Accepted

## Context

delta-lint の核心は「2つの契約面の矛盾検出」。しかし入力がソースコードファイルに限定されており、
README や ADR に書かれた仕様とコードの乖離は検出できなかった。

「ドキュメントに認証必須と書いてあるのに、ハンドラにガードがない」のような矛盾は
コード × コードの矛盾と同じメカニズムで発生する（スコープ外の更新漏れ）。

## Decision

ドキュメントファイル（.md, .adoc, .txt）を「仕様の契約面（specification contract surface）」
として扱い、既存の検出パイプラインに投入する。

### 方式

- **retrieval.py**: `build_context(doc_files=[...])` で ModuleContext.doc_files に格納
- **detector.py**: user prompt にドキュメントが含まれることを明示するヘッダーを追加
- **detect.md**: 「Code × Document Contradictions」セクションで検出指針を定義
- **cli.py**: `--docs` フラグ（明示パス or 引数なしで auto-discover）

### auto-discover 対象

`--docs` を引数なしで指定した場合:
- ルート: README.md, ARCHITECTURE.md, CONTRIBUTING.md, DEVELOPMENT.md, DESIGN.md, API.md
- docs/**/*.md（ADR, 仕様書, ガイド等を再帰探索）

### プロファイルからの設定

```yaml
policy:
  docs: true                    # auto-discover
  docs: ["README.md", "docs/"]  # 明示パス
```

## Consequences

**良い点**:
- 新しい adapter や抽象レイヤーを追加せず、既存パイプラインを拡張するだけで実現
- LLM は自然言語のドキュメントを「仕様」として読める — 静的ツールにはできない
- 既存のパターン（①-⑥）をそのまま再利用（README の記述を file_a、コードを file_b として報告）

**悪い点**:
- ドキュメントが大きいとコンテキスト予算を圧迫する（MAX_CONTEXT_CHARS で制限済み）
- 「将来の計画」「aspirational な記述」と「現在の仕様」の区別が LLM 依存
  → detect.md で「具体的でテスト可能な主張のみ対象」と明記して軽減

**将来の拡張**:
この設計は「コードファイル以外のものを契約面として渡す」汎用パターンの最初の実装。
同じ方式で DB schema、API spec、env config 等を追加できる（adapter パターン）。
