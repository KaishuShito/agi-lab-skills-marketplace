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
**init 中に記録された findings（`found_by: delta-init` / `found_by: first-blood`）も Step 7 の調査対象に含まれる。** init 由来だからといって `found` のまま放置しない。
**ファーストブラッド（init Step 2.1.5）で `confirmed` 済みの finding は、Step 7 で再調査不要。** ステータスが既に `confirmed` なので完走チェックでも `found` にカウントされない。

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

## Step 0.3: Detect output language (--lang)

**ユーザーが日本語で指示した場合は `--lang ja` を付ける。** 英語で指示した場合は `--lang en`（デフォルト）。

- ユーザーの入力が日本語 → `--lang ja`
- ユーザーの入力が英語 → `--lang en`
- `.delta-lint/config.json` に `"lang": "ja"` がある → `--lang ja`

Step 1 以降のすべてのコマンドに `--lang {lang}` を付与する。

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
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --lang {lang} --verbose 2>&1
```

**Custom period (e.g. 1 year):**
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --lang {lang} --since 1year --verbose 2>&1
```

**PR mode:**
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --lang {lang} --scope pr --verbose 2>&1
```

**If cli.py reports 0 files** (「直近 3months に変更されたソースファイルがありません」):
The repo has no recent commits (fork, archive, etc.). Re-run with `--scope smart`:
```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --lang {lang} --scope smart --verbose 2>&1
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
各 finding について以下のチェックを実行し、liveness ラベルを付与する。

### Check 0: Already reported（既存 Issue/PR の確認）

finding の対象ファイル名・関数名・エラー内容で、GitHub 上に既存の Issue/PR がないか確認する:

```bash
# ファイル名や関数名でIssue/PRを検索
gh search issues --repo {owner/repo} "{file_name OR function_name}" --limit 5
gh search prs --repo {owner/repo} "{file_name OR function_name}" --state all --limit 5
```

検索キーワードは finding ごとに適切に選ぶ（ファイル名、関数名、エラーメッセージの一部など）。

- 同じ問題の Issue/PR がオープン中 → `🔁 KNOWN (Issue #NNN)` — 重複報告しない
- 同じ問題の PR がマージ済み → `✅ FIXED (PR #NNN)` — 修正済み
- WONTFIX / by design でクローズ済み → `🔁 KNOWN (wontfix)` — 再報告しない
- 見つからない → Check 1 へ

**KNOWN の finding は DEAD/FIXED と同様、findings add しない。** ただし参考としてトリアージ結果には含める。

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

- **デフォルト値で発火**: 追加設定なしで再現 → Check 4 へ
- **特定の設定/入力で発火**: 条件は限定的だが到達可能 → `🟡 DORMANT`（条件を明記）
- **現設定では到達不能**: 将来の変更で発火する可能性のみ → `🟡 DORMANT`（リスクは注記）

### Check 4: Confirmed Bug（確定バグ判断 — Issue/PR を出して恥ずかしくないか？）

Check 3 で到達可能と判定された finding に対して、**本当にバグか、意図的な設計か**を最終確認する。
これは Issue/PR 提出前の最終ゲート。以下の観点で検証する:

**4a. 意図的な設計ではないか？**
- コメントや CHANGELOG に「intentional」「by design」「won't fix」等の記述がないか確認
- 同じパターンがリポ内の複数箇所で一貫しているか（一貫していれば設計方針の可能性が高い）
```bash
cd {repo_path} && git log --all --oneline -- {file_a} {file_b} | grep -iE "intent|design|wont.?fix|by.design|deliberate" | head -5
```

**4b. テストが意図を証明していないか？**
- 該当の振る舞いをテストが明示的に期待していないか確認
```bash
cd {repo_path} && grep -rn "{関数名\|変数名\|値}" --include="*test*" --include="*spec*" --include="*_test.*" | head -10
```
- テストが「この値を返すこと」を assert しているなら、それは仕様であってバグではない

**4c. 内部証拠の強度は十分か？**
- finding の `internal_evidence`（同リポ内の正しい実装例）があるか？
  - **あり**: 同じリポの別箇所に正しいパターンが存在 → バグの確度が高い
  - **なし**: 正しいパターンが見つからない → 仕様である可能性を疑う

**4d. 影響の具体性**
- 「ユーザーが実際に遭遇するシナリオ」を1文で書けるか？
  - 書ける → 確定バグ `🔴 CONFIRMED`
  - 書けない（理論上の矛盾だが実害が不明） → `🟡 SUSPICIOUS`（findings add するが Issue/PR は出さない）

