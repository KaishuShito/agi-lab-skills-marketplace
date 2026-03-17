# delta-lint

**構造矛盾検出器** — コードモジュール間の暗黙の前提が破れている箇所を LLM で検出します。

スタイルやシンプルなバグではなく、**設計レベルの不整合**（あるモジュールの前提が別のモジュールの振る舞いと矛盾している箇所）を見つけます。

## セットアップ

### 必須

- **Python 3.11+**
- **git** — diff ベースのスキャンに使用

### LLM バックエンド（いずれか1つ）

| 方法 | コスト | セットアップ |
|------|--------|------------|
| **claude CLI**（推奨） | $0（サブスクリプション内） | `npm install -g @anthropic-ai/claude-code` |
| **Anthropic API** | 従量課金 | `export ANTHROPIC_API_KEY='sk-ant-...'` |

### 自動インストール

delta-lint は初回スキャン時に不足している依存を自動でインストールします：

```
claude CLI がない → npm install -g @anthropic-ai/claude-code を試行
anthropic SDK がない → pip install anthropic を試行
PyYAML がない → pip install pyyaml を試行
```

インストールが拒否された場合やエラーの場合も、利用可能な代替手段に自動フォールバックして動作を継続します。手動介入は不要です。

## 使い方

### Claude Code から（推奨）

```
> delta init          # リポジトリの初期化（地雷マップ生成）
> delta scan          # 変更ファイルのスキャン
> delta plan          # コード改修の影響範囲分析
```

### CLI から直接

```bash
cd plugins/delta-lint/scripts

# 変更ファイルをスキャン
python cli.py scan --repo /path/to/repo

# 特定ファイルをスキャン
python cli.py scan --files src/handler.ts src/router.ts

# 日本語で出力
python cli.py scan --lang ja

# APIバックエンドを使用
python cli.py scan --backend api
```

## 検出する矛盾パターン

| # | パターン | 例 |
|---|---------|---|
| ① | Asymmetric Defaults | 登録時は `null` を受け入れるが表示時は `undefined` を空文字に変換 |
| ② | Semantic Mismatch | `status: 0` がモジュール A では "pending"、B では "inactive" |
| ③ | External Spec Divergence | RFC 7230 に準拠すると書いてあるが実装が逸脱 |
| ④ | Guard Non-Propagation | create エンドポイントにはバリデーションがあるが update にはない |
| ⑤ | Paired-Setting Override | `timeout=30s` と `retries=5` の組み合わせが上流の制限を超える |
| ⑥ | Lifecycle Ordering | エラーリカバリパスでは認証ミドルウェアがルートハンドラの後に実行される |

## 設定

`.delta-lint/config.json` をリポジトリルートに配置：

```json
{
  "lang": "ja",
  "backend": "cli",
  "severity": "medium"
}
```

全フィールド省略可。CLI フラグが常に優先されます。

## ライセンス

MIT
