"""Generic post-processing for MinerU markdown output from pdf2zh translated PDFs.
All rules are paper-agnostic — no hardcoded headings or manual fixes."""
import re


# ── Configuration ──────────────────────────────────────────────
# Default paths — override via CLI args or direct assignment
import sys
INPUT  = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\12608\pdf2zh-test\2410.01627_mineru_clean.md"
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else INPUT.replace('_mineru_clean.md', '_yuque.md')
# Optional: English title for bilingual header (auto-detected from PDF filename if not provided)
ENGLISH_TITLE = sys.argv[3] if len(sys.argv) > 3 else None

# ── Paper-specific heading phrases (optional) ─────────────────
# Add paper-specific heading texts here if the generic split logic
# doesn't correctly identify them. The list is checked in order,
# longest match first. For most papers, the generic keywords below
# are sufficient.
PAPER_HEADING_PHRASES = [
    # Add paper-specific heading texts here if the generic split logic
    # doesn't correctly identify them. The list is checked in order,
    # longest match first. For most papers, the generic keywords are sufficient.
    #
    # When MinerU merges heading+body without any delimiter (no period, no space),
    # generic heuristics cannot find the split boundary. Adding the heading text
    # here tells the script exactly where to split.
    #
    # Example (arXiv 2410.01627):
    # '使用大语言模型的自适应上下文学习与基于思维链的意图检测',
    # '利用大语言模型进行意图检测',
    # '使用大语言模型内部表示进行OOS检测',
    # '分析大语言模型的OOS检测能力',
    # '基于不确定性的查询路由',
    # '微调句子转换器',
    # '大语言模型与OOS检测',
]

SENTENCE_END_PUNCTUATION = set('。！？：；"")）】》.!?」』—')
CJK_RANGE = (0x4e00, 0x9fff)

# ── Helpers ────────────────────────────────────────────────────
def is_cjk(ch):
    return CJK_RANGE[0] <= ord(ch) <= CJK_RANGE[1]

def starts_with_cjk(text):
    return text and is_cjk(text[0])

def is_sentence_end(text):
    """True if the stripped text ends with sentence-ending punctuation."""
    t = text.rstrip()
    return t and t[-1] in SENTENCE_END_PUNCTUATION

def is_skippable(stripped):
    """Lines that are layout artifacts (images, tables, captions) —
    not part of paragraph text flow."""
    if not stripped:
        return True
    if stripped.startswith('<!-- image'):
        return True
    if stripped.startswith('<table') or stripped.startswith('</table'):
        return True
    if stripped.startswith('图') and re.match(r'^图\s*\d', stripped):
        return True
    if stripped.startswith('表') and re.match(r'^表\s*\d', stripped):
        return True
    return False


# ── Step 1: Merge broken heading fragments ─────────────────────
# MinerU sometimes splits one heading across two ## lines:
#   "## 3.1.2 使用大语言模型的自适应上下文学"
#   "## 习 +基于思维链的意图检测图2 展示了..."
# The second line may also contain body text after the heading continuation.
# Rule: if two consecutive ## lines appear and the second has NO numbered prefix,
# merge the heading continuation text (up to the first sentence boundary) into
# the first heading, and keep any body text as a separate paragraph.
def merge_heading_fragments(text):
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith('#') and not stripped.startswith('#!'):
            # Look ahead: is the next non-blank line also a heading with no number prefix?
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                next_stripped = lines[j].strip()
                if next_stripped.startswith('#') and not next_stripped.startswith('#!'):
                    # Extract heading text (without # markers)
                    next_title = next_stripped.lstrip('#').strip()
                    # Check if it has a numbered prefix (1, 1.1, A.1, etc.)
                    has_number_prefix = bool(re.match(r'^(\d+[\.\d]*|[A-Z]\.\d+)', next_title))
                    if not has_number_prefix:
                        # The second line is a heading continuation.
                        # But it may also contain body text (e.g., "习 +基于...图2 展示了...")
                        # Find where the heading text ends and body text begins.
                        # Heuristic: heading text ends at the first period/comma or
                        # before common body starters like 图\d, 表\d, 我们, etc.
                        heading_end = len(next_title)
                        for k, ch in enumerate(next_title):
                            if ch in '。！？，；' and k > 1:
                                heading_end = k
                                break
                        
                        # Also check for body starters
                        for starter in ['图1', '图2', '图3', '图4', '图5', '表1', '表2',
                                       '我们', '在', '传统', '具有', '通过']:
                            idx = next_title.find(starter)
                            if 2 <= idx < heading_end:
                                heading_end = idx
                                break
                        
                        heading_continuation = next_title[:heading_end]
                        body_text = next_title[heading_end:].lstrip('，。！？； ')
                        
                        # Merge heading continuation into the first heading
                        current = lines[i].rstrip().rstrip('#').rstrip()
                        result.append(current + heading_continuation)
                        
                        # Add body text as a separate paragraph if present
                        if body_text:
                            for k in range(i + 1, j):
                                result.append('')
                            result.append('')  # blank line after heading
                            result.append(body_text)
                        else:
                            for k in range(i + 1, j + 1):
                                result.append('')
                        
                        i = j + 1
                        continue
        result.append(lines[i])
        i += 1
    return '\n'.join(result)