**判定結果:**
- 4a〜4d すべてクリア → `🔴 CONFIRMED` — Issue/PR を出してよい
- テストが意図を証明 or 設計判断の痕跡あり → `⚪ BY_DESIGN` — 報告しない
- 内部証拠なし + 影響シナリオが曖昧 → `🟡 SUSPICIOUS` — findings add するが Issue/PR は推奨しない

### Triage 結果の表示

全 finding のトリアージ完了後、以下のフォーマットでユーザーに報告:

```
── δ-lint ── スキャン結果

  #1 [🔴 CONFIRMED] ④ Guard Non-Propagation — handler.ts vs validator.ts
     caller: 3箇所, デフォルト設定で再現可能
     内部証拠: create_handler.ts:45 に同じガードあり
     → 放置すると: バリデーション済みと見なされた未検証データがDBに書き込まれる
  #2 [🟡 SUSPICIOUS] ① Asymmetric Defaults — encoder.ts vs decoder.ts
     caller: 5箇所, 到達可能だが内部証拠なし
     → テストが現在の振る舞いを期待している可能性あり。Issue/PR は慎重に。
  #3 [🟡 DORMANT] ② Semantic Mismatch — config.py vs loader.py
     caller: 1箇所, recursive=False を渡した時のみ発火（現在の呼び出し元はすべて True）
     → 放置すると: recursive=False で呼ばれた場合にネストされた設定が無視される
  #4 [🔁 KNOWN]   ① Asymmetric Defaults — parser.ts vs serializer.ts
     既存 Issue #1234 で報告済み（open）
  #5 [🪦 DEAD]    ① Asymmetric Defaults — old_handler.ts vs utils.ts
     caller: 0箇所（コメントアウト済み）
  #6 [✅ FIXED]   ④ Guard Non-Propagation — auth.go vs middleware.go
     develop ブランチで修正済み（commit abc1234）

🎯 対応推奨: #1 は確定バグ → Issue/PR 推奨。
   #2 は要検討（テスト確認後に判断）。#3 は条件付きリスク。#4-#6 は無視可。
```

**CONFIRMED/SUSPICIOUS/DORMANT の finding には必ず「→ 放置すると:」行を付ける。** finding の `user_impact` フィールドから要約する。これが delta-lint の最大の訴求ポイント — 「コードの問題」ではなく「ユーザーが受ける実害」を伝える。DEAD/FIXED/KNOWN/BY_DESIGN には不要。

**CONFIRMED の finding のみを Issue/PR 提出候補とする。**
SUSPICIOUS は findings add するが Issue/PR は推奨しない（追加調査を促す）。
DORMANT は findings add するが `--finding-severity` を1段下げる（high→medium, medium→low）。
DEAD/FIXED/KNOWN/BY_DESIGN は findings add しない。

### トリアージラベル → findings ステータス マッピング（CRITICAL — 必ず従う）

Step 7 の調査完了後、以下のマッピングに従って `findings update` でステータスを更新する。
**`found` のまま残してはならない。** 全件を調査し、必ずいずれかのステータスに更新する。

| トリアージラベル | findings ステータス | 意味 | Issue/PR |
|-----------------|-------------------|------|----------|
| 🔴 CONFIRMED | `confirmed` | 4a〜4d 全クリア。OSSに Issue/PR を出せるレベルの確定バグ | `debt-loop` で提案可（scan 自体は PR を出さない） |
| 🟡 SUSPICIOUS | `suspicious` | 高確率バグだが確証不足（内部証拠なし / 影響が曖昧） | 出さない |
| 🟡 DORMANT | `suspicious` | 到達可能だが特定条件のみで発火 | 出さない |
| 🪦 DEAD | `wontfix` | caller ゼロ / デッドコード | — |
| ✅ FIXED | `wontfix` | 他ブランチで修正済み | — |
| 🔁 KNOWN | `duplicate` | 既存 Issue/PR あり | — |
| ⚪ BY_DESIGN | `wontfix` | テスト/コメントで意図が裏付け | — |
| （LLM の誤検出） | `false_positive` | コード精読で矛盾が存在しないと確認 | — |

**scan は検出・トリアージ・ステータス更新までが責務。PR/Issue の作成は `debt-loop` の責務。** scan の中で PR を自動作成してはならない。

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
2. 確認を求めず、**LIVE + DORMANT の findings のみ**を自動で `findings add` する（DEAD/FIXED/KNOWN は記録しない）
3. DORMANT は `--finding-severity` を1段下げて記録する（high→medium, medium→low）
4. 記録完了後、**Step 7（自動調査）に進む**
5. suppress の提案を行う: "suppress したい finding があれば番号を教えてください（例: `/delta-lint suppress 3`）"

