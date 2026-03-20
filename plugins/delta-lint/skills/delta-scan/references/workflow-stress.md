# Workflow: Stress Test

Stress test は 10-30 分かかる重いスキャン。**必ずバックグラウンド実行し、5分ごとに中間報告する。**

## Step 1: ユーザーに即応答

```
── δ-lint ── ストレステスト開始 🔥
バックグラウンドで仮想改修 × スキャンを実行します（10-30分）。
5分ごとに進捗を報告します。その間、別の質問にもお答えできます。
```

## Step 2: バックグラウンドで stress_test.py を起動

```bash
cd ~/.claude/skills/delta-lint/scripts && python stress_test.py --repo "{repo_path}" --parallel 10 --verbose --visualize --max-wall-time 2400 --lang ja 2>&1
```

**`run_in_background: true` で実行すること。** `block_until_ms: 0` を設定する。

## Step 3: 5分ごとにポーリング → 中間報告

以下のコマンドを**フォアグラウンドで実行**する（即座に終了する）:

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

### ポーリング手順

1. 上のコマンドを実行する（即座に結果が返る）
2. 出力を読んでユーザーに中間報告する
3. `DONE` or `TIMEOUT` が出たら → Step 4 へ
4. それ以外 → `sleep 300`（5分待つ）してから 1 に戻る
5. 最大8回（= 40分）で打ち切り。途中結果で Step 4 へ

### 中間報告フォーマット

```
📡 [{pct}%] {done}/{total} スキャン完了 — {hits}件で矛盾検出
  最新: {pattern} — {contradiction の要約}
```

## Step 4: 完了報告

```bash
cd {repo_path} && python3 -c "
import json
d=json.load(open('.delta-lint/stress-test/results.json'))
results=d['results']
hits=[r for r in results if r.get('findings')]
total_findings=sum(len(r.get('findings',[])) for r in results)
from collections import Counter
file_hits=Counter()
for r in results:
    if r.get('findings'):
        f=r['modification'].get('file','')
        if f: file_hits[f]+=1
        for af in r['modification'].get('affected_files',[]):
            file_hits[af]+=1
print(f'SUMMARY|{len(results)}|{len(hits)}|{total_findings}')
for f,c in file_hits.most_common(5):
    print(f'TOP|{f}|{c}')
"
```

報告フォーマット:

```
── δ-lint ── ストレステスト完了 ✅

📊 結果:
- {done}/{total} 件の仮想改修をスキャン（ヒット率 {hit_rate}%）
- {total_findings} 件の構造矛盾を検出

🔴 高リスクファイル TOP 5:
1. {file} — {hits}回被弾
...

ダッシュボードで確認: `/delta-view`
```

その後 `ingest_stress_test_debt` を実行:
```bash
cd ~/.claude/skills/delta-lint/scripts && python -c "from findings import ingest_stress_test_debt; added=ingest_stress_test_debt('{repo_path}'); print(f'{len(added)} debt findings registered' if added else 'no new debt')"
```

## 注意事項

- ユーザーが途中で別の質問をしたら、ポーリングを中断して対応してよい
- ストレステストはバックグラウンドで継続中なので、対応後にポーリングを再開する
- `TIMEOUT` の場合でも途中結果は保存済み。「残りは再度 `delta scan --lens stress` で継続可能」と伝える
