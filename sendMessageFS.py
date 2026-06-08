#!/usr/bin/env python
# -*- encoding:utf-8 -*-

import sys
import json
import re
import requests
import time
import hashlib
import base64
import hmac
import calendar
import os

def get_project_id_by_url(gitlab_url, repo_http_url, token=""):
    """通过仓库 HTTP URL 自动查询 project_id"""
    if not repo_http_url:
        return None
    
    # 从 URL 提取 namespace/project 路径
    # http://47.96.74.113:7070/root/iitoo-web.git → root/iitoo-web
    match = re.search(r'/([^/]+/[^/]+?)(?:\.git)?$', repo_http_url)
    if not match:
        return None
    
    path = match.group(1)  # "root/iitoo-web"
    encoded_path = path.replace('/', '%2F')
    
    api_url = f"{gitlab_url}/api/v4/projects/{encoded_path}"
    headers = {"PRIVATE-TOKEN": token} if token else {}
    
    try:
        resp = requests.get(api_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            pid = resp.json().get('id')
            print(f"[INFO] 自动获取 project_id={pid} for {path}", file=sys.stderr)
            return str(pid)
    except Exception as e:
        print(f"[WARN] 自动查 project_id 失败: {e}", file=sys.stderr)
    return None

def gen_sign(timestamp, secret):
    string_to_sign = '{}\n{}'.format(timestamp, secret)
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()

    # 对结果进行base64处理
    sign = base64.b64encode(hmac_code).decode('utf-8')

    return sign

def count_diff_lines(diff_text):
    """统计 unified diff 里的增删行数"""
    added   = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed += 1
    return added, removed

def fetch_gitlab_compare(gitlab_url, project_id, before, after, token=""):
    """
    调用 GitLab Compare API 获取变更文件和合并分支信息
    GET /api/v4/projects/:id/repository/compare?from=before&to=after
    """
    if not before or not after or before == after:
        print(f"[WARN] before/after 无效: before={before} after={after}", file=sys.stderr)
        return [], ""

    api_url = f"{gitlab_url}/api/v4/projects/{project_id}/repository/compare"
    params  = {"from": before, "to": after, "straight": "false"}
    headers = {"PRIVATE-TOKEN": token} if token else {}

    print(f"[INFO] 调用 GitLab API: {api_url}", file=sys.stderr)
    print(f"[INFO] from={before[:8]} to={after[:8]}", file=sys.stderr)

    try:
        resp = requests.get(api_url, params=params, headers=headers, timeout=10)
        print(f"[INFO] 响应状态码: {resp.status_code}", file=sys.stderr)

        if resp.status_code == 401:
            return ["[错误] GitLab API 需要认证，请配置 PRIVATE-TOKEN"], ""
        if resp.status_code != 200:
            return [f"[错误] API 返回 HTTP {resp.status_code}"], ""

        data = resp.json()

        # ── 提取变更文件 ──────────────────────────
        files = []
        seen  = set()
        for diff in data.get('diffs', []):
            new_path  = diff.get('new_path', '')
            old_path  = diff.get('old_path', '')
            deleted   = diff.get('deleted_file', False)
            new_file  = diff.get('new_file', False)
            renamed   = diff.get('renamed_file', False)
            diff_text = diff.get('diff', '')
             # 统计增删行数
            added_lines, removed_lines = count_diff_lines(diff_text)
            # 行数标注，只显示非零的
            line_stat = ""
            parts = []
            if added_lines > 0:
                parts.append(f"+{added_lines}")
            if removed_lines > 0:
                parts.append(f"-{removed_lines}")
            if parts:
                line_stat = f"  ({', '.join(parts)})"

            if deleted:
                name = os.path.basename(old_path)
                key  = f"del:{name}"
                if key not in seen:
                    files.append(f"[删除] {name}{line_stat}")
                    seen.add(key)
            elif new_file:
                name = os.path.basename(new_path)
                key  = f"add:{name}"
                if key not in seen:
                    files.append(f"[新增] {name}{line_stat}")
                    seen.add(key)
            elif renamed:
                name = f"{os.path.basename(old_path)} → {os.path.basename(new_path)}"
                key  = f"ren:{name}"
                if key not in seen:
                    files.append(f"[重命名] {name}{line_stat}")
                    seen.add(key)
            else:
                name = os.path.basename(new_path)
                key  = f"mod:{name}"
                if key not in seen:
                    files.append(f"[修改] {name}{line_stat}")
                    seen.add(key)

        # ── 提取合并分支 ──────────────────────────
        merge_info = ""
        for commit in data.get('commits', []):
            msg = commit.get('message', '')
            # 匹配 "Merge branch 'xxx' into yyy"，排除远程 URL 型
            match = re.search(r"Merge branch '([^']+)' into ([^\n]+)", msg)
            if match:
                source = match.group(1).strip()
                target = match.group(2).strip()
                # 排除 "Merge branch 'dev' of http://..." 远程同步型
                if 'http' not in msg and 'ssh' not in msg:
                    merge_info = f"{source} -> {target}"
                    break

        print(f"[INFO] merge_info={merge_info}", file=sys.stderr)
        print(f"[INFO] files={files}", file=sys.stderr)
        return files, merge_info

    except requests.exceptions.ConnectionError:
        return ["[错误] 无法连接 GitLab，请检查网络"], ""
    except Exception as e:
        return [f"[异常] {str(e)}"], ""

JOB_URL = sys.argv[1]
JOB_NAME = sys.argv[2]
BUILD_NUMBER = sys.argv[3]
BUILD_USER = sys.argv[4]

status_arg = str(sys.argv[5])
if status_arg == "0":
    isFinish = "开始构建"
    template_color = "blue"
elif status_arg == "1":
    isFinish = "构建成功"
    template_color = "green"
else:
    isFinish = "构建失败"
    template_color = "red"
# 位置: url  job_name  build_num  user  status  before  after  repo_http_url
before_sha    = sys.argv[6].strip() if len(sys.argv) > 6 else ""
after_sha     = sys.argv[7].strip() if len(sys.argv) > 7 else ""
repo_http_url = sys.argv[8].strip() if len(sys.argv) > 8 else ""

GITLAB_URL = "http://47.96.74.113:7070"
gl_token   = "qeUsvrtzsrLZ-8xza51T"  # 公开仓库不需要，私有仓库在这里填

project_id = get_project_id_by_url(GITLAB_URL, repo_http_url, gl_token) or "0"

# ── 调用 GitLab API ───────────────────────────────────────────
changed_files, merge_info = fetch_gitlab_compare(
    GITLAB_URL, project_id, before_sha, after_sha, gl_token
)
# ── 构建消息内容 ──────────────────────────────
timestamp   = str(calendar.timegm(time.gmtime()))
currenttime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
def build_content():
    lines = [
        f"项目名称：{JOB_NAME}",
        f"构建编号：第{BUILD_NUMBER}次",
        f"构建运行时间：{currenttime}",
        f"构建人：{BUILD_USER}",
        f"合并分支：{merge_info if merge_info else '无'}",
    ]
    if changed_files:
        lines.append(f"变更文件（共{len(changed_files)}个）：")
        for i, f in enumerate(changed_files[:20], 1):
            lines.append(f"　{i}. {f}")
        if len(changed_files) > 20:
            lines.append(f"　... 共{len(changed_files)}个文件")
    else:
        lines.append("变更文件：无")
    lines.append("<at id=all></at>")
    return "\n".join(lines)
    
# ── 发送飞书消息 ──────────────────────────────
sign_key = 'E7ZmCwfZPsFLVsuMXKpQRf'
sgin = gen_sign(timestamp, sign_key)
url = 'https://open.feishu.cn/open-apis/bot/v2/hook/612e56d6-c06c-47d3-9bd6-4ef70c311970'

json_body  = {
    "timestamp": "" + timestamp + "",
    "msg_type": "interactive",
    "sign": "" + sgin + "",
    "card": {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": True
        },
        "elements": [
            {
            "tag": "div",
            "text": {
                "content": build_content(),
                "tag": "lark_md"
            }
        }, {
            "actions": [{
                "tag": "button",
                "text": {
                    "content": "查看报告",
                    "tag": "lark_md"
                },
                "url": JOB_URL+"logText/progressiveText",
                "type": "default",
                "value": {}
            }],
            "tag": "action"
        }],
        "header": {
            "template": template_color,
            "title": {
                "content": JOB_NAME + " "+isFinish+"",
                "tag": "plain_text"
            }
        }
    }
}

requests.request(method='post', url=url,
                 headers={'Content-Type': 'application/json'}, json=json_body)
# 清理临时文件
try:
    if len(sys.argv) > 6 and os.path.exists(sys.argv[6]):
        os.remove(sys.argv[6])
except:
    pass