# ── Step 2: Split heading + body merged lines ──────────────────
# MinerU outputs "## 摘要意图检测是..." — heading and first paragraph merged.
# Key insight: a pure heading line is SHORT and has NO sentence-ending punctuation (。！？).
# If a ## line contains 。！？ or is very long, the text after the heading phrase is body.
def split_heading_body(text):
    lines = text.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if not m:
            result.append(line)
            continue
        
        hashes = m.group(1)
        title = m.group(2)
        
        # Short lines without sentence-ending punctuation are probably pure headings
        # BUT: we need to check if there's body text merged after the heading.
        # A heading is "pure" only if it's SHORT AND the text looks like a complete heading.
        # Heuristic: if there's a space followed by 5+ chars of non-heading text after
        # the first few words, it's probably merged.
        has_sentence_end = any(c in title for c in '。！？；')
        
        # Check for body text after heading: look for space + sentence-like content
        # e.g., "知识密集型任务 此类别包含来自 GAIA" → has body after space
        # Also check for no-space merges where heading is very long (>30 chars without punctuation)
        has_merged_body = False
        if not has_sentence_end and len(title) > 8:
            # Method A: Find spaces that might separate heading from body
            space_positions = [i for i, ch in enumerate(title) if ch == ' ']
            for sp in space_positions:
                after_space = title[sp+1:]
                if len(after_space) > 5:
                    has_merged_body = True
                    break
            
            # Method B: Very long title without sentence-end punctuation is likely merged
            # Most real headings are <20 chars. If >35 chars and no period, it's probably merged.
            if not has_merged_body and len(title) > 35:
                has_merged_body = True
        
        if len(title) <= 25 and not has_sentence_end and not has_merged_body:
            result.append(line)
            continue
        
        # This heading likely has body text merged in.
        # Strategy: find the boundary between heading text and body text.
        # The heading text ends at the first "sentence start" after a short phrase.
        
        # Try splitting at numbered prefix boundary:
        # "1 引言任务导向型..." → split after "1 引言"
        # "3.1.2 使用大语言模型的自适应上下文学习 +基于..." → split after "学习"
        # "摘要意图检测是..." → split after "摘要"
        
        split_pos = None
        
        # Method 1: For numbered headings, find the boundary after the short title phrase
        # Support both digit-numbered (1, 1.1, 3.1.2) and appendix-numbered (A.1, A.2)
        num_prefix = re.match(r'^(\d+(?:\.\d+)*|[A-Z]\.\d+)\s+', title)
        if num_prefix:
            # After the number, the heading phrase is typically 2-10 chars.
            # Strategy: extract the "title phrase" by looking for known patterns.
            after_num = title[num_prefix.end():]
            
            # First try: match against known heading phrases.
            # Combine paper-specific phrases with generic academic keywords.
            # Paper-specific entries are listed first (longest match wins).
            heading_phrases = list(PAPER_HEADING_PHRASES) + [
                # Generic 4+ char academic headings
                '相关工作', '局限性', '伦理声明', '参考文献',
                '方法论', '数据集', '实验设置', '实验与结果',
                '未来工作', '方法概述', '研究背景', '主要贡献',
                # Generic 3-char headings
                '实验', '结果', '分析', '设置', '讨论',
                # Generic 2-char headings (handled carefully below)
                '引言', '结论', '摘要', '附录', '方法', '背景', '概述', '总结',
            ]
            for phrase in heading_phrases:
                if after_num.startswith(phrase):
                    rest = after_num[len(phrase):]
                    # Check if the matched phrase is the complete heading:
                    # - rest is empty → pure heading
                    # - rest starts with punctuation/space → heading + more title text
                    # - rest starts with body text → need to check if phrase is too short
                    if not rest:
                        split_pos = num_prefix.end() + len(phrase)
                        break
                    # If rest starts with a body-text pattern, this phrase was too short
                    # (e.g., "分析" matched "分析大语言模型..." but rest continues the title)
                    # Heuristic: if the next char after phrase is CJK and the phrase is <=2 chars,
                    # it's probably too short — skip this match
                    # Exception: very common heading words like 引言, 结论, 摘要
                    COMMON_HEADINGS_2CHAR = {'引言', '结论', '摘要', '附录', '方法', '结果', '数据', '讨论', '背景'}
                    if len(phrase) <= 2 and rest[0] not in ' \t，。！？、：；' and is_cjk(rest[0]) and phrase not in COMMON_HEADINGS_2CHAR:
                        continue
                    # Otherwise, split after the phrase
                    split_pos = num_prefix.end() + len(phrase)
                    break
            
            # Second try: split at the first period/comma in the title (after the number prefix)
            # This is the most reliable indicator of heading/body boundary
            if split_pos is None:
                for i, ch in enumerate(title):
                    if ch in '。！？' and i > num_prefix.end():
                        split_pos = i
                        break
            
            # Third try: look for body-text starters that appear right after the title
            if split_pos is None:
                # Only look for starters that appear close to the number prefix (2-15 chars)
                body_starters = [
                    '在本节', '在本', '我们', '传统', '具有', '通过', '基于', '随着', '由于',
                    '此外', '最近', '尽管', '总体', '因此', '然而', '为了', '本文',
                    '图2', '图3', '图4', '图5', '表1', '表2',
                    '来自', '第',
                ]
                for starter in body_starters:
                    idx = after_num.find(starter)
                    if 2 <= idx <= 15:
                        split_pos = num_prefix.end() + idx
                        break
            
            # Fourth try: split at comma
            if split_pos is None:
                for i, ch in enumerate(title):
                    if ch in '，；' and i > num_prefix.end():
                        split_pos = i
                        break
        
        # Method 2: For non-numbered headings (摘要, 结论, 知识密集型任务, etc.)
        if split_pos is None:
            section_keywords = [
                # 5+ char common headings
                '相关工作', '局限性', '伦理声明', '伦理考量', '参考文献', '方法概述',
                '方法论', '数据集', '实验设置', '实验与结果', '实验与讨论',
                '未来工作', '研究背景', '主要贡献', '任务定义', '数据构建',
                '评估指标', '实现细节', '结果与分析', '进一步分析',
                # 4-char headings
                '实验', '结果', '分析', '设置', '讨论', '致谢', '附录',
                # Compound headings: N字+型+任务 (e.g., 知识密集型任务, 推理密集型任务)
                # These are sub-section headings that MinerU outputs as ##
                # Pattern: match "XX型任务" or "XX型数据"
                # 2-char headings
                '摘要', '结论', '方法', '背景', '概述', '总结',
            ]
            for kw in sorted(section_keywords, key=len, reverse=True):  # longest first
                if title.startswith(kw):
                    rest = title[len(kw):]
                    if rest and not rest[0] in ' \t':
                        split_pos = len(kw)
                    elif rest and rest[0] in ' \t' and len(rest.strip()) > 0:
                        # Has space after keyword but also has body text
                        split_pos = len(kw)
                    break
            
            # Special: "XX型任务" / "XX型数据" sub-section headings
            # e.g., "知识密集型任务 此类别包含来自 GAIA" → split after "知识密集型任务"
            if split_pos is None:
                type_task_match = re.match(r'^((?:[\u4e00-\u9fff]+型)(?:任务|数据|方法|模型))\s*(.+)$', title)
                if type_task_match:
                    heading_text = type_task_match.group(1)
                    rest = type_task_match.group(2)
                    if rest:
                        split_pos = len(heading_text)
        
        # Method 3: For other merged headings, split at first sentence boundary
        if split_pos is None and has_sentence_end:
            for i, ch in enumerate(title):
                if ch in '。！？' and i > 2:
                    split_pos = i
                    break
        
        if split_pos and split_pos < len(title):
            heading_part = title[:split_pos].rstrip('，。！？；： ')
            body_part = title[split_pos:].lstrip('，。！？；： ')
            result.append(f'{hashes} {heading_part}')
            result.append('')
            if body_part:
                result.append(body_part)
        else:
            result.append(line)
    
    return '\n'.join(result)


