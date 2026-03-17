#!/usr/bin/env python3
"""delta-lint init startup animation — 3 seconds."""

import sys
import time
import random

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
WHITE = "\033[37m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_LINE = "\033[2K"
MOVE = lambda r, c: f"\033[{r};{c}H"
UP = lambda n: f"\033[{n}A"
CLEAR = "\033[2J\033[H"

TITLE_LINES = [
    r"   ____       _ _              _ _       _   ",
    r"  |  _ \  ___| | |_ __ _      | (_)_ __ | |_ ",
    r"  | | | |/ _ \ | __/ _` |_____| | | '_ \| __|",
    r"  | |_| |  __/ | || (_| |_____| | | | | | |_ ",
    r"  |____/ \___|_|\__\__,_|     |_|_|_| |_|\__|",
]

SUBTITLE = "\u30c7\u30b0\u30ec\u7279\u5316\u578bAI"

FILE_PAIRS = [
    ("auth/login.ts", "session/manager.ts"),
    ("payment/charge.ts", "billing/invoice.ts"),
    ("api/handler.ts", "middleware/validator.ts"),
    ("config/defaults.ts", "loader/env.ts"),
    ("email/sender.ts", "auth/verify.ts"),
    ("orders/create.ts", "payment/refund.ts"),
    ("users/profile.ts", "permissions/roles.ts"),
    ("webhook/handler.ts", "auth/token.ts"),
    ("cache/store.ts", "session/ttl.ts"),
    ("jobs/scheduler.ts", "queue/worker.ts"),
]


def write(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def sleep(s):
    time.sleep(s)


def draw_progress(pct, width=30):
    filled = int(width * pct / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)


def run_animation():
    write(HIDE_CURSOR + CLEAR)

    # Layout: center vertically
    R = 4  # start row for logo block

    # -- Phase 1: Logo slides in (0.6s) --

    # Subtitle types in
    write(MOVE(R, 3))
    for ch in SUBTITLE:
        write(f"{WHITE}{BOLD}{ch}{RESET}")
        sleep(0.03)
    sleep(0.1)

    # AA lines wipe in left-to-right
    max_len = max(len(l) for l in TITLE_LINES)
    wipe_steps = 12
    for step in range(1, wipe_steps + 1):
        cols_visible = int(max_len * step / wipe_steps)
        for i, line in enumerate(TITLE_LINES):
            write(MOVE(R + 2 + i, 1))
            visible = line[:cols_visible]
            write(f"{CYAN}{BOLD}{visible}{RESET}")
        sleep(0.03)

    sleep(0.6)

    # -- Phase 2: Logo fades out (0.4s) --

    # Dim
    write(MOVE(R, 3))
    write(f"{DIM}{WHITE}{SUBTITLE}{RESET}")
    for i, line in enumerate(TITLE_LINES):
        write(f"{MOVE(R + 2 + i, 1)}{DIM}{CYAN}{line}{RESET}")
    sleep(0.15)

    # Clear line by line from top
    for i in range(len(TITLE_LINES) + 3):
        write(f"{MOVE(R + i, 1)}{CLEAR_LINE}")
        sleep(0.03)

    # -- Phase 3: Scanning progress (1.8s) --

    scan_row = R
    write(f"{MOVE(scan_row, 3)}{WHITE}Scanning for contradictions...{RESET}\n\n")
    write("\n")  # progress bar
    write("\n")  # file pair

    steps = 20
    duration = 1.8
    interval = duration / steps

    for i in range(steps + 1):
        pct = int(i * 100 / steps)
        pair = FILE_PAIRS[i % len(FILE_PAIRS)]
        bar = draw_progress(pct)

        write(f"{UP(2)}{CLEAR_LINE}")
        write(f"  {CYAN}{bar}{RESET}  {WHITE}{pct:3d}%{RESET}\n")
        write(f"{CLEAR_LINE}")
        write(f"    {DIM}{pair[0]} \u2194 {pair[1]}{RESET}\n")

        sleep(interval)

    sleep(0.1)

    # -- Phase 4: Result (0.5s) --
    write(f"{UP(2)}{CLEAR_LINE}")
    write(f"  {GREEN}{draw_progress(100)}{RESET}  {GREEN}{BOLD}done{RESET}\n")
    write(f"{CLEAR_LINE}\n")

    findings = random.randint(3, 7)
    write(f"  {BOLD}{RED}\u26a1 {findings} contradictions found{RESET}\n")
    sleep(0.15)
    write(f"  {DIM}Analyzing details...{RESET}\n\n")

    write(SHOW_CURSOR)


if __name__ == "__main__":
    try:
        run_animation()
    except KeyboardInterrupt:
        write(SHOW_CURSOR + "\n")
