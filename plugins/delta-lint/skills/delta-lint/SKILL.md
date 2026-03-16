---
name: delta-lint
description: >
  Detect structural contradictions in codebases using LLM analysis.
  Use when user says "delta-lint", "delta scan", "delta init", "delta plan",
  "structural contradiction", "構造矛盾", "デグレチェック",
  "suppress finding", "suppress check", "地雷マップ", "landmine map",
  "影響範囲", "事前チェック", "impact analysis",
  or asks to check code consistency across files.
  Also triggers when user proposes any code change (new feature, bug fix,
  refactoring, performance improvement, etc.) and delta-lint has been
  initialized (`.delta-lint/` exists).
  Supports init, plan, scan, suppress add/list/check workflows.
---

# delta-lint: Structural Contradiction Detector

delta-lint detects structural contradictions between source code modules — places where one module's assumptions contradict another module's behavior. It is NOT a linter for style or bugs, but a design-level conflict detector.

## Prerequisites

- Python 3.11+
- `anthropic` package (`pip install anthropic`)
- `ANTHROPIC_API_KEY` environment variable set (or in `技術的負債定量化PJT/.env`)
- Optional: `pyyaml` for suppress.yml (falls back to JSON format)

## Script Location

All scripts are in: `scripts/` (relative to this skill folder).
The absolute path is: `~/.claude/skills/delta-lint/scripts/`.
The prompt template is at: `scripts/prompts/detect.md`.

## Critical: Exit Code Interpretation

**exit code 1 from `cli.py scan` means high-severity findings were detected — this is NOT an error.**
Only treat it as an error if stderr contains a Python traceback or "Error:" prefix.

---

## Workflow 0: Init (`delta init`)

Initialize delta-lint for a repository. Creates a landmine map (risk heatmap) and enables automatic risk awareness.

**Trigger**: User says "delta init", "地雷マップ作って", "initialize delta-lint", or similar.

**CRITICAL: This workflow is FULLY AUTONOMOUS. Do NOT ask the user for confirmation at any step (except if already initialized). Execute Steps 1→2→3 immediately in sequence without pausing.**

### Step 1: Check if already initialized

```bash
ls {repo_path}/.delta-lint/stress-test/results.json 2>/dev/null
```

- If exists: Tell user "このリポは初期化済みです。再実行しますか？" and wait for confirmation.
- If not: **Immediately proceed to Step 2. Do NOT ask "実行しますか？" — the user already said "delta init", that IS the instruction.**

### Step 2: Run stress-test (background) — EXECUTE IMMEDIATELY

**You MUST execute this Bash command right now. Do not explain what it does, do not ask for confirmation, just run it:**

```bash
cd ~/.claude/skills/delta-lint/scripts && python stress_test.py --repo "{repo_path}" --parallel 10 --verbose --visualize 2>&1
```

Use `run_in_background: true` and `timeout: 600000`.

The script is fully autonomous — it auto-determines modification count, saves checkpoints every 10 scans, focuses on discovered hotspots, and auto-converges.

After launching, tell the user exactly this (adapt the numbers from the verbose output):

```
🔍 delta-lint 初期化を開始しました

📊 リポジトリ: {repo_name}（{n_source_files} ソースファイル）
⚙️ 仮想改修: {n} 件（自動決定）、10並列スキャン
💰 コスト: $0（サブスクCLI）

バックグラウンドでスキャン中です。
- 10件ごとにマップが更新されます
- 完了したら結果を報告します
- 推定所要時間: 約{estimated_minutes}分

この間、通常の作業を続けて大丈夫です。
なにか確認したいことはありますか？
```

Estimate time: n_modifications / 10 * 2 minutes (rough).

### Step 2.5: Add .gitignore for .delta-lint/ — EXECUTE IMMEDIATELY

**Do not wait for Step 2 to complete.** `.delta-lint/` をデフォルトで git 管理対象外にする。

プロジェクトの `.gitignore` に以下を追加する（既に存在する場合はスキップ）：

```gitignore
# delta-lint (uncomment below to share landmine map with team)
.delta-lint/
# To share with team, replace above with:
# .delta-lint/stress-test/structure.json
# .delta-lint/stress-test/modifications.json
# .delta-lint/stress-test/landmine_map.html
# .delta-lint/delta_lint_*.json
```

### Step 3: Add guard rules to CLAUDE.md — EXECUTE IMMEDIATELY

