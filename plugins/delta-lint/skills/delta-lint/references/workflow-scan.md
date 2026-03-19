# Workflow 1: Scan (`/delta-scan`)

## Step -1: Auto-init (if .delta-lint/ doesn't exist)

```bash
ls {repo_path}/.delta-lint/stress-test/structure.json 2>/dev/null
```

If `.delta-lint/` does not exist or structure.json is missing:

→ **[workflow-init.md](workflow-init.md) の全フローを実行する。**

初回スキャン時にリッチな初期化体験（構造分析→ホットスポット表示→既存バグ検出→ストレステスト→ヒートマップ）を自動提供する。
ユーザーが「delta init」と明示的に言った場合も同じフローが走る。

init 完了後、Step 0 に進んでスキャンを続行する。

If `.delta-lint/` already exists, skip this step entirely.

## Step 0: Detect persona

ユーザーの指示からペルソナを判定する。明示指定がなければ `.delta-lint/config.json` のデフォルトを使う（未設定なら `engineer`）。

**判定ルール:**
- `--for pm` / 「PM向け」「非エンジニア向け」「わかりやすく」 → `pm`
- `--for qa` / 「QA向け」「テストケースにして」「テストシナリオで」 → `qa`
- `--for engineer` / 「技術的に」「エンジニア向け」「詳しく」 → `engineer`
- `set-persona {pm|qa|engineer}` → デフォルトを変更して終了（スキャンしない）

```bash
# デフォルト確認（Python ワンライナー）
cd ~/.claude/skills/delta-lint/scripts && python -c "from persona_translator import load_default_persona; print(load_default_persona('{repo_path}'))"
```

**set-persona の場合:**
```bash
cd ~/.claude/skills/delta-lint/scripts && python -c "from persona_translator import save_default_persona; save_default_persona('{persona}', '{repo_path}'); print('✓ デフォルトペルソナを {persona} に設定しました')"
```

判定したペルソナを `{persona}` 変数として以降のステップで使う。

## Step 0.4: Detect time window (--since)

If the user mentions a time period, map it to `--since`:

| Natural language | `--since` |
|-----------------|-----------|
| 「1週間」「last week」 | `1week` |
| 「2週間」 | `2weeks` |
| 「1ヶ月」「先月から」 | `1month` |
| 「3ヶ月」「四半期」(or no mention) | `3months` (default) |
| 「半年」「6ヶ月」 | `6months` |
| 「1年」「去年から」 | `1year` |
| 「2年」 | `2years` |
| 「N日」 | `Ndays` |

If no time period is mentioned, the default is `3months`.

## Step 0.5: Detect PR mode

If the user mentions PR/プルリク/レビュー (e.g. "PRレビューして", "PR scan", "review this PR", "プルリクチェック"), use `--scope pr` instead of the default diff mode.

**PR mode auto-detection:**
- User explicitly says PR-related keywords → `--scope pr`
- Current branch is not main/master AND user says "scan" without specifying scope → suggest PR mode
- `GITHUB_BASE_REF` is set (CI environment) → `--scope pr` automatically

If base branch is ambiguous, add `--base origin/{branch}`.

## Step 1: Determine scope and run

**CRITICAL: ALWAYS let cli.py handle file selection. NEVER manually pick files with `--files`.**
The CLI has built-in logic for file selection (`--since 3months` default, `--scope smart` fallback, batching, etc.). Passing `--files` manually bypasses all of this and drastically reduces scan quality.

**Normal mode (diff — default: 3 months of history):**
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --verbose 2>&1
```

**Custom period (e.g. 1 year):**
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --since 1year --verbose 2>&1
```

**PR mode:**
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --scope pr --verbose 2>&1
```

**If cli.py reports 0 files** (「直近 3months に変更されたソースファイルがありません」):
The repo has no recent commits (fork, archive, etc.). Re-run with `--scope smart`:
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --scope smart --verbose 2>&1
```
Show: `📅 直近3ヶ月の変更なし → smart mode（git履歴の高リスクファイル）でスキャンします`

## Step 2: Auto-proceed (no confirmation needed)

**Do NOT ask the user to confirm.** delta-scan uses claude -p ($0) so there is no cost concern.
The CLI command in Step 1 already runs the full scan (no separate dry-run step needed).
Set Bash timeout to 300000 (5 min) — LLM calls can be slow.

Additional options (append to Step 1 command if needed):
- `--since 6months` — time window (default: 3months for diff mode)
- `--scope pr` — scan all files changed since base branch (for PR review)
- `--scope smart` — git history priority (auto-fallback when no recent changes)
- `--scope wide` — entire codebase, batched
- `--base origin/develop` — specify base branch (default: auto-detect)
- `--severity high` (default) / `medium` / `low`
- `--format json` — machine-readable output
- `--semantic` — enable semantic search

## Step 4: Interpret exit code

| Exit code | Meaning | Action |
|-----------|---------|--------|
| 0 | No high-severity findings | Report clean result |
| 1 + no traceback | High-severity findings found | Proceed to Step 5 (this is normal) |
| 1 + traceback | Script error | Report error, check stderr |
| Other | Unexpected | Report full output |

## Step 5: Explain results to user

Parse the Markdown output and present each finding with:
1. The pattern number and name (see [patterns.md](patterns.md))
2. Which two files/locations are in conflict
3. A brief explanation of why this is a problem
4. Your assessment: does this look like a true positive or false positive?

