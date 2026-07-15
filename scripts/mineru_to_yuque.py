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

SYSTEM_PROMPT = """As an academic expert with specialized knowledge in various fields, provide a proficient and precise translation from English to Chinese of the academic text enclosed in 🔤.

Translate for fluency and accuracy in Chinese academic writing style, not word-by-word. Preserve all technical terms with their first occurrence annotated as: 中文术语（English Term）.

严格规则：
1. 所有 LaTeX 公式（$...$ 和 $$...$$）必须原样保留，不做任何修改
2. 所有 HTML 表格（<table>...</table>）不要翻译，原样保留
3. 所有图片标记保持原样
4. 代码块 ```...``` 内容不翻译
5. Markdown 标题层级结构严格保持不变
6. 只输出翻译后的 Markdown，不要添加任何解释或 🔤 标记
7. 不要在输出中包裹 ```markdown 代码围栏
8. ## 1 Introduction → ## 1 引言，保持编号
9. Figure X: → **图X**，Table X: → **表X**"""


def translate_markdown(md: str, cfg: dict) -> str:
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

    # Chunk
    max_chunk = 4000
    chunks = []
    i = 0
    while i < len(main_text):
        # Try to break at paragraph boundary
        end = min(i + max_chunk, len(main_text))
        if end < len(main_text):
            # Find last double newline
            last_break = main_text.rfind('\n\n', i, end)
            if last_break > i:
                end = last_break + 2
        chunks.append(main_text[i:end])
        i = end

    logger.info("Translating %d chunks...", len(chunks))

    translated = []
    for idx, chunk in enumerate(chunks):
        overlap = translated[-1][-200:] if translated else ""
        user_msg = f"🔤 {chunk} 🔤"
        if overlap:
            user_msg = f"前文末尾（仅供上下文参考，不翻译）：---\n{overlap}\n---\n\n请翻译：\n🔤 {chunk} 🔤"

        # Retry up to 5 times with increasing timeout and backoff
        text = None
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
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                logger.warning("  Chunk %d attempt %d timed out (%ds): %s", idx + 1, attempt + 1, timeout, e)
                if attempt == 4:
                    raise
                import time; time.sleep(10)
                logger.info("  Retrying...")

        translated.append(text)
        logger.info("  Chunk %d/%d done (%d chars)", idx + 1, len(chunks), len(text))

    result = "\n\n".join(translated)

    # Post-process: figure/table numbering
    result = re.sub(r'Figure (\d+):', r'**图\1**', result)
    result = re.sub(r'Table (\d+[a-z]?):', r'**表\1**', result)
    # Fix subsection headings: ## 2.1 → ### 2.1
    result = re.sub(r'^## (\d+\.\d+)', r'### \1', result, flags=re.MULTILINE)

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

def run_pipeline(source: str, title: str = None, skip_translate: bool = False,
                 skip_publish: bool = False, env_path: str = ".env",
                 output_dir: str = "./output", update_slug: str = None):
    cfg = load_env(env_path)

    if not title:
        title = Path(source).stem if not source.startswith("http") else source.split("/")[-1]

    os.makedirs(output_dir, exist_ok=True)
    logger.info("=" * 50)
    logger.info("Pipeline: %s → Yuque", title)
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

    # Step 3: Translate
    if skip_translate:
        logger.info("[3/5] Skipping translation")
    else:
        logger.info("[3/5] Translating to Chinese...")
        md = translate_markdown(md, cfg)

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
