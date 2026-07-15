#!/usr/bin/env python3
"""MinerU → Translate → Yuque: Full pipeline for PDF papers.

Usage:
    python mineru_to_yuque.py <pdf_url_or_path> [--title TITLE] [--skip-translate] [--skip-publish]

Steps:
    1. Submit PDF to MinerU Precise API for parsing
    2. Download ZIP (contains full.md + images/)
    3. Extract Markdown and images
    4. Translate to Chinese via GLM (skip with --skip-translate)
    5. Upload images to Yuque CDN (from ZIP, avoiding CDN 403)
    6. Publish to Yuque via yuque-docs-skill CLI

Requires .env with:
    MINERU_TOKEN, LLM_API_KEY, YUQUE_REPO, YUQUE_TOKEN
    and yuque-session-cookies.json for image upload.
"""
from __future__ import annotations

import argparse, io, json, logging, os, re, sys, tempfile, time, uuid, zipfile
from pathlib import Path
from dotenv import load_dotenv
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────

def load_env(env_path: str = ".env"):
    load_dotenv(env_path)
    cfg = {
        "mineru_token": os.getenv("MINERU_TOKEN", ""),
        "mineru_api": "https://mineru.net/api/v4",
        "llm_api_key": os.getenv("LLM_API_KEY", ""),
        "llm_api_base": os.getenv("DEEPSEEK_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "llm_model": os.getenv("LLM_MODEL", "glm-4-flash"),
        "yuque_repo": os.getenv("YUQUE_REPO", ""),
        "yuque_token": os.getenv("YUQUE_TOKEN", ""),
        "cookies_path": os.getenv("YUQUE_COOKIES_PATH", "yuque-session-cookies.json"),
    }
    missing = [k for k, v in cfg.items() if not v and k in ("mineru_token", "llm_api_key", "yuque_repo", "yuque_token")]
    if missing:
        logger.warning("Missing env vars: %s", ", ".join(missing))
    return cfg


# ── Step 1: MinerU Parse ───────────────────────────────

def submit_mineru_task(source: str, cfg: dict) -> str:
    """Submit PDF to MinerU. Returns task_id."""
    headers = {"Authorization": f"Bearer {cfg['mineru_token']}"}
    api = cfg["mineru_api"]

    if source.startswith("http"):
        # URL submission
        payload = {
            "url": source,
            "enable_formula": True,
            "enable_table": True,
            "language": "en",
        }
        resp = httpx.post(f"{api}/extract/task", headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"MinerU submit failed: {result.get('msg')}")
        task_id = result["data"]["task_id"]
        logger.info("Submitted URL task: %s", task_id)
        return task_id
    else:
        # File upload: get presigned URL → PUT file → wait for task_id
        pdf_path = Path(source)
        data_id = str(uuid.uuid4())
        payload = {
            "files": [{"name": pdf_path.name, "data_id": data_id}],
            "enable_formula": True,
            "enable_table": True,
            "language": "en",
        }
        resp = httpx.post(f"{api}/file-urls/batch", headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"MinerU batch failed: {result.get('msg')}")

        file_items = result.get("data", {}).get("files", result.get("data", []))
        if isinstance(file_items, dict) and "files" in file_items:
            file_items = file_items["files"]
        upload_url = file_items[0].get("upload_url", "") if isinstance(file_items, list) else file_items.get("upload_url", "")
        batch_id = result.get("data", {}).get("batch_id", "")

        if not upload_url:
            raise RuntimeError("MinerU returned empty upload_url")

        # Upload file
        file_bytes = pdf_path.read_bytes()
        httpx.put(upload_url, content=file_bytes, headers={"Content-Type": "application/octet-stream"}, timeout=120).raise_for_status()
        logger.info("Uploaded %d bytes to MinerU", len(file_bytes))

        # Wait for task_id
        for _ in range(20):
            time.sleep(3)
            resp = httpx.get(f"{api}/extract/task/{data_id}", headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                tid = data.get("task_id")
                if tid:
                    logger.info("Got task_id: %s", tid)
                    return tid
            if batch_id:
                resp = httpx.get(f"{api}/extract/batch/{batch_id}", headers=headers, timeout=15)
                if resp.status_code == 200:
                    tasks = resp.json().get("data", {}).get("tasks", [])
                    if tasks and tasks[0].get("task_id"):
                        logger.info("Got task_id via batch: %s", tasks[0]["task_id"])
                        return tasks[0]["task_id"]
        raise RuntimeError("Timeout waiting for MinerU task_id")


def poll_mineru_task(task_id: str, cfg: dict) -> dict:
    """Poll until done. Returns task info with full_zip_url."""
    headers = {"Authorization": f"Bearer {cfg['mineru_token']}"}
    for _ in range(120):
        resp = httpx.get(f"{cfg['mineru_api']}/extract/task/{task_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            time.sleep(5)
            continue
        data = resp.json().get("data", {})
        state = data.get("state", "")
        if state == "done":
            logger.info("MinerU task done: %s", task_id)
            return data
        elif state in ("failed", "error"):
            raise RuntimeError(f"MinerU task failed: {data.get('err_msg', 'unknown')}")
        progress = data.get("progress", "")
        logger.info("  MinerU state=%s progress=%s", state, progress)
        time.sleep(5)
    raise RuntimeError("MinerU task timed out")


# ── Step 2: Download & Extract ZIP ────────────────────

def download_and_extract(zip_url: str, output_dir: str) -> tuple[str | None, list[str]]:
    """Download ZIP, extract. Returns (markdown_path, [image_paths]).
    image_paths are absolute local paths to image files in the same subdirectory as full.md.
    """
    logger.info("Downloading ZIP: %s", zip_url[:80])
    resp = httpx.get(zip_url, timeout=60, follow_redirects=True)
    resp.raise_for_status()

    extract_dir = os.path.join(output_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(extract_dir)

    # Find markdown
    md_path = None
    md_dir = None
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if f == "full.md":
                md_path = os.path.join(root, f)
                md_dir = root
                break
        if md_path:
            break

    # Find images — prefer images in same directory as full.md
    images = []
    if md_dir:
        img_dir = os.path.join(md_dir, "images")
        if os.path.isdir(img_dir):
            for f in os.listdir(img_dir):
                if f.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    images.append(os.path.join(img_dir, f))

    # Fallback: top-level images dir
    if not images:
        img_dir = os.path.join(extract_dir, "images")
        if os.path.isdir(img_dir):
            for f in os.listdir(img_dir):
                if f.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    images.append(os.path.join(img_dir, f))

    logger.info("Extracted: md=%s, images=%d", md_path is not None, len(images))
    return md_path, images


# ── Step 3: Translate ─────────────────────────────────

# Standard NLP/AI terminology glossary for consistent translation
GLOSSARY = {
    "intent detection": "意图检测",
    "intent classification": "意图分类",
    "intent": "意图",
    "dialogue system": "对话系统",
    "dialog system": "对话系统",
    "natural language understanding": "自然语言理解",
    "NLU": "自然语言理解（NLU）",
    "out-of-scope": "域外",
    "OOS": "域外（OOS）",
    "out-of-domain": "域外",
    "OOD": "域外（OOD）",
    "large language model": "大语言模型",
    "LLM": "大语言模型（LLM）",
    "fine-tuning": "微调",
    "fine-tuned": "微调的",
    "pre-trained": "预训练的",
    "pre-training": "预训练",
    "in-context learning": "上下文学习",
    "ICL": "上下文学习（ICL）",
    "prompt": "提示",
    "prompting": "提示",
    "few-shot": "少样本",
    "zero-shot": "零样本",
    "multi-turn": "多轮",
    "single-turn": "单轮",
    "semantic": "语义",
    "embedding": "嵌入",
    "token": "词元",
    "tokenization": "分词",
    "transformer": "Transformer",
    "attention mechanism": "注意力机制",
    "self-attention": "自注意力",
    "cross-attention": "交叉注意力",
    "knowledge distillation": "知识蒸馏",
    "data augmentation": "数据增强",
    "ablation study": "消融实验",
    "baseline": "基线",
    "state-of-the-art": "最先进的",
    "SOTA": "最先进（SOTA）",
    "benchmark": "基准测试",
    "downstream task": "下游任务",
    "overfitting": "过拟合",
    "underfitting": "欠拟合",
    "generalization": "泛化",
    "inference": "推理",
    "entropy": "熵",
    "uncertainty": "不确定性",
    "calibration": "校准",
    "retrieval-augmented generation": "检索增强生成",
    "RAG": "检索增强生成（RAG）",
    "hallucination": "幻觉",
    "chain-of-thought": "思维链",
    "CoT": "思维链（CoT）",
    "reinforcement learning": "强化学习",
    "RLHF": "基于人类反馈的强化学习（RLHF）",
}

GLOSSARY_PROMPT = "\n".join(f"- {k} → {v}" for k, v in GLOSSARY.items())

SYSTEM_PROMPT = f"""You are a professional academic paper translator. Translate the following English academic text to Chinese.

Rules:
- Translate for fluency and accuracy in Chinese academic writing style, not word-by-word
- Preserve all technical terms with their first occurrence annotated as: 中文术语（English Term）
- All LaTeX formulas ($...$ and $$...$$) MUST be preserved exactly as-is
- All HTML tables (<table>...</table>) MUST be preserved as-is, do NOT translate
- All image markers must be preserved as-is
- Code blocks (```...```) must NOT be translated
- Markdown heading hierarchy must be preserved exactly
- Output ONLY the translated Markdown, no explanations, no markdown code fences
- ## 1 Introduction → ## 1 引言, keep numbering
- Figure X: → **图X**, Table X: → **表X**

Key terminology (MUST follow):
{GLOSSARY_PROMPT}"""


def translate_markdown(md: str, cfg: dict, eng_title: str = "") -> str:
    """Translate Markdown to Chinese via GLM. References are removed, Appendix is translated."""
    # Remove References section but keep Appendix (which may follow References)
    main_text = md
    for pat in [r'\n##\s*References?\s*\n', r'\n##\s*参考文献\s*\n']:
        m = re.search(pat, md)
        if m:
            refs_start = m.start()
            after_refs = md[m.end():]
            # Check if Appendix exists after References
            appendix_match = re.search(r'\n##\s*Appendix\s*\n', after_refs)
            if appendix_match:
                # Keep everything before References + Appendix onwards
                appendix_start_in_after = appendix_match.start()
                main_text = md[:refs_start] + "\n" + after_refs[appendix_start_in_after:]
                logger.info("Removed References (%d chars), kept Appendix", m.end() - refs_start - appendix_start_in_after)
            else:
                # No Appendix after References, just remove References section
                # But check for section headings like ## A, ## B that might be appendix subsections
                # Pattern: ## References\n...\n## X Title (where X is a single uppercase letter)
                subsection_match = re.search(r'\n##\s+[A-Z]\s+', after_refs)
                if subsection_match:
                    main_text = md[:refs_start] + "\n" + after_refs[subsection_match.start():]
                    logger.info("Removed References (%d chars), kept appendix subsections", m.end() - refs_start - subsection_match.start())
                else:
                    main_text = md[:refs_start]
                    logger.info("Removed References section (%d chars discarded)", len(md) - refs_start)
            break

    # Chunk — split at heading boundaries first, then split oversized sections
    max_chunk = 4000
    min_chunk = 500  # merge small sections back together

    # Step 1: split at ## heading boundaries (keep heading with its content)
    raw_sections = re.split(r'(?=^## )', main_text, flags=re.MULTILINE)
    raw_sections = [s for s in raw_sections if s.strip()]

    # Step 2: merge tiny sections with the next one
    merged = []
    buf = ""
    for sec in raw_sections:
        if not buf:
            buf = sec
        elif len(buf) < min_chunk:
            buf += "\n\n" + sec
        else:
            merged.append(buf)
            buf = sec
    if buf:
        merged.append(buf)

    # Step 3: split any section that still exceeds max_chunk at paragraph boundaries
    chunks = []
    for sec in merged:
        if len(sec) <= max_chunk:
            chunks.append(sec)
        else:
            # Split at \n\n within the section
            paragraphs = re.split(r'\n\n', sec)
            sub = ""
            for para in paragraphs:
                if sub and len(sub) + len(para) + 2 > max_chunk:
                    chunks.append(sub)
                    sub = para
                else:
                    sub = (sub + "\n\n" + para) if sub else para
            if sub:
                chunks.append(sub)

    logger.info("Translating %d chunks...", len(chunks))
    for i, c in enumerate(chunks):
        first_line = c.split('\n')[0][:60]
        logger.debug("  Chunk %d: %d chars — %s", i + 1, len(c), first_line)

    # Translate chunks with concurrency for speed
    # Each chunk carries ~200 chars of the PREVIOUS ORIGINAL (not translated) text as context
    max_concurrent = min(4, len(chunks))  # up to 4 parallel requests
    
    def _translate_chunk(idx: int, chunk: str) -> tuple[int, str]:
        """Translate a single chunk. Returns (idx, translated_text)."""
        # No context overlap — it causes heading duplication in parallel mode.
        # Instead, the system prompt handles coherence.
        user_msg = chunk

        # Retry up to 5 times with increasing timeout and backoff
        for attempt in range(5):
            timeout = 120 + attempt * 60  # 120s, 180s, 240s, 300s, 360s
            try:
                resp = httpx.post(
                    f"{cfg['llm_api_base']}/chat/completions",
                    headers={"Authorization": f"Bearer {cfg['llm_api_key']}"},
                    json={
                        "model": cfg["llm_model"],
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg},
                        ],
                        "max_tokens": 8192,
                        "temperature": 0.3,
                    },
                    timeout=timeout,
                )
                if resp.status_code >= 500:
                    logger.warning("  Chunk %d attempt %d: server error %d", idx + 1, attempt + 1, resp.status_code)
                    if attempt < 4:
                        import time; time.sleep(10 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                result = resp.json()
                text = result["choices"][0]["message"]["content"]
                return (idx, text)
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                logger.warning("  Chunk %d attempt %d timed out (%ds): %s", idx + 1, attempt + 1, timeout, e)
                if attempt == 4:
                    raise
                import time; time.sleep(10)
                logger.info("  Retrying...")
        raise RuntimeError(f"Chunk {idx + 1} failed after 5 attempts")

    # Use ThreadPoolExecutor for parallel translation
    from concurrent.futures import ThreadPoolExecutor, as_completed
    translated = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {executor.submit(_translate_chunk, idx, chunk): idx for idx, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            idx, text = future.result()
            translated[idx] = text
            logger.info("  Chunk %d/%d done (%d chars)", idx + 1, len(chunks), len(text))

    result = "\n\n".join(translated)

    # Post-process: clean up LLM output artifacts
    result = result.replace('🔤', '')                   # remove 🔤 markers
    result = re.sub(r'^\s*---\s*$', '', result, flags=re.MULTILINE)  # remove standalone ---
    result = re.sub(r'^```markdown\s*\n?', '', result, flags=re.MULTILINE)  # remove markdown fences
    result = re.sub(r'\n{3,}', '\n\n', result)          # collapse excessive blank lines

    # Post-process: deduplicate headings caused by chunk overlap in parallel translation
    # A heading appearing twice within a short span (20 lines) is likely a chunk-boundary duplicate
    lines = result.split('\n')
    heading_positions = []  # (line_idx, heading_text)
    for i, line in enumerate(lines):
        m = re.match(r'^(#{1,6}\s+.+)$', line)
        if m:
            heading_positions.append((i, m.group(1).strip()))
    
    # Find duplicate headings within 20 lines of each other
    dup_indices = set()
    for i in range(len(heading_positions)):
        for j in range(i + 1, len(heading_positions)):
            idx_i, text_i = heading_positions[i]
            idx_j, text_j = heading_positions[j]
            if idx_j - idx_i > 20:
                break  # too far apart, stop checking
            if text_i == text_j:
                # Mark the first occurrence for removal (keep the second, which has content after it)
                dup_indices.add(idx_i)
                logger.debug("Deduplicating heading at line %d: %s", idx_i, text_i[:40])
    
    result = '\n'.join(line for i, line in enumerate(lines) if i not in dup_indices)

    # Post-process: fix < and > inside formulas (Yuque HTML-escapes them)
    def _fix_formula_lt_gt(md: str) -> str:
        """Replace < with \\lt and > with \\gt inside $...$ and $$...$$."""
        def replace_in_formula(m):
            text = m.group(0)
            text = text.replace('<', r'\lt ')
            text = text.replace('>', r'\gt ')
            return text
        # Process $$...$$ first (greedy), then $...$
        md = re.sub(r'\$\$(.+?)\$\$', replace_in_formula, md, flags=re.DOTALL)
        md = re.sub(r'\$(.+?)\$', replace_in_formula, md)
        return md
    result = _fix_formula_lt_gt(result)

    # Post-process: clean up LaTeX residuals
    latex_residuals = [
        (r'\[leftmargin[=\*\d\.\,\s]+\]', ''),        # [leftmargin=*]
        (r'\\bibliography\{[^}]*\}', ''),               # \bibliography{...}
        (r'\\appendix\b', ''),                           # \appendix
        (r'\\section\*\{([^}]*)\}', r'## \1'),          # \section*{附录} → ## 附录
        (r'\\subsection\*\{([^}]*)\}', r'### \1'),      # \subsection*{...}
    ]
    for pattern, repl in latex_residuals:
        result = re.sub(pattern, repl, result)

    # Post-process: figure/table numbering
    result = re.sub(r'Figure (\d+):', r'**图\1**', result)
    result = re.sub(r'Table (\d+[a-z]?):', r'**表\1**', result)

    # Add sequential alt text to images: ![](url) → ![图N](url)
    _fig_idx = [0]
    def _number_image(m):
        _fig_idx[0] += 1
        return f'![图{_fig_idx[0]}]({m.group(1)})'
    result = re.sub(r'!\[\]\(([^)]+)\)', _number_image, result)
    # Fix subsection headings: MinerU outputs all headings as ## regardless of level
    # ## X.Y → ### X.Y (subsection)
    result = re.sub(r'^## (\d+\.\d+)', r'### \1', result, flags=re.MULTILINE)
    # ## X.Y.Z → #### X.Y.Z (sub-subsection)
    result = re.sub(r'^### (\d+\.\d+\.\d+)', r'#### \1', result, flags=re.MULTILINE)

    # Add 2-char indent to paragraph lines (Chinese academic style)
    # Skip: headings, images, formulas, tables, code, empty lines, list items, blockquotes
    indent_skip = re.compile(
        r'^('
        r'#{1,6}\s'        # heading
        r'|>\s?'           # blockquote
        r'|-\s'            # unordered list (- item)
        r'|\|\s'           # table row
        r'|`{3}'           # code fence
        r'|!\['            # image
        r'|\$\$'           # block formula
        r'|\d+\.\s'        # ordered list
        r'|\*\*图\d+\*\*'  # figure caption: **图1**
        r'|\*\*表\d+\*\*'  # table caption: **表1**
        r')'
    )
    lines = result.split('\n')
    in_code_block = False
    in_table = False
    indented = []
    for line in lines:
        if line.startswith('```'):
            in_code_block = not in_code_block
            indented.append(line)
            continue
        if in_code_block:
            indented.append(line)
            continue
        # Track HTML/table blocks
        if line.startswith('<table') or line.startswith('|'):
            in_table = True
        if in_table and (line.startswith('</table>') or (not line.startswith('|') and line.strip())):
            if line.startswith('</table>'):
                indented.append(line)
                in_table = False
                continue
            in_table = False
        if in_table:
            indented.append(line)
            continue
        # Skip empty lines and special lines
        if not line.strip() or indent_skip.match(line):
            indented.append(line)
            continue
        # Regular paragraph line → add indent
        indented.append('\u3000\u3000' + line)
    result = '\n'.join(indented)

    # Add English title under the translated H1 for reference
    if eng_title:
        h1_match = re.match(r'^(#\s+.+)$', result, re.MULTILINE)
        if h1_match:
            old_h1 = h1_match.group(1)
            new_h1 = f"{old_h1}\n\n> {eng_title}"
            result = result.replace(old_h1, new_h1, 1)

    return result


# ── Step 4: Upload images to Yuque CDN ────────────────

def _upload_one_image(img_bytes: bytes, filename: str, is_jpg: bool, ctoken: str, cookie_str: str, yuque_repo: str) -> str | None:
    """Upload a single image to Yuque CDN. Returns CDN URL or None."""
    # Convert JPG→PNG
    if is_jpg:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(img_bytes))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            img_bytes = buf.getvalue()
        except Exception:
            pass

    try:
        upload_resp = httpx.post(
            f"https://www.yuque.com/api/upload/attach?ctoken={ctoken}",
            headers={"Cookie": cookie_str, "Referer": f"https://www.yuque.com/{yuque_repo}", "Origin": "https://www.yuque.com"},
            files={"file": (filename, img_bytes, "image/png")},
            data={"type": "image"},
            timeout=30,
        )
        if upload_resp.status_code == 200:
            filekey = upload_resp.json()["data"]["filekey"]
            return f"https://cdn.nlark.com/{filekey}"
        else:
            logger.warning("  Upload failed: %d %s", upload_resp.status_code, upload_resp.text[:100])
            return None
    except Exception as e:
        logger.warning("  Upload error: %s", e)
        return None


def upload_images_to_yuque(md: str, image_paths: list[str], cfg: dict, md_dir: str = None) -> str:
    """Upload images from ZIP to Yuque CDN, replace URLs in markdown.

    Handles two image reference formats in MinerU markdown:
    - Relative paths: ![...](images/xxx.jpg) — from ZIP's full.md
    - Absolute CDN URLs: ![...](https://cdn-mineru.openxlab.org.cn/...) — from web API

    image_paths: list of absolute local file paths to images extracted from ZIP.
    md_dir: directory of the source full.md (for resolving relative paths).
    """
    cookies_data = json.load(open(cfg["cookies_path"], encoding="utf-8"))
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies_data)
    ctoken = next((c["value"] for c in cookies_data if c["name"] == "yuque_ctoken"), "")

    # Build filename→local_path map
    name_to_path = {}
    for p in image_paths:
        name_to_path[os.path.basename(p)] = p

    # Find ALL image references in markdown
    img_refs = re.findall(r'(!\[[^\]]*\]\()([^)]+)(\))', md)
    total = len(img_refs)
    logger.info("Found %d image references in markdown", total)

    replaced = 0
    for i, (prefix, ref, suffix) in enumerate(img_refs, 1):
        # Determine image filename from reference
        if ref.startswith('http'):
            # Absolute URL — extract filename from URL path
            filename = ref.split('/')[-1]
            # Also try to download from CDN as fallback
        else:
            # Relative path like "images/xxx.jpg"
            filename = os.path.basename(ref)

        # Find local file
        local_path = name_to_path.get(filename)
        if not local_path:
            # Try matching by base name (without extension)
            base = filename.rsplit('.', 1)[0]
            for name, path in name_to_path.items():
                if base in name:
                    local_path = path
                    break

        if local_path:
            img_bytes = Path(local_path).read_bytes()
            is_jpg = local_path.endswith(('.jpg', '.jpeg'))
        elif ref.startswith('http'):
            # Fallback: try downloading from CDN
            logger.warning("  Image %d: no local file, trying CDN download: %s", i, ref[:60])
            try:
                resp = httpx.get(ref, timeout=15, follow_redirects=True)
                if resp.status_code == 200:
                    img_bytes = resp.content
                    is_jpg = '.jpg' in ref or 'jpeg' in resp.headers.get('content-type', '')
                else:
                    logger.warning("  CDN download failed: %d", resp.status_code)
                    continue
            except Exception as e:
                logger.warning("  CDN download error: %s", e)
                continue
        else:
            # Try resolving relative path with md_dir
            if md_dir:
                abs_path = os.path.join(md_dir, ref)
                if os.path.exists(abs_path):
                    img_bytes = Path(abs_path).read_bytes()
                    is_jpg = abs_path.endswith(('.jpg', '.jpeg'))
                    local_path = abs_path
                else:
                    logger.warning("  Image %d: file not found: %s", i, ref)
                    continue
            else:
                logger.warning("  Image %d: cannot resolve: %s", i, ref)
                continue

        # Upload
        upload_name = f"paper_fig_{i}.png"
        cdn_url = _upload_one_image(img_bytes, upload_name, is_jpg, ctoken, cookie_str, cfg['yuque_repo'])
        if cdn_url:
            # Replace the original reference with CDN URL
            old_ref = f"{prefix}{ref}{suffix}"
            new_ref = f"{prefix}{cdn_url}{suffix}"
            md = md.replace(old_ref, new_ref, 1)
            replaced += 1
            logger.info("  Image %d/%d uploaded → %s", i, total, cdn_url[:60])

    logger.info("Replaced %d/%d images", replaced, total)
    return md


# ── Step 5: Publish to Yuque ──────────────────────────

def publish_to_yuque(md_path: str, title: str, cfg: dict, update_slug: str = None) -> str:
    """Publish via yuque-docs-skill CLI. Returns doc URL."""
    skill_cli = os.path.join(os.path.expanduser("~"), ".codex", "skills", "yuque-docs-skill", "scripts", "yuque_cli.py")
    if not os.path.exists(skill_cli):
        raise FileNotFoundError(f"yuque_cli.py not found at {skill_cli}")

    if update_slug:
        cmd = f'python "{skill_cli}" update {update_slug} -f "{md_path}" --format markdown'
    else:
        cmd = f'python "{skill_cli}" create -t "{title}" -f "{md_path}" --format markdown'

    logger.info("Publishing: %s", cmd[:80])
    import subprocess
    result = subprocess.run(cmd, shell=True, capture_output=True, timeout=30, encoding='utf-8', errors='replace')
    output = (result.stdout or "").strip()
    logger.info("Publish result: %s", output)

    # Extract slug from output
    slug_match = re.search(r'slug=(\S+)', output)
    slug = slug_match.group(1) if slug_match else update_slug or ""
    return f"https://www.yuque.com/{cfg['yuque_repo']}/{slug}"


# ── Main Pipeline ──────────────────────────────────────

def extract_paper_prefix(source: str) -> str:
    """Extract conference+year or arXiv ID from URL/path.
    
    Formats:
      - ACL Anthology: "EMNLP24", "ACL25", "NAACL24", "FINDINGS-EMNLP23", etc.
      - arXiv: "2410.01627"
      - Unknown: ""
    """
    # arXiv ID from URL or filename
    arxiv_match = re.search(r'(\d{4}\.\d{4,5})', source)
    if arxiv_match:
        return arxiv_match.group(1)
    
    # ACL Anthology: https://aclanthology.org/2024.emnlp-industry.114/
    # Format: YEAR.CONF[-VARIANT].ID
    acl_match = re.search(r'aclanthology\.org/(\d{4})\.([a-z]+?)(?:-([a-z\d]+?))?\.(\d+)', source)
    if acl_match:
        year_short = acl_match.group(1)[2:]  # "2024" → "24"
        conf = acl_match.group(2).upper()     # "emnlp" → "EMNLP"
        variant = acl_match.group(3)          # "industry", "long", etc.
        if variant:
            # findings-emnlp → "FINDINGS-EMNLP23", emnlp-industry → "EMNLP-INDUSTRY24"
            return f"{conf}-{variant.upper()}{year_short}"
        return f"{conf}{year_short}"
    
    # ACL Anthology alternate format: /2026.customnlp4u-1.8/
    # (conference name contains numbers/dots, different dot structure)
    acl_match2 = re.search(r'aclanthology\.org/(\d{4})\.([a-z\d]+)-(\d+\.\d+)', source)
    if acl_match2:
        year_short = acl_match2.group(1)[2:]
        conf = acl_match2.group(2).upper()
        return f"{conf}{year_short}"
    
    return ""


def extract_english_title(md: str) -> str:
    """Extract the English title from MinerU markdown (first # heading)."""
    m = re.search(r'^#\s+(.+)$', md, re.MULTILINE)
    if m:
        title = m.group(1).strip()
        # Clean up common artifacts
        title = re.sub(r'\s+', ' ', title)
        return title
    return ""


def format_title(source: str, md: str, user_title: str = None) -> str:
    """Build Yuque doc title in format: PREFIX | English Title.
    
    PREFIX = Conference+Year (e.g. EMNLP24) or arXiv ID (e.g. 2410.01627).
    """
    prefix = extract_paper_prefix(source)
    eng_title = extract_english_title(md)
    
    if user_title:
        # User explicitly provided title
        if " | " in user_title:
            return user_title
        if prefix:
            return f"{prefix} | {user_title}"
        return user_title
    
    # Auto-detect: use English title from markdown
    if prefix and eng_title:
        return f"{prefix} | {eng_title}"
    elif eng_title:
        return eng_title
    elif prefix:
        return prefix
    else:
        # Fallback: use filename
        return Path(source).stem if not source.startswith("http") else source.split("/")[-1]


def run_pipeline(source: str, title: str = None, skip_translate: bool = False,
                 skip_publish: bool = False, env_path: str = ".env",
                 output_dir: str = "./output", update_slug: str = None):
    cfg = load_env(env_path)

    os.makedirs(output_dir, exist_ok=True)
    logger.info("=" * 50)
    logger.info("Pipeline: %s → Yuque", source[:60])
    logger.info("=" * 50)

    # Step 1: Submit to MinerU
    logger.info("[1/5] Submitting to MinerU...")
    task_id = submit_mineru_task(source, cfg)

    # Step 2: Poll & download
    logger.info("[2/5] Waiting for MinerU parsing...")
    task_info = poll_mineru_task(task_id, cfg)
    zip_url = task_info.get("full_zip_url")
    if not zip_url:
        raise RuntimeError("No zip_url in task result")

    md_path, image_paths = download_and_extract(zip_url, output_dir)
    if not md_path:
        raise RuntimeError("No full.md found in ZIP")

    md = open(md_path, encoding="utf-8").read()
    md_dir = os.path.dirname(md_path)
    logger.info("  Markdown: %d chars, Images: %d", len(md), len(image_paths))

    # Build title from source + original markdown content (before translation)
    title = format_title(source, md, title)
    eng_title = extract_english_title(md)
    logger.info("  Title: %s", title)

    # Step 3: Translate
    if skip_translate:
        logger.info("[3/5] Skipping translation")
    else:
        logger.info("[3/5] Translating to Chinese...")
        md = translate_markdown(md, cfg, eng_title=eng_title)

    # Step 4: Upload images
    logger.info("[4/5] Uploading images to Yuque CDN...")
    md = upload_images_to_yuque(md, image_paths, cfg, md_dir)

    # Save final .md
    final_path = os.path.join(output_dir, f"{Path(source).stem}_translated.md" if not source.startswith("http") else f"paper_{task_id[:8]}_translated.md")
    with open(final_path, "w", encoding="utf-8") as f:
        f.write(md)
    logger.info("  Saved: %s (%d chars)", final_path, len(md))

    # Step 5: Publish
    if skip_publish:
        logger.info("[5/5] Skipping publish")
        return final_path
    else:
        logger.info("[5/5] Publishing to Yuque...")
        url = publish_to_yuque(final_path, title, cfg, update_slug)
        logger.info("=" * 50)
        logger.info("✅ Done! URL: %s", url)
        logger.info("=" * 50)
        return url


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PDF → MinerU → Translate → Yuque")
    parser.add_argument("source", help="PDF URL or local file path")
    parser.add_argument("--title", default=None, help="Document title")
    parser.add_argument("--skip-translate", action="store_true", help="Skip translation")
    parser.add_argument("--skip-publish", action="store_true", help="Skip Yuque publish")
    parser.add_argument("--env", default=".env", help=".env file path")
    parser.add_argument("--output-dir", default="./output", help="Output directory")
    parser.add_argument("--update", default=None, help="Yuque doc slug to update")
    args = parser.parse_args()

    run_pipeline(
        source=args.source,
        title=args.title,
        skip_translate=args.skip_translate,
        skip_publish=args.skip_publish,
        env_path=args.env,
        output_dir=args.output_dir,
        update_slug=args.update,
    )
