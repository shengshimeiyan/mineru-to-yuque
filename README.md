# MinerU to Yuque

学术论文 PDF → 中文翻译 → 语雀文档，一键自动化。

将任意学术论文（arXiv、ACL Anthology、本地 PDF）通过 MinerU 解析、GLM 翻译、图片上传，最终发布为语雀知识库文档。

## 功能特点

- 📄 **支持任意 PDF** — URL 或本地文件，不限论文来源
- 🔄 **全自动流程** — MinerU 解析 → GLM 翻译 → 图片上传 → 语雀发布，一条命令搞定
- 🖼️ **图片自动处理** — 从 ZIP 提取图片上传至语雀 CDN，避免 MinerU CDN 403 问题
- 📐 **公式保留** — LaTeX 公式原样保留，语雀自动渲染
- 📎 **参考文献去除** — 自动移除 References 章节
- 📚 **附录翻译** — 参考文献后的 Appendix 自动保留并翻译
- 🔁 **健壮重试** — GLM 超时/500 错误自动重试（5次，递增超时+退避）
- ✏️ **更新已有文档** — 支持 `--update` 更新已发布的语雀文档

## 快速开始

### 安装依赖

```bash
pip install httpx python-dotenv Pillow
```

### 配置

创建 `.env` 文件：

```env
MINERU_TOKEN=<从 mineru.net/apiManage 获取>
LLM_API_KEY=<智谱 API Key>
LLM_MODEL=glm-4-flash
DEEPSEEK_BASE_URL=https://open.bigmodel.cn/api/paas/v4
YUQUE_REPO=<你的语雀知识库路径，如 u22014392/kb>
YUQUE_TOKEN=<语雀 Token>
```

图片上传还需 `yuque-session-cookies.json`（从浏览器登录语雀后导出 Cookie）。

### 使用

```bash
# 从 URL 翻译论文
python scripts/mineru_to_yuque.py "https://aclanthology.org/2024.emnlp-industry.114.pdf" --title "LLM时代的意图检测"

# 从本地 PDF 翻译
python scripts/mineru_to_yuque.py "paper.pdf" --title "论文标题"

# 指定 .env 路径
python scripts/mineru_to_yuque.py "paper.pdf" --env /path/to/.env --title "标题"

# 不翻译（保留英文）
python scripts/mineru_to_yuque.py "paper.pdf" --skip-translate

# 只翻译不发布
python scripts/mineru_to_yuque.py "paper.pdf" --skip-publish

# 更新已有语雀文档
python scripts/mineru_to_yuque.py "paper.pdf" --update doc_slug
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `source` | PDF URL 或本地文件路径（必填） | — |
| `--title` | 语雀文档标题 | 从 PDF 文件名推断 |
| `--env` | `.env` 文件路径 | 当前目录 |
| `--output-dir` | 输出目录 | `./output` |
| `--skip-translate` | 跳过翻译，保留英文 | `False` |
| `--skip-publish` | 跳过语雀发布，仅保存 .md | `False` |
| `--update SLUG` | 更新已有文档（而非新建） | — |

## 流程详解

```
PDF URL / 本地文件
       ↓
[1] MinerU 提交解析（Precise API，Token 认证）
       ↓
[2] 下载 ZIP → 提取 full.md + images/
       ↓
[3] GLM 分块翻译（~4K chars/chunk，重叠拼接）
    · 自动移除 References 章节
    · 保留并翻译 Appendix
    · 子标题降级：## 2.1 → ### 2.1
       ↓
[4] 图片上传语雀 CDN（cookie-auth API）
    · JPG → PNG 转换
    · 替换 Markdown 中的图片引用为 nlark CDN URL
       ↓
[5] 语雀发布（yuque-docs-skill CLI）
```

## arXiv 论文的替代方案

arXiv 论文可使用 [hjfy.top](https://hjfy.top) 获取已有中文翻译（LaTeX 格式），翻译质量更好：

```bash
# 检查是否已有翻译
curl https://hjfy.top/api/arxivStatus/2604.15109

