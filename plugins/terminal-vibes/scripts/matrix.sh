#!/bin/bash
# Mini Matrix rain effect (runs for a few seconds)

COLS=$(tput cols 2>/dev/null || echo 80)
LINES_COUNT=$(tput lines 2>/dev/null || echo 24)
case "$COLS" in
    ''|*[!0-9]*) COLS=80 ;;
esac
case "$LINES_COUNT" in
    ''|*[!0-9]*) LINES_COUNT=24 ;;
esac
if [ "$COLS" -lt 1 ]; then COLS=1; fi
if [ "$LINES_COUNT" -lt 1 ]; then LINES_COUNT=1; fi
DURATION=4
CHARS="abcdefghijklmnopqrstuvwxyz0123456789@#$%&*ﾊﾐﾋｰｳｼﾅﾓﾆｻﾜﾂｵﾘｱﾎﾃﾏｹﾒｴｶｷﾑﾕﾗｾﾈｽﾀﾇﾍ"
CHARS_LEN=${#CHARS}

printf '\033[?25l'  # hide cursor
printf '\033[2J'    # clear screen

END=$((SECONDS + DURATION))

while [ $SECONDS -lt $END ]; do
    COL=$((RANDOM % COLS + 1))
    CHAR="${CHARS:$((RANDOM % CHARS_LEN)):1}"
    ROW=$((RANDOM % LINES_COUNT + 1))
    BRIGHTNESS=$((RANDOM % 3))

    case $BRIGHTNESS in
        0) COLOR="\033[38;5;22m" ;;   # dark green
        1) COLOR="\033[38;5;28m" ;;   # medium green
        2) COLOR="\033[38;5;46m" ;;   # bright green
    esac

    printf "\033[%d;%dH${COLOR}%s\033[0m" "$ROW" "$COL" "$CHAR"

    # Occasionally print a bright white character
    if [ $((RANDOM % 5)) -eq 0 ]; then
        COL2=$((RANDOM % COLS + 1))
        ROW2=$((RANDOM % LINES_COUNT + 1))
        CHAR2="${CHARS:$((RANDOM % CHARS_LEN)):1}"
        printf "\033[%d;%dH\033[1;37m%s\033[0m" "$ROW2" "$COL2" "$CHAR2"
    fi
done

printf '\033[?25h'  # show cursor
printf '\033[2J'    # clear screen
printf '\033[H'     # home
