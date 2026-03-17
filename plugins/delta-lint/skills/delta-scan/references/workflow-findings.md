# Workflow 4: Findings (`/delta-lint findings`)

**JSONL ベースのバグ/矛盾記録システム。** 複数LLMがリポジトリ横断で発見を追記・管理できる。

**Trigger**: "findings", "バグ記録", "発見を記録", "finding を追加" 等。scan 後に自動提案してもよい。

## Storage

```
.delta-lint/findings/
├── _index.md              # 全リポの概要（自動生成）
├── Codium-ai__pr-agent.jsonl
├── paul-gauthier__aider.jsonl
└── ...
```

各 `.jsonl` は1行1JSON（追記専用）。同じ `id` で複数行 = イベントログ（最新行が正）。

## Subcommands

### findings add — 発見を記録

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py findings add \
  --repo "{base_path}" \
  --repo-name "owner/repo" \
  --file "src/handler.ts" \
  --line 42 \
  --type bug \
  --finding-severity high \
  --pattern "④ Guard Non-Propagation" \
  --title "TTL refresh guard condition inverted" \
  --description "is_alive() returns True for expired entries" \
  --status verified \
  --url "https://github.com/owner/repo/pull/123" \
  --found-by "claude-opus" \
  --verified
```

| Field | Required | Description |
|-------|----------|-------------|
| `--repo` | No | Base path（`.delta-lint/findings/` の親） |
| `--repo-name` | Yes | リポジトリ名（`owner/repo` 形式推奨） |
| `--file` | Yes | 発見箇所のファイルパス |
| `--line` | No | 行番号 |
| `--type` | No | `bug` / `contradiction` / `suspicious` / `enhancement` |
| `--finding-severity` | No | `high` / `medium` / `low` |
| `--pattern` | No | 矛盾パターン（①〜⑥） |
| `--title` | Yes | 短いタイトル |
| `--description` | No | 詳細説明 |
| `--status` | No | `found` / `verified` / `submitted` / `merged` / `rejected` / `wontfix` / `duplicate` |
| `--url` | No | GitHub Issue/PR URL |
| `--found-by` | No | 発見者（`claude-opus` 等） |
| `--verified` | No | コード確認済みフラグ |

### findings list — 一覧表示

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py findings list --repo "{base_path}" [--repo-name "owner/repo"] [--status submitted] [--type bug] [--format json]
```

### findings update — ステータス更新

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py findings update --repo "{base_path}" --repo-name "owner/repo" {finding_id} {new_status} [--url "https://..."]
```

例: PR 提出後に `submitted` に更新、マージ後に `merged` に更新。

### findings search — キーワード検索

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py findings search --repo "{base_path}" "TTL"
```

### findings stats — 統計

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py findings stats --repo "{base_path}" [--repo-name "owner/repo"] [--format json]
```

### findings index — _index.md 再生成

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py findings index --repo "{base_path}"
```

## LLM ワークフロー統合

**scan 完了後**: findings が検出された場合、確認を求めず自動で `findings add` を全件実行する（[workflow-scan.md](workflow-scan.md) Step 6 参照）。
**Issue/PR 提出後**: `findings update {id} submitted --url {url}` でステータス更新。
**マージ確認後**: `findings update {id} merged` でステータス更新。

**重複防止**: `add` 前に `findings list --repo-name {repo}` で既存 ID を確認する。同じ `id` + 同じ `status` の追記はエラーになる。