# ── Step 3: Fix heading levels ─────────────────────────────────
# Rule: \section = # (h1), so:
#   - Numbered: "1" → #, "1.1" → ##, "1.1.1" → ###
#   - Appendix: "A" → #, "A.1" → ##
#   - Unnumbered section-like: 摘要, 结论, 局限性, 参考文献 → #
def fix_heading_levels(text):
    UNNUMBERED_H1 = {'摘要', '结论', '局限性', '伦理声明', '伦理考量', '参考文献', '致谢', '附录'}
    
    def replacer(m):
        title = m.group(2).strip()
        
        # Numbered section: count dots
        num_match = re.match(r'^(\d+(?:\.\d+)*)\s', title)
        if num_match:
            dot_count = num_match.group(1).count('.')
            level = dot_count + 1  # 1→#, 1.1→##, 1.1.1→###
            return '#' * min(level, 6) + ' ' + title
        
        # Appendix letter: "A 大语言模型的使用" → #, "A.1 ..." → ##
        if re.match(r'^A\s+附录', title) or re.match(r'^A\s*$', title):
            return '# ' + title
        if re.match(r'^[A-Z]\.\d+', title):
            return '## ' + title
        # Standalone appendix letter + title: "A 大语言模型的使用", "B 评估详情", etc.
        if re.match(r'^[A-Z]\s+\S', title) and not re.match(r'^[A-Z]\.\d+', title):
            # This is an appendix section (like \section{A ...})
            return '# ' + title
        
        # Unnumbered H1 keywords
        # Check if the title starts with any of the H1 keywords
        for kw in UNNUMBERED_H1:
            if title == kw or title.startswith(kw + '。') or title.startswith(kw + ' '):
                return '# ' + title
        
        return m.group(0)
    
    return re.sub(r'^(#{2,6})\s+(.+)$', replacer, text, flags=re.MULTILINE)