**Do not wait for Step 2 to complete.** While the stress-test runs in background, add the following to `{repo_path}/CLAUDE.md` (create if not exists, append if exists):

```markdown

## delta-lint guard
- .delta-lint/stress-test/results.json に地雷マップデータがある
- ファイルを編集する前に、そのファイルの risk_score を確認すること
  - 確認方法: `python -c "import json; d=json.load(open('.delta-lint/stress-test/results.json')); risks={r['modification']['file']: len(r['findings']) for r in d['results'] if r['findings']}; print(risks.get('TARGET_FILE', 'not in map'))"` (TARGET_FILE を実際のパスに置換)
- risk_score > 0.35 のファイルを編集する場合、ユーザーに警告すること
- コード改修（新機能・バグ修正・リファクタリング・性能改善等）の計画時:
  - 影響しそうなファイル群の risk_score を一覧表示すること
  - 高リスクファイル（risk_score > 0）が2つ以上関わる場合、事前に `delta scan --files <対象ファイル>` を提案すること
- コード改修後は delta-lint scan の実行を提案すること
```

### Step 4: When stress-test completes

When the background task notification arrives:
1. Read the output file to get the summary
2. Open the heatmap: `open {repo_path}/.delta-lint/stress-test/landmine_map.html`
3. Report to user exactly this format (fill in actual data):

```
✅ delta-lint 初期化完了

📊 結果サマリー:
- スキャン: {hit_mods}/{total_mods} 件の仮想改修で矛盾を検出（ヒット率 {hit_rate}%）
- 発見: {total_findings} 件の構造矛盾
- 対象: {n_files_at_risk} ファイルにリスクあり

🔴 高リスクファイル TOP 3:
1. {file1} — risk {score1}（{hits1}回被弾）
2. {file2} — risk {score2}（{hits2}回被弾）
3. {file3} — risk {score3}（{hits3}回被弾）

🗺️ ヒートマップをブラウザで開きました。
以降、高リスクファイルの編集時に自動で警告します。
```

To get top 3 files, run:
```bash
cd {repo_path} && python -c "
import json
d=json.load(open('.delta-lint/stress-test/results.json'))
from collections import Counter
hits=Counter()
for r in d['results']:
    if r.get('findings'):
        f=r['modification'].get('file','')
        if f: hits[f]+=1
        for af in r['modification'].get('affected_files',[]):
            hits[af]+=1
for f,c in hits.most_common(3):
    print(f'  {f}: {c} hits')
"
```

### If stress-test fails

1. Read stderr to diagnose
2. Common fixes:
   - `claude -p failed` → suggest `--backend api`
   - Timeout → suggest `--n 30`
   - Not a git repo → tell user
3. **Auto-retry once** before reporting to user

---

## Workflow 0.5: Plan (`delta plan`)

**コード改修の前に、影響範囲と潜在リスクを事前分析する。**

**Trigger**: User proposes any code change — new feature, bug fix, refactoring, performance improvement, dependency update, etc. Also triggered by "delta plan", "影響範囲チェック", "事前チェック", "impact analysis". **Auto-trigger**: ユーザーがコード改修を指示した時（「〇〇を追加したい」「〇〇を直したい」「〇〇をリファクタしたい」「〇〇の性能改善したい」等）、`.delta-lint/` が存在すれば自動的にこのワークフローを実行する。

### Step 1: 影響ファイルの特定

ユーザーの要望を分析し、影響しそうなファイルをリストアップする。以下の情報を使う：

1. **構造分析**: `.delta-lint/stress-test/structure.json` からモジュール間依存関係を読む
2. **コード検索**: 関連するキーワード・関数・型をコードベースで検索
3. **地雷マップ**: `.delta-lint/stress-test/results.json` から各ファイルの risk_score を取得

```bash
python3 -c "
import json
d=json.load(open('{repo_path}/.delta-lint/stress-test/results.json'))
from collections import Counter
hits=Counter()
for r in d['results']:
    if r.get('findings'):
        f=r['modification'].get('file','')
        if f: hits[f]+=len(r['findings'])
for f in {affected_files_list}:
    score=hits.get(f, 0)
    mark='🔴' if score>=3 else '🟡' if score>=1 else '⚪'
    print(f'  {mark} {f}: {score} findings')
"
```

### Step 2: 暗黙の契約の洗い出し

影響ファイル群について、以下を分析してユーザーに提示する：

