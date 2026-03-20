# Workflow: Auto Check (lightweight pre-implementation guard)

**実装系の依頼が来たとき、自動で走る軽量チェック。確認を求めず、止めない。**

## 設計思想

- ユーザーは「実装して」と言っている → 止めたら邪魔
- でも考慮漏れは防ぎたい → 情報を**先に出す**
- 3秒で終わる。LLM呼び出しなし。ファイル読むだけ。

## Step 1: データ読み込み（並列・即座に）

以下のファイルを読む（存在するものだけ）:

```bash
# structure.json — モジュール構成、hotspots
cat {repo_path}/.delta-lint/stress-test/structure.json 2>/dev/null

# constraints.yml — チームが登録した暗黙の制約
cat {repo_path}/.delta-lint/constraints.yml 2>/dev/null

# findings — 既存の構造矛盾
cd ~/.claude/skills/delta-lint/scripts && python -c "
from findings import list_findings
import json
findings = list_findings('{repo_path}')
active = [f for f in findings if f.get('status') in ('found', 'suspicious', 'confirmed', 'submitted')]
if active:
    for f in active[:10]:
        print(f'{f.get(\"pattern\",\"?\")} {f.get(\"severity\",\"?\")} {f.get(\"file\",\"\")} — {f.get(\"title\",\"\")[:80]}')
    if len(active) > 10:
        print(f'  ... +{len(active)-10} more')
else:
    print('(なし)')
"
```

## Step 2: ユーザーの要望と照合

読み込んだデータから、ユーザーの要望に**関係するもの**だけ抽出する:

1. **hotspots**: 要望が触りそうなファイルが hotspot に含まれているか
2. **constraints**: 要望に関連する暗黙の制約があるか
3. **active findings**: 要望が触りそうなファイルに既存の矛盾があるか
4. **dev_patterns**: 要望が触りそうなディレクトリの開発パターン（bug-prone 等）

**関係するものがなければ何も出さない。** ノイズを出さないことが最重要。

## Step 3: 結果を 1-3 行で出力

関連する情報がある場合のみ、以下のフォーマットで出力する:

```
── δ pre-check ──
  ⚠ {file} は hotspot（{reason}）
  ⚠ 既存 finding: {pattern} {file_a} ↔ {file_b} — {title}
  📋 制約: {constraint_text}
```

**出力後、確認を求めず、そのまま実装/回答に入る。**

## 出さないもの

- 要望と無関係な情報
- 「実行しますか？」等の確認
- 長い分析レポート（それは FULL MODE でやる）
- hotspots も constraints も findings も関係なければ **何も出さない**（silent pass）
