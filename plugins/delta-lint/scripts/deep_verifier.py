"""
Deep verification layer for delta-lint deep scan (Phase 2).

Takes candidate mismatches from contract_graph (Phase 1) and
verifies each one with a small LLM call (~2-5KB context).

Uses claude -p (subscription CLI, $0 cost) with parallel execution.
"""

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from contract_graph import ContractMismatch, enrich_snippets


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

MAX_PROMPT_CHARS = 5000
DEFAULT_WORKERS = 4
VERIFY_TIMEOUT = 60


# ---------------------------------------------------------------------------
# パターンマッピング（既存の6パターンに対応）
# ---------------------------------------------------------------------------

MISMATCH_TO_PATTERN = {
    "hook_arg_mismatch": "②",
    "filter_arg_mismatch": "②",
    "orphan_hook_fired": "⑤",
    "orphan_hook_listener": "④",
    "constant_conflict": "①",
    "missing_parent_class": "③",
}


# ---------------------------------------------------------------------------
# プロンプト構築
# ---------------------------------------------------------------------------

VERIFY_SYSTEM = """\
You are verifying a potential structural contradiction in source code.
Your job: determine if this is a REAL contradiction or intentional/safe behavior.

Rules:
- "contradiction" = the mismatch WILL cause incorrect behavior at runtime
- "intentional" = the code is designed this way on purpose (extension points, etc.)
- "uncertain" = cannot determine without more context

Respond ONLY with JSON (no markdown):
{"verdict": "contradiction"|"intentional"|"uncertain",
 "severity": "high"|"medium"|"low",
 "explanation": "1-2 sentence explanation",
 "user_impact": "What happens to the end user if this is a real bug"}
"""


def _build_verify_prompt(candidate: ContractMismatch) -> str:
    """検証プロンプトを構築する"""
    parts = [
        f"## Candidate: {candidate.mismatch_type}",
        f"Symbol: {candidate.symbol_name}",
        f"Description: {candidate.description}",
        "",
    ]

    if candidate.snippet_a:
        parts.append(f"## File A: {candidate.provider.file_path}:{candidate.provider.line}")
        parts.append("```")
        parts.append(candidate.snippet_a)
        parts.append("```")
        parts.append("")

    if candidate.snippet_b and candidate.consumer:
        parts.append(f"## File B: {candidate.consumer.file_path}:{candidate.consumer.line}")
        parts.append("```")
        parts.append(candidate.snippet_b)
        parts.append("```")
        parts.append("")

    parts.append("Is this a real structural contradiction, or intentional/safe?")

    prompt = "\n".join(parts)

    # プロンプトサイズ制限
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n... (truncated)"

    return prompt


# ---------------------------------------------------------------------------
# LLM 呼び出し
# ---------------------------------------------------------------------------

def _call_claude_cli(system: str, user: str) -> Optional[str]:
    """claude -p で LLM を呼び出す"""
    prompt = system + "\n\n" + user
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=VERIFY_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _parse_verdict(raw: str) -> Optional[dict]:
    """LLM レスポンスから JSON を抽出する"""
    if not raw:
        return None

    text = raw.strip()

    # markdown コードブロックの除去
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "verdict" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    # JSON 部分を探す
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            parsed = json.loads(text[brace_start:brace_end + 1])
            if isinstance(parsed, dict) and "verdict" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# 候補 → finding 変換
# ---------------------------------------------------------------------------

MISMATCH_TO_CATEGORY = {
    "hook_arg_mismatch": "deep:hook",
    "filter_arg_mismatch": "deep:hook",
    "orphan_hook_fired": "deep:hook",
    "orphan_hook_listener": "deep:hook",
    "constant_conflict": "deep:constant",
    "missing_parent_class": "deep:class",
}