1. **ファイル間の暗黙の前提**: 共通の型・定数・規約に依存している箇所
2. **既存パターンとの整合性**: 同種の既存実装（例: 既存ミドルウェア）と揃えるべき点
3. **6つの矛盾パターンのリスク**: 各パターンが発生しうるポイントを具体的に指摘

出力フォーマット:

```
📋 影響範囲分析: {feature_name}

📁 影響ファイル:
  🔴 mux.go (risk: 9) — ルーティング登録の変更が必要
  🟡 middleware/throttle.go (risk: 2) — 類似機能との整合性
  ⚪ middleware/realip.go (risk: 0) — IP取得ロジックへの依存

⚠️ 潜在リスク:
  1. [Guard Non-Propagation] realip.go のIP取得ロジックと新機能のIP取得が不一致になる可能性
  2. [Lifecycle Ordering] ミドルウェアの実行順序に依存する暗黙の前提がある
  3. [Asymmetric Defaults] throttle.go と HTTPレスポンス形式が異なる可能性

💡 推奨事項:
  - 実装前に realip.go のIP取得関数を共通化することを検討
  - throttle.go のレスポンス形式に揃えること
  - 実装後に `delta scan --files <affected>` で矛盾チェック推奨
```

### Step 3: 事前スキャン（自動実行）

高リスクファイル（risk > 0）が2つ以上含まれる場合、**確認せずに自動で** `delta scan --files` を実行する：

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --files {affected_files} --verbose --severity medium 2>&1
```

### Step 4: 設計レビュー（サブエージェント） + 中間報告

Step 2-3 の分析結果をもとに、**サブエージェントをバックグラウンドで起動して設計レビュー**を行う（`run_in_background: true`）。確認を求めず自動実行する。

**サブエージェントの実行中、メインはユーザーに中間報告を行う。** 以下のフォーマットで報告し、ユーザーの関心事を引き出す：

```
🔄 設計レビューをバックグラウンドで実行中です。

ここまでの分析で気になったポイント:
  1. {Step 2-3 で発見した最も重要なリスク}
  2. {2番目に重要なリスク}
  3. {既存動作で変わりそうなこと}

レビュー中に確認していること:
  - 各 finding が本物かどうかの検証
  - 既存ミドルウェアのスタイル・パターンとの整合性
  - 必要なテストケースの洗い出し

何か気になる点や、特に確認してほしいことはありますか？
```

ユーザーからの追加の懸念点や要望があれば、サブエージェントの結果と統合して Step 5 の提案に反映する。

サブエージェントには以下のプロンプトを渡す：

```
あなたは設計レビュアーです。以下の改修計画について、3つの観点でレビューしてください。

## 改修内容
{ユーザーの要望の要約}

## 影響範囲分析の結果
{Step 2 の出力}

## delta scan の findings
{Step 3 の出力（あれば）}

## レビュー観点

### 1. Findings 検証
各 finding について：
- 該当コードを実際に読み、矛盾が実在するか検証する
- 既存テストがこのケースをカバーしているか確認する
- confidence を判定: ✅ confirmed / ⚠️ likely / ❌ false positive
- false positive の場合はその理由を明記

### 2. 既存コードとの整合性チェック
**大前提: 既存コードのスタイル・パターンは意図的な設計判断として尊重する。**
新しいコードが既存のコードベースに「馴染む」設計になっているかを検証する：

- **スタイルの踏襲**: 既存コードのエラーハンドリング、命名規約、ファイル構成、コメントスタイルを分析し、新コードがそれに揃っているか
- **暗黙の設計ルールの発見**: 既存の類似モジュール（例: 他のミドルウェア）を3つ以上読み、共通パターンを抽出する。そのパターンに従っているか
- **意図的な重複の尊重**: 既存コードに似たロジックの重複がある場合、それは意図的な選択（疎結合のため等）である可能性がある。安易に共通化を提案しない
- **新たな負債の持ち込み防止**: 既存にないレイヤー（新しい抽象化、共通ユーティリティ等）を不必要に追加していないか。既存の仕組みで実現できるならそれを使う

### 3. テスト要件
この改修で追加すべきテストケースを具体的にリストアップする：
- 正常系: 基本的な動作確認
- 異常系: エラーケース、エッジケース
- 結合テスト: 他のミドルウェア/モジュールとの組み合わせ
- 回帰テスト: findings で指摘された矛盾パターンが発生しないことの確認

## 出力フォーマット

📋 設計レビュー結果

