#!/usr/bin/env python3
"""
GitLab MR AI Code Review 工具
用法:
  python3 mr_ai_review.py --mr-url "http://gitlab.xxx.com/group/repo/-/merge_requests/42"
  python3 mr_ai_review.py --project-id 12 --mr-iid 42
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from openai import OpenAI

# ──────────────────────────────────────────────
# 配置区（也可通过环境变量注入）
# ──────────────────────────────────────────────
GITLAB_URL      = os.getenv("GITLAB_URL",      "")   # GitLab 地址
GITLAB_TOKEN    = os.getenv("GITLAB_TOKEN",    "")   # Personal Access Token
DEEPSEEK_KEY    = os.getenv("DEEPSEEK_KEY",    "")
DEEPSEEK_BASE   = os.getenv("DEEPSEEK_BASE",   "https://api.deepseek.com")
DEEPSEEK_MODEL  = os.getenv("DEEPSEEK_MODEL",  "deepseek-v4-pro")

# diff 单次喂给 AI 的最大字符数（按需调整）
MAX_DIFF_CHARS  = 12000
MAX_FILE_LINES  = 300
REVIEWS_DIR     = Path(__file__).parent / "reviews"
MAX_HISTORY_REVIEWS = 5
MAX_HISTORY_CHARS   = 2500

# ──────────────────────────────────────────────
# 历史 Review 存储
# ──────────────────────────────────────────────

class ReviewStore:
    def __init__(self, project_key: str):
        safe_name = project_key.replace("/", "_").replace("%2F", "_")
        self.project_dir = REVIEWS_DIR / safe_name
        self.project_dir.mkdir(parents=True, exist_ok=True)

    def save(self, review_type: str, ref: str, info: dict,
             review: dict, changed_files: list[str]):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        record = {
            "type": review_type,
            "ref": ref,
            "title": info.get("title", ""),
            "project_id": ref,
            "timestamp": datetime.now().isoformat(),
            "info": info,
            "review": review,
            "changed_files": changed_files,
        }
        path = self.project_dir / f"review_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        return path

    def add_feedback(self, feedback: str):
        files = sorted(self.project_dir.glob("review_*.json"), reverse=True)
        if not files:
            return None
        with open(files[0], "r", encoding="utf-8") as f:
            record = json.load(f)
        record["feedback"] = feedback
        with open(files[0], "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        return files[0]

    def load_history(self, limit: int = MAX_HISTORY_REVIEWS) -> list[dict]:
        files = sorted(self.project_dir.glob("review_*.json"), reverse=True)[:limit]
        records = []
        for fp in files:
            with open(fp, "r", encoding="utf-8") as f:
                records.append(json.load(f))
        return records


def build_history_context(store: ReviewStore) -> str:
    records = store.load_history()
    if not records:
        return ""
    lines = ["## 📜 本项目历史 Review 记录", ""]
    for i, r in enumerate(records):
        idx = len(records) - i
        lines.append(f"### 历史 #{idx}: {r.get('title', '')}")
        ts = r.get("timestamp", "")[:10]
        review = r.get("review", {})
        lines.append(f"- 时间: {ts}  |  风险等级: {review.get('risk_level', 'unknown')}")
        lines.append(f"- 变更文件: {', '.join(r.get('changed_files', [])[:5])}")
        summary = review.get("summary", "")
        if summary:
            lines.append(f"- 摘要: {summary}")
        global_risks = review.get("global_risks", [])
        if global_risks:
            lines.append(f"- 全局风险: {'; '.join(global_risks[:3])}")
        fb = r.get("feedback", "")
        if fb:
            lines.append(f"- ⚠️ 用户反馈: {fb}")
        lines.append("")
    context = "\n".join(lines)
    if len(context) > MAX_HISTORY_CHARS:
        context = context[:MAX_HISTORY_CHARS] + "\n... (历史记录已截断)"
    return context


# ──────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """你是一名资深全栈工程师（Java Spring Boot + Vue3/TypeScript），
专注于 Code Review。分析 Git diff 时请保持客观、简洁、聚焦实际风险。
不要重复 diff 原文，不要泛泛而谈，直接指出具体位置和问题。"""

USER_PROMPT_TMPL = """请对以下 GitLab Merge Request 的代码变更进行 Review。

【MR 信息】
标题: {title}
描述: {description}
源分支: {source_branch} → 目标分支: {target_branch}
变更文件数: {file_count}