def _to_finding_dict(candidate: ContractMismatch, verdict: dict) -> dict:
    """検証結果を既存の finding dict フォーマットに変換する"""
    pattern = MISMATCH_TO_PATTERN.get(candidate.mismatch_type, "②")
    category = MISMATCH_TO_CATEGORY.get(candidate.mismatch_type, "deep")
    consumer_file = ""
    consumer_detail = ""
    if candidate.consumer:
        consumer_file = candidate.consumer.file_path
        consumer_detail = f"line ~{candidate.consumer.line}"

    # Determine certainty from verdict
    v = verdict.get("verdict", "uncertain")
    certainty = {"contradiction": "definite", "uncertain": "uncertain"}.get(v, "probable")

    return {
        "pattern": pattern,
        "category": category,
        "severity": verdict.get("severity", candidate.severity_hint),
        "taxonomies": {
            "category": category,
            "certainty": certainty,
            "source": "deep_scan",
            "mismatch_type": candidate.mismatch_type,
        },
        "location": {
            "file_a": candidate.provider.file_path,
            "detail_a": f"line ~{candidate.provider.line}",
            "file_b": consumer_file,
            "detail_b": consumer_detail,
        },
        "contradiction": verdict.get("explanation", candidate.description),
        "impact": verdict.get("user_impact", ""),
        "user_impact": verdict.get("user_impact", ""),
        "internal_evidence": f"Deep scan: {candidate.mismatch_type} on {candidate.symbol_name}",
        "_source": "deep_scan",
        "_mismatch_type": candidate.mismatch_type,
    }


# ---------------------------------------------------------------------------
# 単一候補の検証
# ---------------------------------------------------------------------------

def verify_candidate(candidate: ContractMismatch) -> Optional[dict]:
    """1つの候補を LLM で検証する。

    Returns:
        finding dict（contradiction の場合）or None（intentional/uncertain）
    """
    prompt = _build_verify_prompt(candidate)
    raw = _call_claude_cli(VERIFY_SYSTEM, prompt)
    verdict = _parse_verdict(raw)

    if verdict is None:
        return None

    if verdict.get("verdict") == "intentional":
        return None

    # uncertain は severity を下げて返す
    if verdict.get("verdict") == "uncertain":
        verdict["severity"] = "low"

    return _to_finding_dict(candidate, verdict)


# ---------------------------------------------------------------------------
# メイン API
# ---------------------------------------------------------------------------

def verify_all(candidates: list[ContractMismatch],
               repo_path: str,
               max_workers: int = DEFAULT_WORKERS,
               verbose: bool = False) -> list[dict]:
    """全候補を並列で LLM 検証する。

    Args:
        candidates: Phase 1 の候補リスト
        repo_path: リポジトリルート
        max_workers: 並列ワーカー数
        verbose: 詳細表示

    Returns:
        検証済み finding dict のリスト
    """
    if not candidates:
        return []

    # Phase 1.5: スニペット拡張（前後10行に広げる）
    candidates = enrich_snippets(candidates, repo_path, radius=10)

    # LLM 不要で明確に判定できるものを先にフィルタ
    needs_llm = []
    auto_findings = []

    for c in candidates:
        # 定数衝突は LLM 不要で確定
        if c.mismatch_type == "constant_conflict":
            auto_findings.append(_to_finding_dict(c, {
                "verdict": "contradiction",
                "severity": "high",
                "explanation": c.description,
                "user_impact": "同じ定数が異なる値で定義されており、ロード順によって動作が変わる",
            }))
        # orphan_hook_fired は低優先度、LLM 不要
        elif c.mismatch_type == "orphan_hook_fired":
            continue  # スキップ（extension point の可能性が高い）
        # missing_parent_class で vendor 由来の可能性が高いものはスキップ
        elif c.mismatch_type == "missing_parent_class":
            desc = c.description
            if "vendor" in desc or "外部ライブラリ" in desc:
                continue
            needs_llm.append(c)
        else:
            needs_llm.append(c)

    if verbose:
        print(f"  [deep] Phase 2: {len(auto_findings)} auto-confirmed, "
              f"{len(needs_llm)} need LLM verification "
              f"(workers={max_workers})", file=sys.stderr)

    # 並列 LLM 検証
    llm_findings = []
    if needs_llm:
        confirmed = 0
        rejected = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(verify_candidate, c): c
                for c in needs_llm
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        llm_findings.append(result)
                        confirmed += 1
                        if verbose:
                            print(f"    [+] {candidate.mismatch_type}: "
                                  f"{candidate.symbol_name} — confirmed",
                                  file=sys.stderr)
                    else:
                        rejected += 1
                        if verbose:
                            print(f"    [-] {candidate.mismatch_type}: "
                                  f"{candidate.symbol_name} — rejected",
                                  file=sys.stderr)
                except Exception as e:
                    rejected += 1
                    if verbose:
                        print(f"    [!] {candidate.mismatch_type}: "
                              f"{candidate.symbol_name} — error: {e}",
                              file=sys.stderr)

        if verbose:
            print(f"  [deep] Phase 2 complete: "
                  f"{confirmed} confirmed, {rejected} rejected",
                  file=sys.stderr)

    return auto_findings + llm_findings