🔍 Findings 検証:
  1. Finding X: ✅/⚠️/❌ — {理由}
  2. ...

🏗️ 既存コードとの整合性:
  - 踏襲すべきパターン: {既存の類似モジュールから抽出した共通パターン}
  - 注意点: {既存スタイルと乖離しそうな箇所があれば具体的に}

🧪 必須テストケース:
  1. {テスト名} — {何を確認するか}
  2. ...

⚠️ 実装時の注意事項:
  1. {findings + 負債チェックから導出された具体的な注意点}
  2. ...
```

サブエージェントは `subagent_type: "general-purpose"` で起動し、結果を待つ。

### Step 5: 実装提案をユーザーに提示する

Step 1-4 の全分析結果を統合し、**実装提案**としてユーザーに提示する。以下のフォーマットで報告する：

```
📋 実装提案: {feature_name}

🔍 現状:
  - {既存コードの関連部分の要約}
  - {地雷マップ・scan から判明したリスク}

⚡ 既存動作から変わること:
  - {この改修によって変わる既存の振る舞いを具体的に列挙}
  - 例: 「OPTIONS リクエストが 405 → 200 + CORSヘッダーに変わる」
  - 例: 「エラーレスポンスのフォーマットが JSON に統一される」
  - 変更なし（純粋な追加）の場合は「既存動作への影響なし」と明記

📐 提案する設計:
  - {具体的な実装方針}
  - {既存パターンに揃えるポイント}
  - {findings を踏まえた回避策}

❓ 確認したいこと:
  - {ユーザーに判断を仰ぎたい設計上の選択肢}
  - 例: 「既存の route_headers.go の CORS パターンと併用可能にするか、置き換えるか？」
  - 例: 「デフォルトで全オリジン許可にするか、明示的な設定を必須にするか？」
  - 判断不要な場合は省略

🚫 スコープ外（今回やらないこと）:
  - {意図的に対象外とするもの＋理由}

🧪 追加するテスト:
  - {テストケースのリスト}

確認事項に問題なければ実装に進みます。
```

**ユーザーの承認を待ってから実装に着手する。** 調査・分析・レビューは自律的に行うが、実装の最終判断はユーザーに委ねる。

### Step 6: 実装 → 事後検証

ユーザーの承認後：
- 提案に沿ってコードを実装する
- レビューで挙がったテストケースも一緒に実装する
- 実装完了後に `delta scan` で新たな矛盾がないことを確認する

---

## Workflow 1: Scan (`/delta-lint` or `/delta-lint scan`)

### Step 1: Determine scope and dry-run

Determine the target repo path (default: current working directory).
Run dry-run first to show what will be sent to the LLM:

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --dry-run --verbose 2>&1
```

If no changed files are found, suggest `--files` to specify files manually.

### Step 2: Confirm with user before LLM call

Present the dry-run summary to the user:
- Number of target files and dependency files
- Total context size in characters (~4 chars/token)
- Estimated cost: context_chars / 4 * input_price_per_token

**Ask the user to confirm before proceeding.** If context exceeds 60K chars, warn that results may degrade and suggest narrowing with `--files`.

### Step 3: Run the actual scan

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --verbose --severity {severity} 2>&1
```

Set Bash timeout to 300000 (5 min) — LLM calls can be slow.

Common options:
- `--severity high` (default) / `medium` / `low`
- `--files path/a.ts path/b.ts` — specific files instead of git diff
- `--diff-target HEAD` — git ref to diff against
- `--format json` — machine-readable output
- `--model claude-sonnet-4-20250514` — detection model (default)
- `--semantic` — enable semantic search (see below)

### Step 4: Interpret exit code

| Exit code | Meaning | Action |
|-----------|---------|--------|
| 0 | No high-severity findings | Report clean result |
| 1 + no traceback | High-severity findings found | Proceed to Step 5 (this is normal) |
| 1 + traceback | Script error | Report error, check stderr |
| Other | Unexpected | Report full output |

### Step 5: Explain results to user

Parse the Markdown output and present each finding with:
1. The pattern number and name (see "6 Contradiction Patterns" below)
2. Which two files/locations are in conflict
3. A brief explanation of why this is a problem
4. Your assessment: does this look like a true positive or false positive?

For findings tagged with `[EXPIRED SUPPRESS]`:
- Explain that this was previously suppressed but the code has changed
- Recommend the user review whether the contradiction still applies

### Step 6: Offer next actions

Based on the results, suggest:
- **If findings exist**: "suppress したい finding があれば番号を教えてください（例: `/delta-lint suppress 3`）"
- **If expired suppressions exist**: "期限切れの suppress があります。再確認して re-suppress するか、対応を検討してください"
- **If no findings**: Report clean result and mention suppressed/filtered counts if any

---

## Workflow 2: Suppress Add (`/delta-lint suppress {number}`)

### Step 1: Validate finding number

The user provides a finding number (1-based, as shown in scan output).
If the user hasn't run a scan in this session, warn them and suggest scanning first.

### Step 2: Collect reason from user

Ask the user for both fields BEFORE running the command (stdin is unavailable to the script):

- **why_type**: Which category?
  - `domain` — intentional design decision (business logic requires this)
  - `technical` — known limitation (accepted for now, may fix later)
  - `preference` — style/preference choice (team agreed on this)
- **why**: Reason for suppression
  - English: minimum 20 characters
  - Japanese: minimum 10 characters
  - Must be a meaningful explanation, not just "false positive"

### Step 3: Run suppress command (non-interactive)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress {number} --repo "{repo_path}" --why "{why_text}" --why-type "{why_type}" 2>&1
```

