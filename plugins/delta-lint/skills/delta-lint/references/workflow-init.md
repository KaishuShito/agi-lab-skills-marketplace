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

## Step 1.5: Instant banner — OUTPUT IMMEDIATELY (テキスト出力のみ)

**このステップでは Bash ツールを使わない。** Claude のテキスト出力として以下をそのまま表示する。これが最初にユーザーの目に入るもの。
（※ 次の Step 2 では Bash でスクリプトを実行する。「Bash を使わない」はこのバナー表示ステップのみの指示）

```
── δ-lint ── 初期化開始
  デグレ特化型構造矛盾検出
  ストレステストを開始します...
```

**この出力を最初に行ってから** Step 2 に進む。外部スクリプトは実行しない。

## Step 2: Run stress-test (background) — EXECUTE IMMEDIATELY

**You MUST execute this Bash command right now:**

```bash
cd ~/.claude/skills/delta-lint/scripts && python stress_test.py --repo "{repo_path}" --parallel 10 --verbose --visualize --max-wall-time 2400 2>&1
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

## Step 2.15: 過去バグ履歴の収集 — BACKGROUND CONTEXT

**structure.json の待ち時間を利用して、リポの過去バグ傾向を収集する。**
この情報は `.delta-lint/bug_history.json` に保存し、以降のスキャン時にコンテキストとして活用する。

```bash
cd {repo_path} && python3 -c "
import json, subprocess, os, re
result = {}

# 0. リモートURLからowner/repoを取得
try:
    remote = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True).stdout.strip()
    repo_slug = '/'.join(remote.replace('.git','').split('/')[-2:])
    if ':' in repo_slug:
        repo_slug = repo_slug.split(':')[-1]
    result['repo_slug'] = repo_slug
except Exception:
    repo_slug = ''

    # 1. ラベル体系の把握 + 両方の経路で検索してマージ
