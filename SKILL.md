---
name: mineru-to-yuque
description: Translate academic papers (PDF/URL) to Chinese and publish to Yuque. Pipeline: MinerU parsing → GLM translation → image upload → Yuque publish. Use when user wants to translate a paper, convert PDF to Chinese markdown, or publish a paper to Yuque. Supports arXiv URLs, ACL Anthology URLs, local PDF files, and hjfy.top pre-translated papers.
---

# MinerU to Yuque

Translate academic papers to Chinese and publish as Yuque documents.

## Quick Start

```bash
# From a URL (arXiv, ACL Anthology, etc.)
python scripts/mineru_to_yuque.py "https://aclanthology.org/2026.customnlp4u-1.8.pdf" --title "论文标题"

# From a local PDF
python scripts/mineru_to_yuque.py "paper.pdf" --title "论文标题"

# Skip translation (keep English)
python scripts/mineru_to_yuque.py "paper.pdf" --skip-translate

# Skip Yuque publish (save .md only)
python scripts/mineru_to_yuque.py "paper.pdf" --skip-publish

# Update existing Yuque doc
python scripts/mineru_to_yuque.py "paper.pdf" --update doc_slug
```

Script location: `scripts/mineru_to_yuque.py`

## Two Paths

### Path A: hjfy.top (arXiv papers only)

For arXiv papers with Chinese translations on hjfy.top, use `D:\01\2\latex_to_md.py` instead:

```python
from latex_to_md import convert_hjfy_paper
md_path = convert_hjfy_paper("2604.15109")  # arXiv ID
```

Then publish: `python yuque_cli.py create -t "Title" -f <md_path> --format markdown`

**Check availability first**: `GET https://hjfy.top/api/arxivStatus/{paper_id}` → `{"status": "finished"}`

### Path B: MinerU (any PDF)

For non-arXiv papers or when hjfy.top doesn't have the translation. This is the default path handled by `mineru_to_yuque.py`.

## Pipeline Steps

1. **MinerU Parse** — Submit PDF to Precise API (token auth). URL submission preferred (avoids file upload issues). Falls back to file upload with presigned URL.
2. **Download ZIP** — ZIP contains `full.md` + `images/`. Download from MinerU CDN.
3. **Translate** — GLM translates to Chinese in chunks (~4K chars). References section preserved untranslated. Subsection headings (`## 2.1`) → `###`. Figure/Table numbering added.
4. **Upload Images** — Images extracted from ZIP (avoids CDN 403 issues). Uploaded to Yuque CDN via cookie-auth API. MinerU CDN URLs replaced with `cdn.nlark.com` URLs.
5. **Publish** — Via `yuque-docs-skill` CLI (`create` or `update`).

## Required Configuration

`.env` file (default path: working directory):

```
MINERU_TOKEN=<jwt from mineru.net/apiManage>
LLM_API_KEY=<zhipuai key>
LLM_MODEL=glm-4-flash
DEEPSEEK_BASE_URL=https://open.bigmodel.cn/api/paas/v4
YUQUE_REPO=u22014392/kb
YUQUE_TOKEN=<40-char token>
```

`yuque-session-cookies.json` — for cookie-auth image upload (required for images).

## Key Technical Details

- **MinerU CDN images can 403** — always extract images from ZIP instead of downloading individually
- **Yuque v2 API rate limit**: 50 calls/day per account — each publish/update = 1 call
- **Image upload** uses internal cookie API (`POST /api/upload/attach?ctoken=xxx`), not v2 token — no quota consumed
- **JPG→PNG conversion** required for Yuque upload (PIL/Pillow)
- **`<` in formulas** must be replaced with `\lt` before Yuque publish (HTML escaping issue)
- **Subsection numbering**: `## 2.1 Title` should become `### 2.1 Title` in Yuque
