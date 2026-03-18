# ADR-001: Retrieval 定数を argparse に入れない

> Date: 2026-03-19
> Status: Accepted

## Context

`max_context_chars`, `max_file_chars`, `max_deps_per_file`, `min_confidence` は
retrieval エンジンの容量制限パラメータ。これらをユーザーがカスタマイズできるようにしたい。

方法は2つ:
1. argparse にフラグとして追加し、他の設定と同じルートで処理する
2. config.json / profile から直接読み取り、`args._retrieval_config` に格納する

## Decision

**方法 2** を採用。retrieval 定数は argparse に入れず、config.json と profile から
直接 dict として読み取り、`build_context(retrieval_config=...)` に渡す。

## Consequences

**良い点**:
- `--max-context-chars` 等の CLI フラグが増えない（ユーザー向け namespace が汚れない）
- retrieval 定数はエンジン内部パラメータであり、CLI で毎回指定するものではない
- config.json や profile で設定するのが適切な粒度

**悪い点**:
- `_apply_config_to_parser()` の統一パスを通らない。cli.py のメイン解析部で個別処理が必要
- `args._retrieval_config` はプライベート属性で、IDE の補完が効かない

**config vs policy の境界**:
- retrieval 定数は profile の `config` セクションに置く（`policy` ではない）
- 理由: これらは「エンジンがどれだけのリソースを使うか」であり、「どう検出するか」ではない