# ── Step 4: Merge cross-page broken paragraphs ─────────────────
# Skip over images/tables/captions to find paragraph continuation.
def merge_broken_paragraphs(text):
    lines = text.split('\n')
    merged = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        
        # Headings stay as-is
        if stripped.startswith('#'):
            merged.append(line)
            i += 1
            continue
        
        # Skippable elements stay as-is
        if is_skippable(stripped):
            merged.append(line)
            i += 1
            continue
        
        # Short lines (< 5 chars) stay as-is (likely labels, numbers)
        if len(stripped) < 5:
            merged.append(line)
            i += 1
            continue
        
        # Regular text line. Look backwards past skippable lines
        # to find the previous text line.
        prev_text_idx = len(merged) - 1
        while prev_text_idx >= 0 and is_skippable(merged[prev_text_idx].strip()):
            prev_text_idx -= 1
        
        if prev_text_idx >= 0:
            prev_text = merged[prev_text_idx].rstrip()
            prev_stripped = prev_text.strip()
            
            # Only merge if previous line is text (not heading) and doesn't end a sentence
            if (prev_stripped and 
                not prev_stripped.startswith('#') and
                not is_sentence_end(prev_text)):
                # Merge current line into previous text line
                merged[prev_text_idx] = prev_text + stripped
                i += 1
                continue
        
        merged.append(line)
        i += 1
    
    return '\n'.join(merged)