## Step 7: 自動調査 & ステータス更新（MANDATORY — スキャンの一部）

**これは scan の必須最終ステップである。Step 6 完了後、ユーザーの指示を待たず自動的に全件を調査・判定・ステータス更新する。`found` のまま残る finding がゼロになるまで完走すること。**

### なぜ自律完走が必要か

- ユーザーに「ステータス更新して」と言わせるのは UX として失格
- `found` のままの finding は「未確認の発見」であり、confirmed か false_positive かわからない中途半端な状態
- scan の価値は「確実バグかどうかの判定」まで出して初めて成立する
- **Step 7 を完走しない scan は未完了の scan である**

### 調査手順（finding ごとに全チェックを実行）

各 finding について以下を**すべて**実行する。1つでもスキップしない。

1. **ソースコード精読**: finding の `file_a`, `file_b` を Read で読む（grep だけで判断しない。関数全体を読む）
2. **矛盾の実在確認**: LLM が指摘した矛盾が実際のコードに存在するか、自分の目で確認
3. **caller 確認**: 矛盾箇所を呼び出すコードパスが存在するか grep + Read で確認
4. **到達可能性**: デフォルト設定で発火するか、特定条件のみか
5. **意図確認**: テストが現在の振る舞いを expect しているか、コメントに by design 等あるか
6. **内部証拠**: 同リポ内に正しい実装例があるか（あればバグの確度が高い）

### 判定 → ステータス更新（Step 5.5 のマッピング表に従う）

| 調査結果 | ステータス |
|----------|-----------|
| 矛盾が実在 + デフォルトで到達可能 + 内部証拠あり + 影響シナリオ明確 | `confirmed` |
| 矛盾が実在するが、内部証拠なし / 影響が曖昧 / 条件付き到達のみ | `suspicious` |
| caller ゼロ / デッドコード | `wontfix` |
| 他ブランチで修正済み | `wontfix` |
| テストが現在の振る舞いを assert / by design | `wontfix` |
| 既存 Issue/PR あり | `duplicate` |
| LLM の指摘自体が誤り（コードを読んで矛盾が存在しない） | `false_positive` |

### ステータス更新コマンド

```bash
cd ~/.claude/skills/delta-lint/scripts && python3 -c "
from findings import update_status
update_status('{repo_path}', '{repo_name}', '{finding_id}', '{new_status}')
"
```

### 報告フォーマット（調査完了後、全件まとめて報告）

```
── δ-lint ── 調査完了: {total}件中 {confirmed}件確定 / {suspicious}件要注意 / {wontfix+fp}件除外

dl-{id}: {title の先頭60文字}
  → confirmed — {根拠を1行で}
dl-{id}: {title}
  → suspicious — {なぜ確証不足か1行で}
dl-{id}: {title}
  → false_positive — {なぜ誤検出か1行で}
...

🎯 confirmed の {n}件は Issue/PR 提出候補です。提出しますか？
```

### 完走チェック

調査完了後、以下を確認する：

```bash
cd ~/.claude/skills/delta-lint/scripts && python3 -c "
from findings import list_findings
ff = [f for f in list_findings('{repo_path}') if f.get('status') == 'found']
print(f'remaining found: {len(ff)}')
for f in ff: print(f'  {f[\"id\"]}: {f.get(\"title\",\"\")[:60]}')
"
```

- `remaining found: 0` → 完走。ダッシュボードを再生成して終了
- `remaining found: N` → **まだ終わっていない。残りを調査してからダッシュボードを再生成**

### ダッシュボード再生成（全件完走後）

```bash
python ~/.claude/skills/delta-lint/scripts/cli.py view --regenerate --repo {repo_path}
```

### 重要ルール

- **ユーザーの指示を待たない。** Step 6 → Step 7 は自動で進む
- **`found` が 0 になるまで終わらない。** これが scan の完了条件
- 調査中にユーザーが別の指示を出した場合は、そちらを優先してよい。ただし戻ってきたら残りを完走する
- 1件の調査に時間がかかりすぎる場合でも `suspicious` に倒して先に進む（`found` のまま放置しない）

**If expired suppressions exist**: "期限切れの suppress があります。再確認して re-suppress するか、対応を検討してください"

**If no findings**: Report clean result and mention suppressed/filtered counts if any
