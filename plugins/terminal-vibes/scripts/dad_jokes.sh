#!/bin/bash
# Programmer dad jokes raining from the sky (JP + EN mix)

declare -a JOKES
JOKES[0]="プログラマーはなぜダークモードが好き？ ライト（光/軽い）はバグを引き寄せるから。"
JOKES[1]="SQLクエリがバーに入って、2つのテーブルを見て言った...「JOINしていい？」"
JOKES[2]="JavaScriptの開発者はなぜ悲しい？ Nodeで自分をExpressできなかったから。"
JOKES[3]="プログラマーの行きつけは？ Foo Bar。"
JOKES[4]="Java開発者はなぜメガネをかける？ C#（シーシャープ/見えない）だから。"
JOKES[5]="電球を替えるのにプログラマーは何人必要？ ゼロ。それはハードウェアの問題。"
JOKES[6]="!false ... 面白いよね、だって true（本当）だから。"
JOKES[7]="世界には10種類の人間がいる。2進数がわかる人とわからない人。"
JOKES[8]="開発者はなぜ破産した？ cache（キャッシュ/現金）を使い果たしたから。"
JOKES[9]="3.14メートルのヘビを何と呼ぶ？ パイソン（Pi-thon）。"
JOKES[10]="git commit -m 'バグ修正' ... ナレーション：彼はバグを修正していなかった。"
JOKES[11]="関数はなぜ議論に勝つ？ 有効な return（返り値/返答）があるから。"
JOKES[12]="ノックノック。レースコンディション。「誰？」"
JOKES[13]="幽霊の好きなデータ型は？ ブーリアン（Boo-lean）。"
JOKES[14]="プログラマーは寝る前に2つのグラス置く。水入り1つと、喉が渇かない場合のために空1つ。"

COLS=$(tput cols 2>/dev/null || echo 80)
case "$COLS" in
  ''|*[!0-9]*) COLS=80 ;;
esac
if [ "$COLS" -lt 4 ]; then
  COLS=4
fi
MAX_INDENT=$((COLS / 4))

echo ""
printf "\033[1;33m  === ダジャレの雨 ===\033[0m\n"
echo ""

for _ in 1 2 3 4 5; do
  IDX=$((RANDOM % ${#JOKES[@]}))
  JOKE="${JOKES[$IDX]}"

  INDENT=$((RANDOM % MAX_INDENT))
  SPACES=$(printf '%*s' "$INDENT" '')

  COLOR_NUM=$((31 + (RANDOM % 6)))

  printf "%s\033[%dm>> %s\033[0m\n\n" "$SPACES" "$COLOR_NUM" "$JOKE"
  sleep 0.3
done

printf "\033[2m  ...ﾊﾞﾀﾞﾑ ﾂｯ！\033[0m\n\n"
