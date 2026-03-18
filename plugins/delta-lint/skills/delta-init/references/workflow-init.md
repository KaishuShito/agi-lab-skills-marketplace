# Workflow 0: Init (`delta init`)

Initialize delta-lint for a repository. Creates a landmine map (risk heatmap) and enables automatic risk awareness.

**Trigger**: User says "delta init", "地雷マップ作って", "initialize delta-lint", or similar.

**CRITICAL: This workflow is FULLY AUTONOMOUS. Do NOT ask the user for confirmation at any step (except if already initialized). Execute Steps 1→2→3 immediately in sequence without pausing.**

## Step 0.5: Check git availability

```bash
git -C "{repo_path}" rev-parse --is-inside-work-tree 2>/dev/null
```

- If git repo: proceed normally.
- If NOT git repo: **proceed anyway**, but display this warning once:

```
⚠️ git リポジトリではないため、.gitignore によるフィルタリングが使えません。
node_modules 等は自動除外しますが、git 管理下のリポジトリと比べて精度が下がります。
git init してからの実行を推奨します。
```

## Step 1: Check if already initialized

```bash
ls {repo_path}/.delta-lint/stress-test/results.json 2>/dev/null
```

- If exists: Tell user "このリポは初期化済みです。再実行しますか？" and wait for confirmation.
- If not: **Immediately proceed to Step 2. Do NOT ask "実行しますか？" — the user already said "delta init", that IS the instruction.**

## Step 1.5: Instant banner — OUTPUT IMMEDIATELY (NO Bash, NO script)

**Bash ツールを使わない。** Claude のテキスト出力として以下をそのまま表示する。これが最初にユーザーの目に入るもの。

```
── δ-lint ── 初期化開始
  デグレ特化型構造矛盾検出
  ストレステストを開始します...
```

**この出力を最初に行ってから** Step 2 に進む。外部スクリプトは実行しない。

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
── δ-lint ── 初期化中...

📊 リポジトリ概要:
  {n_source_files} ソースファイル ({primary_language})
  {n_modules} モジュール, {n_hotspots} ホットスポット

🔥 変更リスクが高いファイル:
  1. {dir/file1} — {reason1}
  2. {dir/file2} — {reason2}
  3. {dir/file3} — {reason3}
  ※ディレクトリ付き相対パスで表示すること（ファイル名だけにしない）

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
── δ-lint ── 既存バグスキャン: {total_findings} 件検出

🔴 実バグ（今壊れている）:
  パターン{pattern}: {file_a} vs {file_b}
  → 放置すると: {user_impact}
  再現: {reproduction}

🟡 サイレント障害（エラーなく間違った結果）:
  パターン{pattern}: {file_a} vs {file_b}
  → 放置すると: {user_impact}
  再現: {reproduction}

⚪ 潜在リスク（特定条件で発火）:
  パターン{pattern}: {file_a} vs {file_b}
  → 放置すると: {user_impact}
  再現: {reproduction}

🔴 の項目は早期の確認・修正をお勧めします。
🟡 はエラーなく動作しているため気づきにくいですが、影響を確認してください。
```

**findings がない場合は:**

```
✅ ホットスポットの直接スキャン: 既存の構造矛盾は検出されませんでした。
```

## Step 2.5: .gitignore は自動生成 — 操作不要

`stress_test.py` が `.delta-lint/.gitignore`（`*` + `!.gitignore`）を自動生成する。
プロジェクトの root `.gitignore` を編集する必要はない。

チームで地雷マップを共有したい場合は、`.delta-lint/.gitignore` を編集して必要なファイルを除外解除する。

## Step 3: Add guard rules to CLAUDE.md — EXECUTE IMMEDIATELY

**Do not wait for Step 2 to complete.** While the stress-test runs in background, add the following to `{repo_path}/CLAUDE.md` (create if not exists, append if exists):

```markdown

## delta-lint guard（必須）
- .delta-lint/stress-test/results.json に地雷マップデータがある
- **コード改修の前に必ず `/delta-review` を実行すること**（新機能・バグ修正・リファクタリング・性能改善等、コードを変更するタスク全般）
  - `/delta-review` スキルが利用可能であればそれを使う
  - 利用できない場合は、以下で影響ファイルの risk_score を確認してからコードを書く:
    `python -c "import json; d=json.load(open('.delta-lint/stress-test/results.json')); risks={r['modification']['file']: len(r['findings']) for r in d['results'] if r['findings']}; print(risks.get('TARGET_FILE', 'not in map'))"`
- risk_score > 0 のファイルを編集する場合、ユーザーに警告すること
- コード改修後は `/delta-scan` の実行を提案すること
```

## Step 3.5: 自動進捗ポーリング — MANDATORY

**ユーザーに「今どう？」と聞かせてはならない。** Step 2.5 と Step 3 を完了したら、stress-test 完了まで自動で進捗をポーリングし続ける。

**ポーリングループ: 30秒ごとに results.json を読んで進捗報告する。** stress-test が完了するまで繰り返す。

```bash
cd {repo_path} && python3 -c "
import json, os, time
f='.delta-lint/stress-test/results.json'
prev_count=0
while True:
    if os.path.exists(f):
        try:
            d=json.load(open(f))
            results=d.get('results',[])
            total=d.get('total_modifications', 0)
            hits=[r for r in results if r.get('findings')]
            count=len(results)
            if count > prev_count:
                pct=int(count*100/total) if total else 0
                print(f'PROGRESS|{count}|{total}|{len(hits)}|{pct}')
                for r in reversed(results):
                    if r.get('findings'):
                        f0=r['findings'][0]
                        print(f'LATEST|{f0.get(\"pattern\",\"\")}|{f0.get(\"contradiction\",\"\")[:80]}')
                        break
                prev_count=count
            if total and count>=total:
                print('DONE')
                break
        except: pass
    time.sleep(30)
"
```

**このコマンドはフォアグラウンドで実行する（`run_in_background: false`）。** 30秒ごとに出力が来るので、それを読んでユーザーに報告する。

出力の解釈:
- `PROGRESS|{done}|{total}|{hits}|{pct}` → 進捗報告
- `LATEST|{pattern}|{contradiction}` → 最新の発見
- `DONE` → 完了、Step 4 へ

報告フォーマット（30秒ごと）:

```
📡 [{pct}%] {done}/{total} スキャン完了 — {hits}件で矛盾検出
  最新: {pattern} — {contradiction の要約}
```

**ユーザーが途中で別の質問をしたら、ポーリングを中断して対応してよい。** ただしストレステストはバックグラウンドで継続中なので、対応後にポーリングを再開するか、Step 4 の完了通知を待つ。

## Step 4: When stress-test completes

When the background task notification arrives:
1. Read the output file to get the summary
2. Open the heatmap: `open {repo_path}/.delta-lint/stress-test/landmine_map.html`
3. Report to user exactly this format (fill in actual data):

```
── δ-lint ── 初期化完了 ✅

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
以降、高リスクファイルとして扱います。
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