**Shell escaping**: If `why_text` contains quotes or special characters, escape them properly or use single-quote wrapping.

### Step 4: Confirm result

- Success: show the suppress ID (8-char hex) and confirm it was written to `.delta-lint/suppress.yml`
- Duplicate: if already suppressed, inform the user and show the existing entry ID
- Validation error: show the specific error and ask user to correct

---

## Workflow 3: Suppress List (`/delta-lint suppress --list`)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress --list --repo "{repo_path}" 2>&1
```

Present each entry with: ID, pattern, files, why_type, why, date.

---

## Workflow 4: Suppress Check (`/delta-lint suppress --check`)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress --check --repo "{repo_path}" 2>&1
```

If expired entries found:
1. List each expired entry with the hash change
2. Explain: "コードが変更されたため、suppress が期限切れになりました"
3. Suggest: re-scan to see if the contradiction still exists, then re-suppress or fix

---

## Error Handling

| Error | Likely Cause | Recovery |
|-------|-------------|----------|
| `ANTHROPIC_API_KEY not set` | Environment variable missing | Ask user to set it or check `.env` file |
| `No changed source files found` | Clean git status | Suggest `--files` to specify files manually |
| `ModuleNotFoundError: anthropic` | Package not installed | `pip install anthropic` |
| `Connection error` / timeout | Network issue | Retry once, then report |
| `Context limit reached` | Too many files/deps | Narrow scope with `--files` |
| Python traceback in stderr | Bug in delta-lint | Report the traceback to the user |

---

## Argument Reference

See [suppress-design.md](references/suppress-design.md) for the full suppress mechanism design.

### Scan
| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | `.` | Repository path |
| `--files` | (git diff) | Specific files to scan |
| `--severity` | `high` | Minimum severity: high/medium/low |
| `--format` | `markdown` | Output format: markdown/json |
| `--model` | `claude-sonnet-4-20250514` | Detection model |
| `--diff-target` | `HEAD` | Git ref to diff against |
| `--dry-run` | false | Show context only |
| `--verbose` | false | Detailed progress |
| `--log-dir` | `.delta-lint/` | Log directory |
| `--semantic` | false | Enable semantic search beyond import-based 1-hop |

### Suppress
| Flag | Default | Description |
|------|---------|-------------|
| `{number}` | - | Finding number (1-based) |
| `--repo` | `.` | Repository path |
| `--list` | false | List all suppressions |
| `--check` | false | Check for expired entries |
| `--scan-log` | (latest) | Path to scan log file |
| `--why` | - | Reason for suppression (non-interactive) |
| `--why-type` | - | domain/technical/preference (non-interactive) |

---

## 6 Contradiction Patterns

When explaining findings, use these pattern descriptions:

| # | Name | Signal |
|---|------|--------|
| 1 | **Asymmetric Defaults** | Input/output paths handle the same value differently |
| 2 | **Semantic Mismatch** | Same name means different things in different modules |
| 3 | **External Spec Divergence** | Implementation contradicts the spec it claims to follow |
| 4 | **Guard Non-Propagation** | Validation present in one path, missing in a parallel path |
| 5 | **Paired-Setting Override** | Independent-looking settings secretly interfere |
| 6 | **Lifecycle Ordering** | Execution order assumption breaks under specific code paths |
