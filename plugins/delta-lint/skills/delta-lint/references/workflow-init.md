# Workflow 0: Init (`delta init`)

Initialize delta-lint for a repository. Creates a landmine map (risk heatmap) and enables automatic risk awareness.

**Trigger**: User says "delta init", "地雷マップ作って", "initialize delta-lint", or similar.

**CRITICAL: This workflow is FULLY AUTONOMOUS. Do NOT ask the user for confirmation at any step (except if already initialized). Execute Steps 1→2→3 immediately in sequence without pausing.**

## Step 1: Check if already initialized

```bash
ls {repo_path}/.delta-lint/stress-test/results.json 2>/dev/null
```

- If exists: Tell user "このリポは初期化済みです。再実行しますか？" and wait for confirmation.
- If not: **Immediately proceed to Step 2. Do NOT ask "実行しますか？" — the user already said "delta init", that IS the instruction.**

## Step 1.5: Startup animation — EXECUTE IMMEDIATELY

Run the 3-second intro animation. This gives the user a visual "delta-lint is starting" experience while the system initializes.

```bash
cd ~/.claude/skills/delta-lint/scripts && python intro_animation.py
```

**Do NOT skip this step.** It runs in ~3 seconds and sets the first impression. After it completes, immediately proceed to Step 2.

## Step 2: Run stress-test (background) — EXECUTE IMMEDIATELY

**You MUST execute this Bash command right now:**

```bash
cd ~/.claude/skills/delta-lint/scripts && python stress_test.py --repo "{repo_path}" --parallel 10 --verbose --visualize 2>&1
```

Use `run_in_background: true` and `timeout: 600000`.

## Step 2.1: 構造分析の結果を即表示 — CRITICAL UX STEP

**stress-test をバックグラウンドで起動した直後、structure.json が生成されるのを待って読む。**
structure.json は Step 0（構造分析）完了時に生成され、通常10〜30秒で完了する。

```bash
for i in $(seq 1 30); do [ -f "{repo_path}/.delta-lint/stress-test/structure.json" ] && break; sleep 2; done && cd {repo_path} && python3 -c "
import json
d=json.load(open('.delta-lint/stress-test/structure.json'))
modules=d.get('modules',[])
hotspots=d.get('hotspots',[])
constraints=d.get('implicit_constraints',[])
print(f'modules: {len(modules)}')
print(f'hotspots: {len(hotspots)}')
for h in hotspots[:5]:
    print(f'  {h.get(\"path\", h.get(\"file\",\"\"))} — {h.get(\"reason\",\"\")}')
for c in constraints[:5]:
    print(f'  constraint: {c}')
"
```

**このコマンドの結果を使って、以下のフォーマットでユーザーに即座に表示する。これが delta init の第一印象になる。絶対にスキップしないこと：**

```
🔍 delta-lint 初期化中...

📊 リポジトリ概要:
  {n_source_files} ソースファイル ({primary_language})
  {n_modules} モジュール, {n_hotspots} ホットスポット

🔥 依存が集中しているファイル:
  1. {file1} — {reason1}
  2. {file2} — {reason2}
  3. {file3} — {reason3}

⚠️ 検出された暗黙の制約:
  - {constraint1}
  - {constraint2}
  - {constraint3}

📡 ストレステスト実行中（10並列）
  矛盾が見つかり次第、随時報告します。
  この間、通常の作業を続けて大丈夫です。
  なにか確認したいことはありますか？
```

## Step 2.2: 既存バグの表示 — CRITICAL UX STEP

**existing_findings.json が生成されるのを待って読む。** ホットスポットの直接スキャン結果で、構造分析の直後（structure.json の後）に生成される。通常 structure.json から1〜3分後に完了する。