# ── Step 5: Fix URLs broken into single-char lines ─────────────
# MinerU sometimes renders URLs as one character per line, or splits them
# across lines with the first char attached to the previous text.
def fix_broken_urls(text):
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        
        # Pattern 1: standalone 'h' on its own line (URL start)
        if len(stripped) == 1 and stripped == 'h':
            url_chars = [stripped]
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if not s:
                    j += 1
                    continue
                if len(s) == 1 and s.isprintable():
                    url_chars.append(s)
                    j += 1
                elif len(s) > 1:
                    # This could be the remainder of the URL (e.g., "s://www...")
                    url_chars.append(s)
                    j += 1
                    break
                else:
                    break
            candidate = ''.join(url_chars)
            if re.match(r'^https?://', candidate) and len(candidate) > 10:
                result.append(candidate)
                i = j
                continue
        
        # Pattern 2: 'h' at end of a text line (URL start merged with prev text)
        # e.g., "Gartner预测...。h\nt\nt\np\ns://www..."
        if stripped.endswith('h') and len(stripped) > 2:
            # Check if the next few lines are single chars that form a URL
            j = i + 1
            url_chars = []
            while j < len(lines) and j < i + 10:
                s = lines[j].strip()
                if not s:
                    j += 1
                    continue
                if len(s) == 1 and s.isprintable():
                    url_chars.append(s)
                    j += 1
                elif len(s) > 1:
                    url_chars.append(s)
                    j += 1
                    break
                else:
                    break
            candidate = 'h' + ''.join(url_chars)
            if re.match(r'^https?://', candidate) and len(candidate) > 10:
                # Remove trailing 'h' from current line, add URL as next line
                text_part = stripped[:-1]
                result.append(text_part)
                result.append(candidate)
                i = j
                continue
        
        result.append(lines[i])
        i += 1
    return '\n'.join(result)


