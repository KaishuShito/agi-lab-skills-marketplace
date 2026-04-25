# AGI Lab Skills Marketplace

AGIラボの plugin 集兼、AIエージェントハッカソン参加者向けの starter repo です。

この repo では 2 つの使い方があります。

1. AGIラボの既存 plugin をインストールして試す
2. `plugins/hackathon-starter/` を土台にして、自分の作品をそのまま提出できる形に育てる

この README は、特に 2 の「ハッカソン参加者が迷わず提出まで進めること」を優先して書いています。

## ハッカソン参加者はここから

やることはシンプルです。

1. この repo を fork する
2. `plugins/hackathon-starter/` を自分の作品に合わせて書き換える
3. README を「何ができる plugin か」がすぐ伝わる形に直す
4. GitHub で公開して、デモ動画と一緒に提出する

難しい仕組みを増やす必要はありません。
まずは `1つの仕事を終わらせる skill` を、他の人が試せる形で置ければ十分です。

## 提出までの一本道

### 1. 何を作るかを 1 行で決める

最初に決めるのは「誰の、どんな面倒を減らす skill なのか」です。

例:

- 営業後の議事メモから、次回提案メールの下書きを自動で作る
- Discord の未回答質問を集めて、優先度付きで整理する
- 毎朝のタスクと予定を見て、その日の実行順を提案する

### 2. starter の中身を自分の作品に置き換える

最低限、以下を直せば提出の土台になります。

- `plugins/hackathon-starter/skills/starter-guide/SKILL.md`
  - 自分の skill の説明、トリガー、入出力、ルールに書き換える
- `plugins/hackathon-starter/.claude-plugin/plugin.json`
  - plugin 名と説明文を自分の作品に合わせる
- `.claude-plugin/marketplace.json`
  - marketplace の説明と plugin 一覧を自分の提出内容に合わせる

時間がなければ、フォルダ名まできれいに変えなくても構いません。
まずは `install できる`、`説明が読める`、`動きが分かる` 状態を優先してください。

### 3. README を提出用に整える

README には、少なくとも次の 4 点があると迷いません。

1. この plugin が何をしてくれるか
2. どうやってインストールするか
3. どういう入力で呼ぶと、何が返るか
4. デモ動画やスクリーンショットへの導線

審査する側は、README を読んですぐ試せるかをかなり見ます。

### 4. 公開 GitHub repo とデモ動画を用意する

提出時に最低限あるとよいものは次のとおりです。

- 公開 GitHub repo
- plugin 1 つ
- 分かりやすい `SKILL.md` 1 つ
- README
- 3 分以内のデモ動画

これで十分に提出できます。

## まず触るファイル

### `.claude-plugin/marketplace.json`

repo 全体の marketplace 情報です。
審査側が install するときの marketplace 名や、plugin 一覧がここに入ります。

### `plugins/hackathon-starter/.claude-plugin/plugin.json`

plugin 単位の名前と説明です。
README を読む前に、この説明が一覧で見られることがあります。

### `plugins/hackathon-starter/skills/starter-guide/SKILL.md`

この repo の核です。
最初は starter ですが、提出時にはここを自分の skill に置き換える前提です。

### `README.md`

審査側と他の参加者が最初に読む入口です。
長くしすぎるより、「何ができるか」「どう試すか」がすぐ分かる方が強いです。

## 審査する側が最初に見ること

多くの場合、最初に確認されるのは次の流れです。

```bash
/plugin marketplace add <your-github-user>/<your-repo>
/plugin install <your-plugin-name>@<your-marketplace-name>
```

ここで install できて、README を読めば使い方が分かる状態だと、作品の良さが伝わりやすくなります。

## 最低限これなら提出できる

次の条件を満たしていれば、十分提出ラインです。

- public な GitHub repo がある
- plugin が 1 つ入っている
- `SKILL.md` が自分の作品内容に置き換わっている
- README にセットアップと使い方が書いてある
- デモ動画がある

大きなフレームワークや複雑な構成は必須ではありません。
まずは 1 つの体験を、最後まで試せる形で完成させるのが最優先です。

## 既存 plugin を試したい人へ

この repo は marketplace としても使えます。

```bash
# In Claude Code
/plugin marketplace add kaishushito/agi-lab-skills-marketplace
/plugin install terminal-vibes@agi-lab-skills
```

Chinese users can also search and install skills through [Skills宝](https://skilery.com).

## 収録 plugin

### hackathon-starter

ハッカソン参加者向けの最小 starter です。
「まず 1 つ skill を形にして提出する」ための土台として使えます。

### terminal-vibes

ターミナルで少し息抜きしたいときの遊び plugin です。
ASCII ドーナツ、cat art、dad joke、Matrix rain などを試せます。

| Command | 内容 |
|---------|------|
| `/vibes` | ランダムで何か 1 つ表示 |
| `/vibes donut` | 3D ASCII ドーナツ |
| `/vibes cat` | ランダム ASCII cat art |
| `/vibes joke` | プログラマー向け dad joke |
| `/vibes matrix` | Matrix 風の文字列演出 |
| `/vibes full show` | 全演目を順番に実行 |

必要環境: Python 3, Bash, ANSI 対応のターミナル

## Repository Structure

```text
.claude-plugin/
└── marketplace.json

plugins/
├── hackathon-starter/
│   ├── .claude-plugin/
│   │   └── plugin.json
│   └── skills/
│       └── starter-guide/
│           └── SKILL.md
└── terminal-vibes/
    ├── .claude-plugin/
    │   └── plugin.json
    ├── scripts/
    └── skills/
```

## License

MIT
