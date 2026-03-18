"""
Persona translation layer for delta-lint.

Translates technical findings into persona-appropriate language:
- engineer (default): file paths, method names, line numbers — current behavior
- pm: user-facing impact, spec gaps, business decisions needed
- qa: test scenarios, reproduction steps, environment conditions

Uses `claude -p` (subscription CLI, $0 cost) for translation.
Falls back to template-based translation if CLI unavailable.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

PERSONAS = ("engineer", "pm", "qa")

# ---------------------------------------------------------------------------
# Prompt templates per persona
# ---------------------------------------------------------------------------

_PM_SYSTEM = """\
あなたはソフトウェア品質リスクを非技術者に説明するアドバイザーです。
以下の技術的な構造矛盾を、PM・事業責任者・品質管理チームが理解できる言葉で翻訳してください。

ルール:
- ファイル名・メソッド名・行番号は絶対に使わない
- 「ユーザーが○○するとき」「○○という機能で」のように操作ベースで説明
- 影響の大きさを 🔴高 / 🟡中 / 🟢低 で示す
- 仕様として未確定な点があれば「📋 判断が必要な点」として箇条書き
- 技術用語を使う場合は必ず（）で平易な説明を添える
"""

_QA_SYSTEM = """\
あなたはQAエンジニアのアシスタントです。
以下の技術的な構造矛盾を、コードを読めないQA担当者でも実行可能なテストシナリオに変換してください。

ルール:
- 手順は番号付きステップで書く
- 「○○画面を開く」「○○ボタンを押す」のようにUI操作ベースで書く
- 期待結果（正常）と実際の結果（予想される不具合）を明記
- 環境条件（タイムゾーン・権限・データ量・ブラウザ等）があれば指定
- 再現確率が条件依存の場合、その条件を明記
"""

_FINDING_TEMPLATE = """\
Finding #{index}:
- パターン: {pattern}
- 重要度: {severity}
- 矛盾: {contradiction}
- 影響: {impact}
- ユーザー影響: {user_impact}
- トリアージ: {triage}
"""


def _build_finding_text(findings: list[dict]) -> str:
    """Convert findings list to text for LLM prompt."""
    parts = []
    for i, f in enumerate(findings, 1):
        parts.append(_FINDING_TEMPLATE.format(
            index=i,
            pattern=f.get("pattern", "?"),
            severity=f.get("severity", "?"),
            contradiction=f.get("contradiction", "N/A"),
            impact=f.get("impact", "N/A"),
            user_impact=f.get("user_impact", "N/A"),
            triage=f.get("_triage_label", "N/A"),
        ))
    return "\n".join(parts)


def _call_claude_cli(system_prompt: str, user_prompt: str,
                     model: str = "claude-sonnet-4-20250514") -> Optional[str]:
    """Call claude -p for $0 translation. Returns None if unavailable."""
    if not shutil.which("claude"):
        return None

    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--model", model],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _fallback_pm(findings: list[dict]) -> str:
    """Template-based PM translation when CLI is unavailable."""
    lines = ["# δ-lint スキャン結果（PM向け）\n"]
    for i, f in enumerate(findings, 1):
        severity = f.get("severity", "medium")
        icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(severity, "🟡")
        lines.append(f"## {icon} 問題 #{i}")
        lines.append("")
        if f.get("user_impact"):
            lines.append(f"**ユーザーへの影響:** {f['user_impact']}")
        elif f.get("impact"):
            lines.append(f"**影響:** {f['impact']}")
        lines.append("")
    return "\n".join(lines)


def _fallback_qa(findings: list[dict]) -> str:
    """Template-based QA translation when CLI is unavailable."""
    lines = ["# δ-lint スキャン結果（QA向けテストシナリオ）\n"]
    for i, f in enumerate(findings, 1):
        lines.append(f"## テストシナリオ #{i}")
        lines.append("")
        if f.get("user_impact"):
            lines.append(f"**確認すべき挙動:** {f['user_impact']}")
        elif f.get("impact"):
            lines.append(f"**確認すべき挙動:** {f['impact']}")
        lines.append(f"**重要度:** {f.get('severity', '?')}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config: default persona persistence
# ---------------------------------------------------------------------------

def load_default_persona(repo_path: str = ".") -> str:
    """Load default persona from .delta-lint/config.json."""
    config_path = Path(repo_path) / ".delta-lint" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            persona = config.get("persona", "engineer")
            if persona in PERSONAS:
                return persona
        except (json.JSONDecodeError, OSError):
            pass
    return "engineer"


def save_default_persona(persona: str, repo_path: str = ".") -> Path:
    """Save default persona to .delta-lint/config.json (merge with existing)."""
    config_dir = Path(repo_path) / ".delta-lint"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    config["persona"] = persona
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# Main translate function
# ---------------------------------------------------------------------------

def translate(findings: list[dict], persona: str = "engineer",
              model: str = "claude-sonnet-4-20250514",
              verbose: bool = False) -> str:
    """Translate findings for the given persona.

    Args:
        findings: List of finding dicts from detector/output
        persona: "engineer", "pm", or "qa"
        model: Model for claude -p call
        verbose: Print progress to stderr

    Returns:
        Translated output as markdown string
    """
    if persona == "engineer":
        return ""  # No translation needed; use existing output

    if not findings:
        return "検出された構造矛盾はありません。\n"

    finding_text = _build_finding_text(findings)

    if persona == "pm":
        system_prompt = _PM_SYSTEM
        user_prompt = (
            f"以下の {len(findings)} 件の構造矛盾を PM・事業責任者向けに翻訳してください。\n\n"
            f"{finding_text}"
        )
        fallback_fn = _fallback_pm
    elif persona == "qa":
        system_prompt = _QA_SYSTEM
        user_prompt = (
            f"以下の {len(findings)} 件の構造矛盾を、テストシナリオに変換してください。\n\n"
            f"{finding_text}"
        )
        fallback_fn = _fallback_qa
    else:
        print(f"Unknown persona: {persona}", file=sys.stderr)
        return ""

    if verbose:
        print(f"  Translating findings for persona: {persona} ...", file=sys.stderr)

    result = _call_claude_cli(system_prompt, user_prompt, model=model)
    if result:
        return result

    if verbose:
        print("  claude CLI unavailable, using template fallback", file=sys.stderr)
    return fallback_fn(findings)