```bash
for i in $(seq 1 90); do [ -f "{repo_path}/.delta-lint/stress-test/existing_findings.json" ] && break; sleep 2; done && cd {repo_path} && python3 -c "
import json
d=json.load(open('.delta-lint/stress-test/existing_findings.json'))
results=d.get('results',[])
hits=[r for r in results if r.get('findings')]
total_f=sum(len(r['findings']) for r in hits)
print(f'clusters: {len(results)}')
print(f'hits: {len(hits)}')
print(f'findings: {total_f}')
for r in results:
    for f in r.get('findings',[]):
        bc=f.get('bug_class','⚪ 潜在リスク')
        pat=f.get('pattern','?')
        loc=f.get('location',{})
        fa=loc.get('file_a','')
        fb=loc.get('file_b','')
        ui=f.get('user_impact','')[:150]
        rp=f.get('reproduction','')[:100]
        print(f'  {bc} | {pat} | {fa} vs {fb}')
        print(f'    影響: {ui}')
        print(f'    再現: {rp}')
"
```

**findings がある場合、bug_class ごとにグループ化してユーザーに報告する。これは init の最大の価値 — 「今すでに壊れている箇所」の報告:**

```
🐛 既存の構造矛盾を {total_findings} 件検出:

🔴 実バグ（今壊れている）:
  パターン{pattern}: {file_a} vs {file_b}
  影響: {user_impact}
  再現: {reproduction}

🟡 サイレント障害（エラーなく間違った結果）:
  パターン{pattern}: {file_a} vs {file_b}
  影響: {user_impact}
  再現: {reproduction}

⚪ 潜在リスク（特定条件で発火）:
  パターン{pattern}: {file_a} vs {file_b}
  影響: {user_impact}
  再現: {reproduction}

🔴 の項目は早期の確認・修正をお勧めします。
🟡 はエラーなく動作しているため気づきにくいですが、影響を確認してください。
```

**findings がない場合は:**

```
✅ ホットスポットの直接スキャン: 既存の構造矛盾は検出されませんでした。
```

## Step 2.5: Add .gitignore for .delta-lint/ — EXECUTE IMMEDIATELY

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

## Step 3: Add guard rules to CLAUDE.md — EXECUTE IMMEDIATELY

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

## Step 3.5: チェックポイントごとの中間報告（任意）

stress-test はバックグラウンドで実行中だが、ユーザーとの会話の合間に進捗を確認し、新しい発見があれば報告する。

チェックポイント（10件ごと）で results.json が更新されるので、以下のコマンドで最新の findings を取得できる：

```bash
cd {repo_path} && python3 -c "
import json, os
f='.delta-lint/stress-test/results.json'
if os.path.exists(f):
    d=json.load(open(f))
    results=d.get('results',[])
    findings=[r for r in results if r.get('findings')]
    print(f'進捗: {len(results)} スキャン完了, {len(findings)} 件で矛盾検出')
    # Show latest interesting finding
    for r in reversed(results):
        if r.get('findings'):
            f0=r['findings'][0]
            print(f'  最新: {f0.get(\"pattern\",\"\")} — {f0.get(\"file_a\",\"\")} vs {f0.get(\"file_b\",\"\")}')
            print(f'  概要: {f0.get(\"contradiction\",\"\")[:100]}...')
            break
else:
    print('まだスキャン結果なし')
"
```

見つかった矛盾が興味深い場合、ユーザーに報告：

```
📡 チェックポイント: {n}件スキャン完了、{hits}件で矛盾検出

🐛 発見した矛盾の例:
  [{pattern_name}] {file_a} vs {file_b}
  {contradiction の要約}

引き続きスキャン中...
```

**ユーザーが別の作業で忙しい場合は中間報告を控える。会話の自然な切れ目で報告する。**

## Step 4: When stress-test completes

When the background task notification arrives:
1. Read the output file to get the summary
2. Open the heatmap: `open {repo_path}/.delta-lint/stress-test/landmine_map.html`
3. Report to user exactly this format (fill in actual data):

```
✅ delta-lint 初期化完了

📊 結果サマリー:
- 既存バグ: {existing_findings} 件の構造矛盾を現在のコードから検出
- ストレステスト: {hit_mods}/{total_mods} 件の仮想改修で矛盾を検出（ヒット率 {hit_rate}%）
- 発見: {total_findings} 件の構造矛盾（改修リスク）
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

## If stress-test fails

1. Read stderr to diagnose
2. Common fixes:
   - `claude -p failed` → suggest `--backend api`
   - Timeout → suggest `--n 30`
   - Not a git repo → tell user
3. **Auto-retry once** before reporting to user
