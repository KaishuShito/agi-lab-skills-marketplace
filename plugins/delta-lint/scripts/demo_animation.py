#!/usr/bin/env python3
"""DeltaLint demo animation — simulates /delta-scan in Claude Code UI."""

import sys
import time

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
WHITE = "\033[37m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
ORANGE = "\033[38;2;217;119;87m"  # Anthropic Hazel #D97757
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_LINE = "\033[2K"
UP = lambda n: f"\033[{n}A"
MOVE = lambda r, c: f"\033[{r};{c}H"

TITLE_LINES = [
    r"  ____       _ _        _     _       _   ",
    r" |  _ \  ___| | |_ __ _| |   (_)_ __ | |_ ",
    r" | | | |/ _ \ | __/ _` | |   | | '_ \| __|",
    r" | |_| |  __/ | || (_| | |___| | | | | |_ ",
    r" |____/ \___|_|\__\__,_|_____|_|_| |_|\__|",
]


def write(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def sleep(s):
    time.sleep(s)


def draw_progress(pct, width=30):
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def bullet(color=GREEN):
    return f"{color}●{RESET}"


def run():
    write(HIDE_CURSOR)

    # == Claude Code startup banner ==
    write(f"\n")
    write(f"  {ORANGE}▐▛███▜▌{RESET}   {WHITE}{BOLD}Claude Code{RESET} {DIM}v2.1.79{RESET}\n")
    write(f" {ORANGE}▝▜█████▛▘{RESET}  {CYAN}Opus 4.6{RESET} {DIM}(1M context){RESET} {DIM}· Claude Max{RESET}\n")
    write(f"  {ORANGE} ▘▘ ▝▝{RESET}    {DIM}~/Project/my-app{RESET}\n")
    write(f"\n")
    sleep(1.0)

    # == User types /delta-scan ==
    write(f"  {MAGENTA}{BOLD}>{RESET} ")
    sleep(0.8)

    # == Skill header ==
    write(f"\n\n")
    write(f"  {WHITE}{BOLD}❯ /delta-scan{RESET}\n\n")
    sleep(0.3)

    # == Read files ==
    write(f"    {DIM}Read 2 files (ctrl+o to expand){RESET}\n\n")
    sleep(0.3)

    # == Init message ==
    write(f"  {bullet()} {DIM}.delta-lint/ が存在しないため、初回初期化から始めます。{RESET}\n")
    sleep(0.4)

    # Clear lines for logo area and add spacing
    write(f"\n\n\n\n\n\n\n")

    # == ASCII logo wipe-in ==
    logo_row = 14
    max_len = max(len(l) for l in TITLE_LINES)
    wipe_steps = 12
    for step in range(1, wipe_steps + 1):
        cols_visible = int(max_len * step / wipe_steps)
        for i, line in enumerate(TITLE_LINES):
            write(MOVE(logo_row + i, 3))
            visible = line[:cols_visible]
            write(f"{CYAN}{BOLD}{visible}{RESET}")
        sleep(0.03)
    sleep(0.6)

    # Move below logo
    write(MOVE(logo_row + len(TITLE_LINES), 1))
    write(f"    {DIM}Structural contradiction detector  v0.3.0{RESET}\n\n")
    sleep(0.4)

    # == δ-lint init ==
    write(f"  {bullet(CYAN)} — {CYAN}δ-lint{RESET} — 初期化開始\n")
    write(f"    ストレステストを開始します...\n\n")
    sleep(0.4)

    # == Bash tool call ==
    write(f"  {bullet()} {DIM}Bash{RESET}({DIM}python stress_test.py --repo ~/Project/my-app --parallel·{RESET})\n")
    write(f"    {DIM}└ Running in the background (↓ to manage){RESET}\n\n")
    sleep(0.5)

    # == Structure results ==
    write(f"  {bullet()} {DIM}Bash{RESET}({DIM}check structure.json{RESET})\n")
    write(f"      {WHITE}modules: 12{RESET}\n")
    write(f"      {WHITE}hotspots: 5{RESET}\n")
    write(f"      {DIM}  src/core/ — Central library with 418 commits{RESET}\n")
    write(f"      {DIM}…+9 lines (ctrl+o to expand){RESET}\n")
    write(f"    {DIM}└ (timeout 2m){RESET}\n\n")
    sleep(0.5)

    # == Repository overview ==
    write(f"  {bullet(CYAN)} — {CYAN}δ-lint{RESET} — 初期化中...\n\n")
    sleep(0.3)
    write(f"  📊 {WHITE}{BOLD}リポジトリ概要：{RESET}\n")
    write(f"    12 モジュール、5 ホットスポット\n\n")
    sleep(0.3)

    # == High risk files ==
    write(f"  🔥 {WHITE}{BOLD}変更リスクが高いファイル：{RESET}\n")
    risk_files = [
        ("src/core/engine.ts", "Central module with 418 commits, all subsystems depend on it"),
        ("src/api/handler.ts", "125 commits, deep middleware chain with implicit contracts"),
        ("tests/config/baseTest.ts", "Foundation fixture — merge order creates precedence issues"),
    ]
    for idx, (path, desc) in enumerate(risk_files, 1):
        write(f"  {idx}. {WHITE}{path}{RESET} — {DIM}{desc}{RESET}\n")
        sleep(0.2)
    write(f"\n")
    sleep(0.3)

    # == Scanning progress ==
    write(f"  🔨 {WHITE}{BOLD}ストレステスト実行中（10並列）{RESET}\n")
    write(f"    {DIM}矛盾が見つかり次第、随時報告します。{RESET}\n\n")
    sleep(0.4)

    # == Progress bar ==
    FILE_PAIRS = [
        ("auth/login.ts", "session/manager.ts"),
        ("payment/charge.ts", "billing/invoice.ts"),
        ("api/handler.ts", "middleware/validator.ts"),
        ("config/defaults.ts", "loader/env.ts"),
        ("users/profile.ts", "permissions/roles.ts"),
        ("webhook/handler.ts", "auth/token.ts"),
    ]
    write(f"  {WHITE}Scanning...{RESET}\n")
    write("\n")
    write("\n")

    steps = 20
    for i in range(steps + 1):
        pct = int(i * 100 / steps)
        pair = FILE_PAIRS[i % len(FILE_PAIRS)]
        bar = draw_progress(pct)

        write(f"{UP(2)}{CLEAR_LINE}")
        write(f"    {CYAN}{bar}{RESET}  {WHITE}{pct:3d}%{RESET}\n")
        write(f"{CLEAR_LINE}")
        write(f"      {DIM}{pair[0]} ↔ {pair[1]}{RESET}\n")
        sleep(0.06)

    write(f"{UP(2)}{CLEAR_LINE}")
    write(f"    {GREEN}{draw_progress(100)}{RESET}  {GREEN}{BOLD}done{RESET}\n")
    write(f"{CLEAR_LINE}\n")
    sleep(0.3)

    # == Findings ==
    write(f"  {RED}{BOLD}⚡ 3 structural contradictions found{RESET}\n\n")
    sleep(0.3)

    # == Report table ==
    H = f"{WHITE}{BOLD}"
    write(f"  {H}┌──────┬────────────────────────┬──────────┬───────┐{RESET}\n")
    write(f"  {H}│{RESET}  ID  {H}│{RESET} Pattern                {H}│{RESET} Severity {H}│{RESET} Score {H}│{RESET}\n")
    write(f"  {H}├──────┼────────────────────────┼──────────┼───────┤{RESET}\n")
    sleep(0.15)

    rows = [
        ("F001", "Asymmetric Defaults   ", f"{RED}{BOLD}HIGH{RESET}  ", f"{RED}8.4{RESET}"),
        ("F002", "Guard Non-Propagation ", f"{YELLOW}{BOLD}MEDIUM{RESET}", f"{YELLOW}6.1{RESET}"),
        ("F003", "Lifecycle Ordering    ", f"{RED}{BOLD}HIGH{RESET}  ", f"{RED}7.8{RESET}"),
    ]
    for fid, pat, sev, score in rows:
        write(f"  {H}│{RESET} {YELLOW}{fid}{RESET}  {H}│{RESET} {WHITE}{pat}{RESET} {H}│{RESET} {sev}   {H}│{RESET} {score}  {H}│{RESET}\n")
        sleep(0.2)
    write(f"  {H}└──────┴────────────────────────┴──────────┴───────┘{RESET}\n\n")
    sleep(0.3)

    # == Detail cards ==
    details = [
        ("F001", "Asymmetric Defaults", "HIGH",
         "auth/login.ts:42", "session/manager.ts:18",
         'user_id or "" vs not user_id — falsy check diverges'),
        ("F002", "Guard Non-Propagation", "MEDIUM",
         "api/handler.ts:87", "middleware/validator.ts:31",
         "null guard present in handler but missing in validator path"),
        ("F003", "Lifecycle Ordering", "HIGH",
         "payment/charge.ts:55", "billing/invoice.ts:23",
         "invoice created before charge confirmation — race condition"),
    ]

    for fid, pat, sev, fa, fb, summary in details:
        sev_c = f"{RED}{BOLD}" if sev == "HIGH" else f"{YELLOW}{BOLD}"
        write(f"  {CYAN}{'─' * 55}{RESET}\n")
        write(f"  {YELLOW}{BOLD}{fid}{RESET}  {WHITE}{BOLD}{pat}{RESET}  [{sev_c}{sev}{RESET}]\n")
        write(f"    {DIM}Location:{RESET} {fa} ↔ {fb}\n")
        write(f"    {DIM}Summary:{RESET}  {summary}\n\n")
        sleep(0.4)

    # == Overall ==
    write(f"  {CYAN}{'─' * 55}{RESET}\n")
    write(f"  {WHITE}{BOLD}Debt Score:{RESET}  {RED}{BOLD}7.2{RESET}{DIM} / 10{RESET}\n")
    write(f"  {WHITE}{BOLD}Risk Level:{RESET}  {RED}{BOLD}■■■■{RESET}{DIM}■■■■■■{RESET}  {DIM}Action recommended{RESET}\n\n")
    sleep(0.3)

    write(f"  {GREEN}✓{RESET} Report saved → {DIM}.delta-lint/findings.json{RESET}\n")
    write(f"  {GREEN}✓{RESET} Dashboard   → {DIM}.delta-lint/findings.html{RESET}\n\n")

    write(SHOW_CURSOR)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        write(SHOW_CURSOR + "\n")