{history_context}
【代码 Diff】
{diff}

请按以下 JSON 格式输出，不要输出任何 JSON 之外的内容：
{{
  "summary": "一句话说明本次变更的核心业务目的",
  "risk_level": "low | medium | high",
  "risk_reason": "risk_level 定级理由（一句话）",
  "changes": [
    {{
      "file": "文件路径",
      "logic": "该文件改动的逻辑说明",
      "risks": ["风险点1", "风险点2"]
    }}
  ],
  "global_risks": [
    "跨文件/全局层面的风险，如接口破坏性变更、事务边界、并发问题等"
  ],
  "test_focus": ["建议重点测试的场景或接口"],
  "suggestions": ["可选的改进建议，非强制"]
}}"""

COMMIT_USER_PROMPT_TMPL = """请对以下 Git Commit 的代码变更进行 Review。

【Commit 信息】
提交信息: {title}
作者: {author}
提交时间: {date}
变更文件数: {file_count}

{history_context}
【代码 Diff】
{diff}

请按以下 JSON 格式输出，不要输出任何 JSON 之外的内容：
{{
  "summary": "一句话说明本次变更的核心业务目的",
  "risk_level": "low | medium | high",
  "risk_reason": "risk_level 定级理由（一句话）",
  "changes": [
    {{
      "file": "文件路径",
      "logic": "该文件改动的逻辑说明",
      "risks": ["风险点1", "风险点2"]
    }}
  ],
  "global_risks": [
    "跨文件/全局层面的风险，如接口破坏性变更、事务边界、并发问题等"
  ],
  "test_focus": ["建议重点测试的场景或接口"],
  "suggestions": ["可选的改进建议，非强制"]
}}"""


# ──────────────────────────────────────────────
# GitLab API 工具函数
# ──────────────────────────────────────────────

def gitlab_get(path: str) -> dict | list:
    """带认证的 GitLab API GET"""
    url = f"{GITLAB_URL.rstrip('/')}/api/v4{path}"
    resp = requests.get(url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_url(url: str) -> tuple[str, str, str]:
    """
    从 MR / Commit 页面 URL 解析出 (type, encoded_project_path, id_or_sha)
    type: "mr" 或 "commit"
    例 MR:     http://gitlab.xxx.com/group/repo/-/merge_requests/42
    例 Commit: http://gitlab.xxx.com/group/repo/-/commit/abc123def
    """
    mr_pattern = r"https?://[^/]+/(.+?)/-/merge_requests/(\d+)"
    m = re.search(mr_pattern, url)
    if m:
        project_path = m.group(1).strip("/")
        encoded = project_path.replace("/", "%2F")
        return ("mr", encoded, str(m.group(2)))

    commit_pattern = r"https?://[^/]+/(.+?)/-/commit/([a-f0-9]+)"
    m = re.search(commit_pattern, url)
    if m:
        project_path = m.group(1).strip("/")
        encoded = project_path.replace("/", "%2F")
        return ("commit", encoded, m.group(2))

    raise ValueError(f"无法解析 URL，需要 MR 或 Commit 页面链接: {url}")


def get_mr_info(project_id: str, mr_iid: str) -> dict:
    return gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}")


def get_mr_changes(project_id: str, mr_iid: str) -> list[dict]:
    data = gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    return data.get("changes", [])


def get_commit_info(project_id: str, sha: str) -> dict:
    return gitlab_get(f"/projects/{project_id}/repository/commits/{sha}")


def get_commit_changes(project_id: str, sha: str) -> list[dict]:
    return gitlab_get(f"/projects/{project_id}/repository/commits/{sha}/diff")


# ──────────────────────────────────────────────
# Diff 预处理：过滤噪音 + 裁剪大文件
# ──────────────────────────────────────────────

SKIP_EXTENSIONS = {
    ".lock", ".sum", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".min.js", ".min.css", ".map"
}

def should_skip(file_path: str) -> bool:
    fp = file_path.lower()
    for ext in SKIP_EXTENSIONS:
        if fp.endswith(ext):
            return True
    # 跳过自动生成目录
    if any(seg in fp for seg in ["node_modules/", "dist/", "target/", ".idea/", ".mvn/"]):
        return True
    return False


def format_changes(changes: list[dict]) -> tuple[str, int]:
    """
    将 changes 列表格式化为适合 AI 阅读的文本
    返回 (diff_text, 有效文件数)
    """
    parts = []
    valid_count = 0

    for ch in changes:
        path = ch.get("new_path") or ch.get("old_path", "unknown")
        diff = ch.get("diff", "")

        if should_skip(path):
            continue
        if not diff.strip():
            continue

        # 裁剪过长的单文件 diff
        lines = diff.splitlines()
        truncated = False
        if len(lines) > MAX_FILE_LINES:
            lines = lines[:MAX_FILE_LINES]
            truncated = True

        status = ""
        if ch.get("new_file"):   status = "[新增]"
        elif ch.get("deleted_file"): status = "[删除]"
        elif ch.get("renamed_file"):
            old = ch.get("old_path", "")
            status = f"[重命名 from {old}]"

        block = f"### {status} {path}\n```diff\n" + "\n".join(lines) + "\n```"
        if truncated:
            block += f"\n> ⚠️ 文件过长，仅展示前 {MAX_FILE_LINES} 行"

        parts.append(block)
        valid_count += 1

    return "\n\n".join(parts), valid_count


def truncate_diff(diff_text: str, max_chars: int) -> str:
    if len(diff_text) <= max_chars:
        return diff_text
    return diff_text[:max_chars] + f"\n\n> ⚠️ Diff 总量过大，已截断至 {max_chars} 字符"


# ──────────────────────────────────────────────
# AI 分析
# ──────────────────────────────────────────────

def ai_review(info: dict, diff_text: str, file_count: int,
              review_type: str = "mr", history_context: str = "") -> dict:
    client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE)

    if review_type == "commit":
        prompt = COMMIT_USER_PROMPT_TMPL.format(
            title=info.get("title", ""),
            author=info.get("author_name", "未知"),
            date=(info.get("committed_date") or "")[:10],
            file_count=file_count,
            history_context=history_context,
            diff=diff_text,
        )
    else:
        description = (info.get("description") or "").strip() or "（无描述）"
        prompt = USER_PROMPT_TMPL.format(
            title=info.get("title", ""),
            description=description[:500],
            source_branch=info.get("source_branch", ""),
            target_branch=info.get("target_branch", ""),
            file_count=file_count,
            history_context=history_context,
            diff=diff_text,
        )

    print("  → 正在调用 AI 分析，请稍候...")
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    # 防御性解析
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    result = json.loads(raw)
    return deduplicate_result(result)


# ──────────────────────────────────────────────
# 结果去重
# ──────────────────────────────────────────────

def deduplicate_result(result: dict) -> dict:
    seen_files = set()
    unique_changes = []
    for ch in result.get("changes", []):
        fname = ch.get("file", "")
        if fname and fname not in seen_files:
            seen_files.add(fname)
            unique_changes.append(ch)
    result["changes"] = unique_changes

    for key in ("global_risks", "test_focus", "suggestions"):
        items = result.get(key, [])
        if isinstance(items, list):
            seen = set()
            deduped = []
            for item in items:
                if item and item not in seen:
                    seen.add(item)
                    deduped.append(item)
            result[key] = deduped

    return result


# ──────────────────────────────────────────────
# 输出格式化
# ──────────────────────────────────────────────

RISK_ICON = {"low": "🟢 低", "medium": "🟡 中", "high": "🔴 高"}

def print_report(result: dict, info: dict, review_type: str = "mr"):
    if review_type == "commit":
        web_url = info.get("web_url", "")
        author = info.get("author_name", "未知")
        created = (info.get("committed_date") or "")[:10]
        print("\n" + "═" * 60)
        print(f"  🤖 AI Code Review 报告")
        print("═" * 60)
        print(f"  Commit: {info.get('title')}")
        print(f"  作者  : {author}  |  时间: {created}")
        print(f"  链接  : {web_url}")
    else:
        web_url = info.get("web_url", "")
        author = info.get("author", {}).get("name", "未知")
        created = info.get("created_at", "")[:10]
        print("\n" + "═" * 60)
        print(f"  🤖 AI Code Review 报告")
        print("═" * 60)
        print(f"  MR    : {info.get('title')}")
        print(f"  作者  : {author}  |  时间: {created}")
        print(f"  链接  : {web_url}")
        print(f"  分支  : {info.get('source_branch')} → {info.get('target_branch')}")
    print("─" * 60)

    risk = result.get("risk_level", "unknown")
    print(f"  风险等级  : {RISK_ICON.get(risk, risk)}")
    print(f"  定级理由  : {result.get('risk_reason', '')}")
    print(f"  变更摘要  : {result.get('summary', '')}")
    print("─" * 60)

    changes = result.get("changes", [])
    if changes:
        print("  📁 文件变更分析")
        for i, ch in enumerate(changes, 1):
            print(f"\n  [{i}] {ch.get('file', '')}")
            print(f"      逻辑: {ch.get('logic', '')}")
            risks = ch.get("risks", [])
            if risks:
                for r in risks:
                    print(f"      ⚠️  {r}")

    global_risks = result.get("global_risks", [])
    if global_risks:
        print("\n" + "─" * 60)
        print("  🌐 全局风险")
        for r in global_risks:
            print(f"  • {r}")

    test_focus = result.get("test_focus", [])
    if test_focus:
        print("\n" + "─" * 60)
        print("  🧪 建议测试重点")
        for t in test_focus:
            print(f"  • {t}")

    suggestions = result.get("suggestions", [])
    if suggestions:
        print("\n" + "─" * 60)
        print("  💡 改进建议（可选）")
        for s in suggestions:
            print(f"  • {s}")

    print("\n" + "═" * 60 + "\n")


def save_report(result: dict, info: dict, output_path: Optional[str], review_type: str = "mr"):
    if not output_path:
        return
    key = "commit" if review_type == "commit" else "mr"
    payload = {key: info, "review": result}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  📄 JSON 报告已保存到: {output_path}")


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GitLab MR AI Code Review")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mr-url",     help="MR 页面完整 URL")
    group.add_argument("--project-id", help="GitLab 项目 ID 或 encoded path")

    parser.add_argument("--mr-iid",    type=int, help="MR 序号（与 --project-id 配合使用）")
    parser.add_argument("--output",    help="将 JSON 报告保存到指定文件路径", default=None)
    parser.add_argument("--max-chars", type=int, default=MAX_DIFF_CHARS,
                        help=f"单次 AI 分析的最大 diff 字符数（默认 {MAX_DIFF_CHARS}）")
    parser.add_argument("--feedback",  help="对本次 Review 追加用户反馈（发现的实际问题或补充说明）")
    parser.add_argument("--no-history", action="store_true",
                        help="不加载历史 Review 记录作为上下文")
    args = parser.parse_args()

    if args.mr_url:
        review_type, project_id, ref_id = parse_url(args.mr_url)
    else:
        if not args.mr_iid:
            parser.error("--project-id 需配合 --mr-iid 使用")
        review_type = "mr"
        project_id = args.project_id
        ref_id = str(args.mr_iid)

    if review_type == "commit":
        print(f"\n🔍 正在获取 Commit 信息 (project={project_id}, sha={ref_id[:8]})...")
        info = get_commit_info(project_id, ref_id)
        print(f"  ✓ {info.get('title')}")
        print("📦 正在获取变更文件...")
        changes = get_commit_changes(project_id, ref_id)
    else:
        print(f"\n🔍 正在获取 MR 信息 (project={project_id}, iid={ref_id})...")
        info = get_mr_info(project_id, ref_id)
        print(f"  ✓ {info.get('title')}")
        print("📦 正在获取变更文件...")
        changes = get_mr_changes(project_id, ref_id)

    print(f"  ✓ 共 {len(changes)} 个文件变更")

    changed_files = [ch.get("new_path") or ch.get("old_path", "") for ch in changes]

    diff_text, valid_count = format_changes(changes)
    diff_text = truncate_diff(diff_text, args.max_chars)
    print(f"  ✓ 有效分析文件: {valid_count} 个（已过滤二进制/依赖文件）")

    if not diff_text.strip():
        print("  ⚠️  没有可分析的代码变更（全部为二进制或忽略文件）")
        sys.exit(0)

    store = ReviewStore(project_id)

    history_context = ""
    if not args.no_history:
        history_context = build_history_context(store)
        if history_context:
            print(f"  📜 已加载历史 Review 记录作为上下文")

    print("🤖 正在进行 AI 分析...")
    result = ai_review(info, diff_text, valid_count, review_type, history_context)

    print_report(result, info, review_type)

    saved_path = store.save(review_type, ref_id, info, result, changed_files)
    print(f"  📄 Review 记录已保存到: {saved_path}")

    save_report(result, info, args.output, review_type)

    if args.feedback:
        fb_path = store.add_feedback(args.feedback)
        if fb_path:
            print(f"  💬 用户反馈已追加到: {fb_path}")


if __name__ == "__main__":
    main()