if repo_slug:
    try:
        labels_out = subprocess.run(
            ['gh', 'label', 'list', '--repo', repo_slug, '--limit', '200', '--json', 'name'],
            capture_output=True, text=True, timeout=15
        )
        all_labels = [l['name'] for l in json.loads(labels_out.stdout)] if labels_out.returncode == 0 else []
        result['all_labels'] = all_labels

        bug_pat = re.compile(r'bug|defect|regression|broken|error|crash|fault', re.I)
        bug_labels = [l for l in all_labels if bug_pat.search(l)]
        result['bug_labels'] = bug_labels

        # 両方の経路で検索して URL で重複排除
        seen_urls = set()
        all_issues = []
        all_prs = []

        # 経路A: ラベル検索（各バグ系ラベルで）
        for label in bug_labels[:3]:
            try:
                r = subprocess.run(
                    ['gh', 'search', 'issues', '--repo', repo_slug, '--label', label,
                     '--limit', '30', '--json', 'title,url,state,createdAt,labels'],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    for item in json.loads(r.stdout):
                        if item['url'] not in seen_urls:
                            seen_urls.add(item['url'])
                            item['_source'] = 'label:' + label
                            all_issues.append(item)
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['gh', 'search', 'prs', '--repo', repo_slug, '--label', label,
                     '--state', 'merged', '--limit', '20', '--json', 'title,url,mergedAt'],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    for item in json.loads(r.stdout):
                        if item['url'] not in seen_urls:
                            seen_urls.add(item['url'])
                            item['_source'] = 'label:' + label
                            all_prs.append(item)
            except Exception:
                pass

        # 経路B: タイトル検索（ラベルの有無に関係なく常にやる）
        try:
            r = subprocess.run(
                ['gh', 'search', 'issues', '--repo', repo_slug, 'bug OR fix OR regression OR broken',
                 '--limit', '50', '--json', 'title,url,state,createdAt,labels'],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                for item in json.loads(r.stdout):
                    if item['url'] not in seen_urls:
                        seen_urls.add(item['url'])
                        item['_source'] = 'title_search'
                        all_issues.append(item)
        except Exception:
            pass
        try:
            r = subprocess.run(
                ['gh', 'search', 'prs', '--repo', repo_slug, 'fix OR bug OR regression OR revert',
                 '--state', 'merged', '--limit', '30', '--json', 'title,url,mergedAt'],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                for item in json.loads(r.stdout):
                    if item['url'] not in seen_urls:
                        seen_urls.add(item['url'])
                        item['_source'] = 'title_search'
                        all_prs.append(item)
        except Exception:
            pass

        result['bug_issues'] = all_issues[:80]
        result['bugfix_prs'] = all_prs[:50]

        # ラベルカバレッジ: ラベル経路の件数 vs 全件数で信頼度を判定
        label_hits = len([i for i in all_issues if i.get('_source','').startswith('label:')])
        title_only = len([i for i in all_issues if i.get('_source') == 'title_search'])
        result['label_coverage'] = {
            'labeled': label_hits,
            'title_only': title_only,
            'total': len(all_issues),
            'reliability': 'high' if label_hits > title_only else 'low' if label_hits < title_only * 0.3 else 'medium',
        }
    except Exception as e:
        result['gh_error'] = str(e)

# 2. git log: fix/revert/bug コミット（直近6ヶ月）
try:
    log = subprocess.run(
        ['git', 'log', '--since=6 months', '--grep=fix\\|revert\\|bug', '-i',
         '--format=%H|%s|%an|%ai', '--', '*.py', '*.ts', '*.js', '*.go', '*.rs', '*.java', '*.rb', '*.php'],
        capture_output=True, text=True, timeout=15
    )
    commits = []
    for line in log.stdout.strip().split('\n'):
        if '|' in line:
            parts = line.split('|', 3)
            commits.append({'hash': parts[0][:8], 'subject': parts[1], 'author': parts[2], 'date': parts[3][:10]})
    result['bugfix_commits'] = commits[:100]
except Exception as e:
    result['git_error'] = str(e)

# 3. ファイル別バグ頻度
try:
    freq = subprocess.run(
        ['git', 'log', '--since=6 months', '--grep=fix\\|revert\\|bug', '-i',
         '--name-only', '--format='],
        capture_output=True, text=True, timeout=15
    )
    from collections import Counter
    files = [f.strip() for f in freq.stdout.split('\n') if f.strip()]
    result['bugfix_hotfiles'] = [{'file': f, 'count': c} for f, c in Counter(files).most_common(20)]
except Exception:
    pass

os.makedirs('.delta-lint', exist_ok=True)
with open('.delta-lint/bug_history.json', 'w') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
bl = result.get('bug_labels', [])
lc = result.get('label_coverage', {})
n_issues = len(result.get('bug_issues', []))
n_prs = len(result.get('bugfix_prs', []))
n_commits = len(result.get('bugfix_commits', []))
n_hotfiles = len(result.get('bugfix_hotfiles', []))
print(f'bug_labels: {bl[:5] if bl else \"(なし)\"}')
if lc:
    print(f'label_coverage: labeled={lc.get(\"labeled\",0)}, title_only={lc.get(\"title_only\",0)}, reliability={lc.get(\"reliability\",\"?\")}'  )
print(f'bug_issues: {n_issues}, bugfix_prs: {n_prs}, bugfix_commits: {n_commits}, hotfiles: {n_hotfiles}')
if result.get('bugfix_hotfiles'):
    print('Top bugfix files:')
    for h in result['bugfix_hotfiles'][:5]:
        print(f'  {h[\"file\"]} ({h[\"count\"]}回)')
"
```

**結果がある場合、ユーザーに報告する:**

```
📋 過去バグ履歴:
  ラベル体系: {bug_labels} / 信頼度: {reliability}
    (high=ラベル運用が定着 / medium=混在 / low=ラベルほぼ未使用)
  GitHub Issues: {n_issues} 件 (ラベル経由 {labeled}件 + タイトル検索 {title_only}件)
  マージ済み bugfix PR: {n_prs} 件
  bugfix コミット(6ヶ月): {n_commits} 件

🔧 バグ修正が多いファイル TOP 5:
  1. {file1} — {count1}回
  2. {file2} — {count2}回
  ...

この情報を以降のスキャンで優先度判定に使います。
```

**gh コマンドが使えない（認証なし等）場合は git log のみで進める。エラーでブロックしない。**

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

### ポーリング方法: 1回読み取りコマンドを繰り返し実行

**重要: `while True` ループのフォアグラウンドコマンドは使わない。** 代わりに、1回で即座に終了するコマンドを自分のループで繰り返し呼ぶ。

1回分のコマンド（即座に終了する）:
```bash
cd {repo_path} && python3 -c "
import json, os
f='.delta-lint/stress-test/results.json'
if not os.path.exists(f):
    print('WAITING')
else:
    try:
        d=json.load(open(f))
        results=d.get('results',[])
        total=d.get('metadata',{}).get('n_modifications',0)
        hits=[r for r in results if r.get('findings')]
        count=len(results)
        status=d.get('metadata',{}).get('status','running')
        pct=int(count*100/total) if total else 0
        if total and count>=total:
            print(f'DONE|{count}|{total}|{len(hits)}|{pct}')
        elif status=='timeout':
            print(f'TIMEOUT|{count}|{total}|{len(hits)}|{pct}')
        else:
            print(f'PROGRESS|{count}|{total}|{len(hits)}|{pct}')
        for r in reversed(results):
            if r.get('findings'):
                f0=r['findings'][0]
                print(f'LATEST|{f0.get(\"pattern\",\"\")}|{f0.get(\"contradiction\",\"\")[:80]}')
                break
    except: print('ERROR|read failed')
"
```

### ポーリング手順（YOU が制御するループ）

1. 上のコマンドを実行する（即座に結果が返る）
2. 出力を読んでユーザーに中間報告する
3. `DONE` or `TIMEOUT` が出たら → Step 4 へ
4. それ以外 → **5分 sleep してから再度 1 に戻る**
5. **最大8回**（= 40分）で打ち切り。途中結果で Step 4 へ

**sleep コマンド例:**
```bash
sleep 300
```

### 中間報告フォーマット

各ポーリングごとにユーザーに報告する:

```
📡 [{pct}%] {done}/{total} スキャン完了 — {hits}件で矛盾検出
  最新: {pattern} — {contradiction の要約}
```

### 注意事項

- **ポーリングコマンドは即座に終了する** ので、ユーザーの質問にいつでも応答できる
- ユーザーが途中で別の質問をしたら対応してよい。ストレステストはバックグラウンドで継続中
- 対応後にポーリングを再開するか、Step 4 に進む
- `TIMEOUT` の場合: 途中結果でも地雷マップは生成済み。「{done}/{total} 件まで完了。残りは `delta scan --lens stress` で再開可能」と報告

## Step 4: When stress-test completes

When the background task notification arrives:
1. Read the output file to get the summary
2. Open the dashboard: `open {repo_path}/.delta-lint/findings/dashboard.html`
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