# ── Step 6: Clean footnotes mixed into body text ───────────────
# pdf2zh renders superscript footnotes as inline numbers.
# Pattern 1: Chinese + digit + Chinese (e.g., "查询3缓存", "意图1，", "昂贵。3 因此")
# Pattern 2: English+digit+double-comma (e.g., "AID34,，")
# Strategy: remove the bare footnote digit, keeping the surrounding text intact.
def clean_inline_footnotes(text):
    # Pattern 1a: Chinese + period/comma + 1-2 digit(s) + space + CJK (sentence boundary)
    # e.g., "昂贵。3 因此" → "昂贵。因此"
    text = re.sub(r'([。！？，；])\s*(\d{1,2})\s+(?=[\u4e00-\u9fff])', r'\1', text)
    
    # Pattern 1b: CJK + digit + CJK where digit is NOT part of a quantity
    # e.g., "查询3缓存" → "查询缓存", "意图1，" → "意图，"
    # But preserve: "7个SOTA", "3个数据集", "50%更低"
    def footnote_remover(m):
        before = m.group(1)
        digit = m.group(2)
        after = m.group(3)
        # Skip figure/table references: "图1：", "表1：", "图2展示", etc.
        # Only skip if the digit clearly refers to a figure/table number
        # (followed by colon, "展", etc.), NOT just any punctuation
        if before in '图表' and after in '：:展示中里':
            return m.group(0)
        # Skip section references: "第3节", "第1章"
        if before == '第' and after in '节章部分条':
            return m.group(0)
        # Quantity suffixes indicate the digit is a real number, not a footnote
        quantity_suffixes = '个次年月日万千百亿倍条项种类台块名位篇部册套件场次轮版期步层组节章段行列排帧像素%'
        if after and after[0] in quantity_suffixes:
            return m.group(0)
        # If before ends with ASCII letter/digit, the digit is part of a name (e.g., "AID3")
        # Note: isalpha() returns True for CJK too, so we check ASCII range explicitly
        if before and (before[-1].isascii() and (before[-1].isalpha() or before[-1].isdigit())):
            return m.group(0)
        # Remove the footnote digit
        return before + after
    
    text = re.sub(r'([\u4e00-\u9fff])(\d{1,2})([\u4e00-\u9fff，。！？、；：%])', footnote_remover, text)
    
    # Pattern 2: Alphanumeric+digit + double punctuation (e.g., "AID34,，" → "AID3,")
    # Handles both ASCII and fullwidth punctuation
    text = re.sub(r'([A-Z]{2,}\d+)[,，]\s*[,，]', r'\1,', text)
    
    # Pattern 3: Named entity + extra digit at end (footnote merged into name)
    # e.g., "AID34," → "AID3," — the last digit before comma/period is likely a footnote
    # This handles cases where the double-comma was already reduced to single comma
    # Heuristic: if a known-pattern name (like AID3, HINT3) has an extra trailing digit
    # before punctuation, the extra digit is likely a footnote marker
    def entity_footnote_remover(m):
        name = m.group(1)  # e.g., "AID3"
        extra_digit = m.group(2)  # e.g., "4"
        punct = m.group(3)  # e.g., ","
        return name + punct
    
    text = re.sub(r'([A-Z]{2,}\d+)(\d)([,，。：])', entity_footnote_remover, text)
    
    return text

# ── Step 5.5: Fix display formula boundaries ────────────────────
# MinerU sometimes merges body text into $$ blocks:
#   $$formula\n$$正文被混入$$ → should be $$formula$$\n\n正文
# Pattern: closing $$ followed by CJK text on the same line
def fix_formula_boundaries(text):
    # Pattern: "$$\n正文" where the closing $$ is on its own line but
    # immediately followed by CJK body text (should be separated)
    # e.g., "$$formula\n$$模型不确定F1..." → "$$formula$$\n\n模型不确定F1..."
    text = re.sub(r'\$\$\n(\$\$)([\u4e00-\u9fff])', r'$$\n\n\2', text)
    
    # Pattern: "$$公式\n$$正文" — closing $$ at start of line with CJK body after it
    # This happens when MinerU puts $$ + body text on the same line
    text = re.sub(r'\n\$\$([\u4e00-\u9fff])', r'\n$$\n\n\1', text)
    
    return text


# ── Step 6.5: Split inline section markers ─────────────────────
# MinerU sometimes renders section markers like "贡献。" inline with body text.
# e.g., "...能力。贡献。1. 我们采用..." → "...能力。\n\n**贡献。**\n\n1. 我们采用..."
# Generic pattern: a short Chinese noun+period that appears mid-paragraph,
# followed by a numbered list item.
def split_inline_markers(text):
    # Common section marker words that should be standalone
    marker_words = ['贡献', '主要贡献', '创新点', '关键贡献', '我们的贡献', '方法概述']
    
    for word in marker_words:
        pattern = word + '。'
        # Replace inline occurrences: preceded by any text, followed by numbered item
        # The preceding character can be CJK, punctuation, or space
        text = re.sub(
            rf'([^\n])({re.escape(pattern)})\s*(\d+\.)',
            rf'\1\n\n**\2**\n\n\3',
            text
        )
    
    return text


