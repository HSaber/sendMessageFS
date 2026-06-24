# GitLab MR AI Code Review 工具

手动对任意 GitLab Merge Request 进行 AI 代码分析。

## 文件说明

```
mr_ai_review.py       # 主脚本
mr_review_config.env  # 配置（填入你的 token，不要提交到 Git）
```

## 第一步：填写配置

编辑 `mr_review_config.env`，填入：

| 变量 | 说明 |
|------|------|
| `GITLAB_URL` | 你的 GitLab 地址，如 `http://192.168.1.100:8080` |
| `GITLAB_TOKEN` | GitLab Personal Access Token（需要 `read_api` 权限） |
| `DEEPSEEK_KEY` | DeepSeek API Key |

**生成 GitLab Token：**
GitLab → 右上角头像 → Edit Profile → Access Tokens → 勾选 `read_api` → 生成

## 第二步：加载配置 & 运行

### Linux / Mac / Git Bash（推荐）

```bash
source mr_review_config.env

python3 mr_ai_review.py --mr-url "http://your-gitlab/group/repo/-/merge_requests/42"
```

### Windows CMD

```cmd
for /f "tokens=1,2 delims==" %a in (mr_review_config.env) do set %a=%b

python mr_ai_review.py --mr-url "http://your-gitlab/group/repo/-/merge_requests/42"
```

### Windows PowerShell

```powershell
Get-Content mr_review_config.env | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)') {
        $env:$matches[1] = $matches[2]
    }
}

python mr_ai_review.py --mr-url "http://your-gitlab/group/repo/-/merge_requests/42"
```

### 其他运行方式

```bash
# 方式 2：用项目 ID + MR 编号
python3 mr_ai_review.py --project-id 15 --mr-iid 42

# 同时保存 JSON 报告
python3 mr_ai_review.py --mr-url "..." --output report_mr42.json

# 代码量大时增加 token 上限（默认 12000 字符）
python3 mr_ai_review.py --mr-url "..." --max-chars 20000
```

## 输出示例

```
════════════════════════════════════════════════════════════
  🤖 AI Code Review 报告
════════════════════════════════════════════════════════════
  MR    : feat: 用户权限模块重构
  作者  : 张三  |  时间: 2025-06-16
  链接  : http://gitlab.xxx/group/repo/-/merge_requests/42
  分支  : feature/auth-refactor → main
────────────────────────────────────────────────────────────
  风险等级  : 🟡 中
  定级理由  : 修改了权限校验核心逻辑，但有完善的单测覆盖
  变更摘要  : 将角色权限从硬编码枚举迁移到数据库动态配置

  📁 文件变更分析

  [1] src/main/java/com/xxx/service/AuthService.java
      逻辑: 新增 loadPermissionsFromDB() 替代原有静态 Map
      ⚠️  缓存失效逻辑缺失，权限变更后需重启才生效
      ⚠️  数据库查询在每次鉴权时触发，高并发下存在性能风险

  🌐 全局风险
  • RoleController 接口入参新增字段，需确认前端已同步更新

  🧪 建议测试重点
  • 多角色叠加时的权限边界场景
  • 角色被删除后已登录用户的鉴权行为

  💡 改进建议（可选）
  • 建议对 loadPermissionsFromDB 加本地缓存 + TTL
════════════════════════════════════════════════════════════
```

## 常见问题

**Q: 提示 401 Unauthorized**  
A: GitLab Token 填写错误，或 Token 没有 `read_api` 权限

**Q: 提示 404 Not Found**  
A: 项目路径解析失败，改用 `--project-id 数字ID` 方式（项目 ID 在 GitLab 项目首页 Settings → General 里查看）

**Q: AI 返回不是 JSON**  
A: 偶发，重新执行即可；或适当降低 `--max-chars` 减少 diff 长度

**Q: 哪些文件会被跳过？**  
A: 自动跳过：`*.lock` / `*.min.js` / `*.png` 等二进制和构建产物，以及 `node_modules/`、`dist/`、`target/` 目录下的文件