# 使用 hjfy 流程
python D:/01/2/latex_to_md.py  # 内置 convert_hjfy_paper()
```

## 注意事项

- **语雀 v2 API 每日限额 50 次** — 每次发布/更新消耗 1 次，不要轮询检查
- **图片上传走 cookie API** — 不消耗 v2 配额，需有效的 `yuque-session-cookies.json`
- **MinerU 免费额度** — 每天 1,000 页
- **公式中的 `<`** — 会被语雀 HTML 转义为 `&lt;`，需替换为 `\lt`
- **Windows 编码** — 子进程使用 `encoding='utf-8', errors='replace'` 避免 GBK 乱码

## 安装到 AI 编程工具

### Codex（OpenAI）

Codex 通过 skill 系统集成，安装后可直接用自然语言触发：

```bash
# 方式 1：手动安装
git clone https://github.com/shengshimeiyan/mineru-to-yuque.git ~/.codex/skills/mineru-to-yuque

# 方式 2：使用 skill-installer（Codex 内置）
# 在 Codex 对话中输入：
/install-skill https://github.com/shengshimeiyan/mineru-to-yuque
```

安装后，在 Codex 对话中直接说：

> "翻译这篇论文到语雀：https://aclanthology.org/2024.emnlp-industry.114/"

Codex 会自动调用 skill 执行全流程。

### Claude Code

Claude Code 使用 MCP Server 集成外部工具：

1. 在项目根目录创建 `.mcp.json`（或编辑 `~/.claude/.mcp.json`）：

```json
{
  "mcpServers": {
    "mineru-to-yuque": {
      "command": "python",
      "args": ["C:/Users/你/.codex/skills/mineru-to-yuque/scripts/mineru_to_yuque.py"],
      "env": {
        "DOTENV_PATH": "C:/Users/你/.env文件的路径/.env"
      }
    }
  }
}
```

2. 或者，直接在 `CLAUDE.md` 中添加说明，让 Claude 调用脚本：

```markdown
## 论文翻译工具

翻译论文到语雀时，运行：
python ~/.codex/skills/mineru-to-yuque/scripts/mineru_to_yuque.py "<PDF_URL>" --title "标题" --env /path/to/.env
```

3. 在 Claude Code 对话中使用：

> "帮我翻译这篇论文：https://aclanthology.org/2024.emnlp-industry.114/"

### Cursor

Cursor 通过 MCP Server 或自定义指令集成：

1. 打开 Cursor Settings → Features → Model Context Protocol

2. 添加 MCP Server：

```json
{
  "mineru-to-yuque": {
    "command": "python",
    "args": ["C:/Users/你/.codex/skills/mineru-to-yuque/scripts/mineru_to_yuque.py"]
  }
}
```

3. 或者在 `.cursorrules` 中添加：

```
## 论文翻译
翻译论文到语雀时，运行命令：
python ~/.codex/skills/mineru-to-yuque/scripts/mineru_to_yuque.py "<PDF_URL>" --title "标题" --env /path/to/.env
```

4. 在 Cursor Chat 中使用：

> "翻译这篇论文到语雀：https://arxiv.org/pdf/2604.15109"

### 通用方式（任何 AI 工具）

无论什么 AI 编程工具，核心脚本都是独立的 Python 脚本，可以直接调用：

```bash
# 1. 克隆仓库
git clone https://github.com/shengshimeiyan/mineru-to-yuque.git
cd mineru-to-yuque

# 2. 安装依赖
pip install httpx python-dotenv Pillow

# 3. 配置 .env（见上方"配置"章节）

# 4. 运行
python scripts/mineru_to_yuque.py "https://aclanthology.org/2024.emnlp-industry.114.pdf" --title "LLM时代的意图检测"
```

只要能让 AI 工具执行 shell 命令，就能集成此工具。

## License

MIT