# ── Step 7: Clean heading-body artifacts ───────────────────────
def clean_artifacts(text):
    # Remove leading period after headings: "## X\n\n。" → "## X\n\n"
    # Also handle: "## X\n\n。 对于..." → "## X\n\n对于..."
    text = re.sub(r'(\n#[^\n]+\n\n)\s*。\s*', r'\1', text)
    
    # Remove references section (per project rule: "参考文献可以直接去掉")
    # BUT keep appendices that come after references — only remove the references section itself.
    # Find "# 参考文献" heading and remove until the next section-level heading (H1 or appendix).
    ref_match = re.search(r'^#\s+参考文献\s*\n', text, re.MULTILINE)
    if ref_match:
        after_ref = text[ref_match.end():]
        # Find the next H1 heading (including appendix like "# A ...")
        next_h1 = re.search(r'^#\s+', after_ref, re.MULTILINE)
        if next_h1:
            text = text[:ref_match.start()] + after_ref[next_h1.start():]
        else:
            # No more sections after references — remove to end
            text = text[:ref_match.start()]
    
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # Strip trailing whitespace per line
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    
    return text


# ── Step 8: Add Chinese paragraph indent ───────────────────────
def add_paragraph_indent(text):
    lines = text.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        if (stripped and
            not stripped.startswith('#') and
            not stripped.startswith('<!-- image') and
            not stripped.startswith('<table') and
            not stripped.startswith('</table') and
            not stripped.startswith('图') and
            not stripped.startswith('表') and
            not stripped.startswith('- ') and
            not stripped.startswith('> ') and
            not stripped.startswith('^') and  # footnote markers
            not re.match(r'^\d+\.\s', stripped) and
            starts_with_cjk(stripped) and
            len(stripped) > 15):
            line = '　　' + stripped
        result.append(line)
    return '\n'.join(result)


# ── Step 9: Add bilingual title ────────────────────────────────
def add_bilingual_title(text, english_title=None):
    """Add English title as a blockquote after the Chinese H1 title.
    If english_title is not provided, skip this step (title will be Chinese-only).
    """
    if not english_title:
        return text
    
    # Extract the Chinese title from the first H1
    m = re.match(r'^#\s+(.+)$', text, re.MULTILINE)
    if not m:
        return text
    chinese_title = m.group(1).strip()
    
    # Remove the original H1 and add bilingual version
    text = re.sub(r'^#\s+' + re.escape(chinese_title) + r'\s*\n*', '', text)
    text = f'# {chinese_title}\n\n> {english_title}\n\n' + text.lstrip()
    
    return text


# ── Main pipeline ──────────────────────────────────────────────
with open(INPUT, 'r', encoding='utf-8') as f:
    text = f.read()

print(f"Input: {len(text)} chars, {len(text.splitlines())} lines")

text = merge_heading_fragments(text)
text = split_heading_body(text)
text = fix_heading_levels(text)
text = merge_broken_paragraphs(text)
text = fix_broken_urls(text)
text = fix_formula_boundaries(text)
text = clean_inline_footnotes(text)
text = split_inline_markers(text)
text = clean_artifacts(text)
text = add_paragraph_indent(text)
text = add_bilingual_title(text, ENGLISH_TITLE)

with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write(text)

print(f"Output: {len(text)} chars, {len(text.splitlines())} lines")

# ── Validation ─────────────────────────────────────────────────
print("\n=== HEADING STRUCTURE ===")
for line in text.splitlines():
    if line.startswith('#'):
        print(f'  {line.strip()[:80]}')

print("\n=== STATS ===")
print(f'  Total lines: {len(text.splitlines())}')
print(f'  Total chars: {len(text)}')
print(f'  Heading count: {sum(1 for l in text.splitlines() if l.startswith("#"))}')
print(f'  Chinese indented paragraphs: {sum(1 for l in text.splitlines() if l.startswith("　　"))}')
print(f'  Image placeholders: {sum(1 for l in text.splitlines() if "<!-- image" in l)}')
print(f'  Tables: {sum(1 for l in text.splitlines() if "<table" in l)}')

# Check for common issues
issues = []
for line in text.splitlines():
    s = line.strip()
    if len(s) == 1 and s in 'htp':
        issues.append('Single-char URL fragment found')
        break
if issues:
    print("\n=== ISSUES ===")
    for issue in issues:
        print(f'  ⚠️ {issue}')