For findings tagged with `[EXPIRED SUPPRESS]`:
- Explain that this was previously suppressed but the code has changed
- Recommend the user review whether the contradiction still applies

## Step 5.5: Auto-triage (AUTONOMOUS — do NOT ask user)

**findings が 1件以上ある場合、確認を求めず自動で全 findings をトリアージする。**
各 finding について以下の3チェックを並列で実行し、liveness ラベルを付与する。

### Check 1: Dead code（caller ゼロ）

finding の関数・メソッド・クラスについて、呼び出し元が存在するか確認する:

```bash
# 関数名/メソッド名で grep（finding の location から抽出）
cd {repo_path} && grep -rn "{function_name}" --include="*.py" --include="*.ts" --include="*.js" --include="*.go" --include="*.rs" | grep -v "def {function_name}\|function {function_name}\|fn {function_name}" | head -5
```

- caller が 0件 → `🪦 DEAD` — 呼び出し元なし、修正しても影響ゼロ
- caller がコメントアウトのみ → `🪦 DEAD` — 実質デッドコード
- caller あり → Check 2 へ

### Check 2: Already fixed（他ブランチで修正済み）

主要ブランチ（develop, dev, next, staging 等）で同じコードを確認:

```bash
# 主要ブランチの存在確認
cd {repo_path} && git branch -r | grep -E "origin/(develop|dev|next|staging)" | head -5
```

存在するブランチがあれば:
```bash
# 該当行が修正済みか差分確認
cd {repo_path} && git diff main..origin/{branch} -- {file_path} | head -30
```

- 修正済み → `✅ FIXED in {branch}` — PR/Issue にする価値なし（自リポなら cherry-pick 検討）
- 未修正 → Check 3 へ

### Check 3: Reachability（実際に到達可能か）

finding の条件が現在の設定/コードで実際に発火するか確認:

- **デフォルト値で発火**: 追加設定なしで再現 → `🔴 LIVE`
- **特定の設定/入力で発火**: 条件は限定的だが到達可能 → `🟡 DORMANT`（条件を明記）
- **現設定では到達不能**: 将来の変更で発火する可能性のみ → `🟡 DORMANT`（リスクは注記）

### Triage 結果の表示

全 finding のトリアージ完了後、以下のフォーマットでユーザーに報告:

```
── δ-lint ── スキャン結果

  #1 [🔴 LIVE]    ④ Guard Non-Propagation — handler.ts vs validator.ts
     caller: 3箇所, デフォルト設定で再現可能
     → 放置すると: バリデーション済みと見なされた未検証データがDBに書き込まれる
  #2 [🟡 DORMANT] ② Semantic Mismatch — config.py vs loader.py
     caller: 1箇所, recursive=False を渡した時のみ発火（現在の呼び出し元はすべて True）
     → 放置すると: recursive=False で呼ばれた場合にネストされた設定が無視される
  #3 [🪦 DEAD]    ① Asymmetric Defaults — old_handler.ts vs utils.ts
     caller: 0箇所（コメントアウト済み）
  #4 [✅ FIXED]   ④ Guard Non-Propagation — auth.go vs middleware.go
     develop ブランチで修正済み（commit abc1234）

🎯 対応推奨: #1 のみ要対応。#2 は条件付きリスク、#3-#4 は無視可。
```

**LIVE/DORMANT の finding には必ず「→ 放置すると:」行を付ける。** finding の `user_impact` フィールドから要約する。これが delta-lint の最大の訴求ポイント — 「コードの問題」ではなく「ユーザーが受ける実害」を伝える。DEAD/FIXED には不要。

**LIVE の finding のみを Step 6 で findings add する。** DEAD/FIXED は記録しない（ノイズ削減）。
DORMANT は findings add するが `--finding-severity` を1段下げる（high→medium, medium→low）。

## Step 5.7: Persona translation（pm / qa の場合のみ）

**`{persona}` が `pm` または `qa` の場合、トリアージ結果を翻訳して表示する。**
`engineer` の場合はこのステップをスキップ。

```bash
cd ~/.claude/skills/delta-lint/scripts && python -c "
import json
from persona_translator import translate

findings = {findings_json}
result = translate(findings, persona='{persona}', verbose=True)
print(result)
"
```

`{findings_json}` は Step 5.5 のトリアージ完了後の LIVE + DORMANT findings を JSON 配列として渡す。

翻訳結果をユーザーに表示する。engineer 向けのテクニカル出力は**表示しない**（翻訳結果のみ）。

## Step 6: Record findings and offer next actions

**If findings exist**:
1. まず `findings list --repo-name {repo}` で既存の記録を確認し、重複を避ける
2. 確認を求めず、**LIVE + DORMANT の findings のみ**を自動で `findings add` する（DEAD/FIXED は記録しない）
3. DORMANT は `--finding-severity` を1段下げて記録する（high→medium, medium→low）
4. 記録完了後、suppress の提案を行う: "suppress したい finding があれば番号を教えてください（例: `/delta-lint suppress 3`）"

**If expired suppressions exist**: "期限切れの suppress があります。再確認して re-suppress するか、対応を検討してください"

**If no findings**: Report clean result and mention suppressed/filtered counts if any
