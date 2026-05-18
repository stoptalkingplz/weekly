from pathlib import Path

import builtins
import sys
import os
import re
import json
import time
import uuid
import tempfile
import traceback
from datetime import datetime, timedelta
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from openai import OpenAI
from zenv import get_zdkit_env
from zdbase import ZFile  # 保留平台兼容；本脚本主体不直接依赖


# =============================================================================
# 日志编码兜底
# =============================================================================
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def safe_log_text(text):
    text = str(text or "")
    try:
        text.encode("utf-8")
        return text
    except Exception:
        return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


# =============================================================================
# print flush patch
# =============================================================================
if not getattr(builtins.print, "_patched_flush", False):
    _original_print = builtins.print

    def print(*args, **kwargs):
        kwargs.setdefault("flush", True)
        _original_print(*args, **kwargs)

    print._patched_flush = True
    builtins.print = print


# =============================================================================
# 全局配置加载
# =============================================================================
zenv_obj = get_zdkit_env()
BASE_URL = zenv_obj.zdkit._http_client.config.get("url")

try:
    with open(config_file.path, "r", encoding="utf-8") as config_fp:
        config = json.load(config_fp)
except Exception as e:
    print(f"❌ 配置文件读取失败: {e}")
    raise

AK = config.get("ak")
SK = config.get("sk")
ORG_GUID = config.get("org_guid")
USER_GUID = config.get("user_guid")
projects = config.get("projects", [])


# =============================================================================
# 配置读取：保持和原子池周报脚本一致
# =============================================================================
def get_llm_config(key, default=None):
    """
    LLM 配置优先级：
    1. config["llm_global"][key]
    2. 兼容旧字段 config["llm_xxx"]
    3. default
    """
    llm_global = config.get("llm_global", {}) or {}
    if llm_global.get(key) is not None:
        return llm_global.get(key)

    legacy_key_map = {
        "base_url": "llm_base_url",
        "api_key": "llm_api_key",
        "model": "llm_model",
        "temperature": "llm_temperature",
        "max_tokens": "llm_max_tokens",
        "print_stream": "llm_print_stream",
        "max_retries": "llm_max_retries",
    }
    legacy_key = legacy_key_map.get(key)
    if legacy_key and config.get(legacy_key) is not None:
        return config.get(legacy_key)

    return default


def get_weekly_config(project, key, default=None):
    """
    周报配置优先级：
    1. project[key]
    2. config["weekly_global"][key]
    3. config[key] 兼容旧结构
    4. default
    """
    if project and project.get(key) is not None:
        return project.get(key)

    weekly_global = config.get("weekly_global", {}) or {}
    if weekly_global.get(key) is not None:
        return weekly_global.get(key)

    if config.get(key) is not None:
        return config.get(key)

    return default


def normalize_to_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [x for x in value if x]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return []


# =============================================================================
# OpenAI SDK 直连模型配置（豆包）
# =============================================================================
LLM_BASE_URL = get_llm_config("base_url", "")
LLM_API_KEY = get_llm_config("api_key", "")
LLM_MODEL = get_llm_config("model", "doubao-seed-2.0-pro")
LLM_TEMPERATURE = float(get_llm_config("temperature", 0.3))
LLM_MAX_TOKENS = int(get_llm_config("max_tokens", 4096))
LLM_PRINT_STREAM = bool(get_llm_config("print_stream", False))
LLM_MAX_RETRIES = int(get_llm_config("max_retries", 5))

if not LLM_BASE_URL or not LLM_API_KEY:
    raise ValueError("请在 config.llm_global 中配置 base_url 和 api_key，或兼容旧字段 llm_base_url / llm_api_key")

openai_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


# =============================================================================
# API 路由：保持原子池脚本风格
# =============================================================================
ACCESS_TOKEN_ROUTE = "/api/user/platform/getAccessToken"
NOTE_JSON_ROUTE = "/platform/ws/noteInfo/getDocJson"
DOC_TREE_ROUTE = "/platform/api/main/doc/treeList"
SIGNED_URL_ROUTE = "/platform/api/main/storage/getSignedUrl"
WORKSPACE_SAVE_ROUTE = "/middle/server/api/workspace/save"
MD_INSERT_ROUTE = "/middle/server/api/file/md/insert"
MESSAGE_SEND_ROUTE = "/middle/server/api/msg/send"

MESSAGE_TEMPLATE_ID = "80"
PLATFORM_TYPE = "all"
DEFAULT_MAX_PROJECTS_PER_BATCH = 8
DEFAULT_BATCH_NUMBER = min(int(get_weekly_config({}, "batch_number", 20)), 50)


# =============================================================================
# 通用工具函数
# =============================================================================
def get_headers_with_ak(user_guid="", doc_id=""):
    response = requests.post(
        url=BASE_URL + ACCESS_TOKEN_ROUTE,
        json={"ak": AK, "sk": SK},
        timeout=30,
    )
    response_json = response.json()
    if not response_json.get("data"):
        raise Exception(f"获取 AccessToken 失败: {response_json}")

    access_token = response_json["data"].get("accessToken")
    headers = {
        "Access-Token": access_token,
        "ak": AK,
        "X-User-GUID": user_guid or USER_GUID,
    }
    if doc_id:
        headers["docId"] = doc_id
    return headers


def _request_with_retry(method, url, max_retries=3, **kwargs):
    kwargs.setdefault("timeout", 30)
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            if method == "post":
                return requests.post(url, **kwargs)
            return requests.get(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            if attempt < max_retries:
                wait = min(2 ** attempt, 10)
                print(f"    ⚠️ {url.split('/')[-1]} 第 {attempt} 次请求失败: {e}, {wait}s 后重试...")
                time.sleep(wait)
            else:
                raise last_error


def get_note_json_content(user_guid="", doc_id=""):
    headers = get_headers_with_ak(user_guid=user_guid, doc_id=doc_id)
    response = requests.get(
        url=BASE_URL + NOTE_JSON_ROUTE,
        headers=headers,
        params={"docId": doc_id},
        timeout=60,
    )
    return response.json()


def strip_markdown_wrapper(content):
    content = (content or "").strip()
    if content.startswith("```json"):
        content = content[len("```json"):].lstrip("\n")
    elif content.startswith("```markdown"):
        content = content[len("```markdown"):].lstrip("\n")
    elif content.startswith("```"):
        content = content[3:].lstrip("\n")
    if content.endswith("```"):
        content = content[:-3].rstrip("\n")
    return content.strip()


def _repair_truncated_json(text):
    """尝试修复被 max_tokens 截断的 JSON：补齐未闭合的 } 和 ]。"""
    stack = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif ch == ']' and stack and stack[-1] == '[':
            stack.pop()
    close_map = {'{': '}', '[': ']'}
    suffix = ''.join(close_map[c] for c in reversed(stack))
    if not suffix:
        return text
    repaired = text.rstrip()
    # 去掉尾部可能不完整的值（截断在字符串/数字/true/false/null 中间）
    # 从末尾向前找到最后一个完整的值结束位置
    repaired = re.sub(r'[,]\s*$', '', repaired)
    # 如果最后在字符串中被截断，补上引号
    if in_string:
        repaired += '"'
    repaired += suffix
    return repaired


def safe_json_loads(text):
    clean_text = strip_markdown_wrapper(text)
    try:
        return json.loads(clean_text)
    except Exception:
        pass

    # 尝试修复截断的 JSON
    try:
        repaired = _repair_truncated_json(clean_text)
        return json.loads(repaired)
    except Exception:
        pass

    object_match = re.search(r"(\{.*\})", clean_text, flags=re.DOTALL)
    if object_match:
        try:
            return json.loads(object_match.group(1))
        except Exception:
            pass
    array_match = re.search(r"(\[.*\])", clean_text, flags=re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group(1))
        except Exception:
            pass
    raise ValueError("无法解析 JSON")


def _convert_special_nodes(content):
    """将 markdown mention / mentionUrl 转换为 workspace 可识别节点。"""
    content = re.sub(
        r"\[@([^\]]*)\]\(mention:[^:]+:([^)]+)\)",
        lambda m: f'<span data-node-type="mention" data-guid="{m.group(2)}"></span>',
        content,
    )
    content = re.sub(
        r"\[([^\]]+)\]\(mentionUrl:[^:]+:[^:]+:([^)]+)\)",
        lambda m: f'<a data-node-type="mentionUrl" data-url="{m.group(2)}">{m.group(1)}</a>',
        content,
    )
    content = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        content,
    )
    content = re.sub(
        r":::highlight\[[^\]]*\]\n(.*?):::",
        lambda m: f"<div data-node-type='highlightBlock' data-content-markdown>\n{m.group(1).rstrip()}\n</div>",
        content,
        flags=re.DOTALL,
    )
    return content


def load_prompt_text(prompt_file_guid, default_prompt):
    if not prompt_file_guid:
        return default_prompt
    try:
        signed_url_response = requests.get(
            BASE_URL + SIGNED_URL_ROUTE,
            headers=get_headers_with_ak(),
            params={"categoryGuid": prompt_file_guid},
            timeout=30,
        )
        signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")
        if not signed_url:
            return default_prompt
        return requests.get(signed_url, timeout=20).text
    except Exception as e:
        print(f"⚠️ Prompt 文件读取失败，使用默认 prompt: {e}")
        return default_prompt




def get_prompt_text(project, prompt_key, default_prompt):
    """
    Prompt 配置读取：对齐原子池周报脚本的项目级覆盖思路。

    优先级：
    1. project[prompt_key]：每个 projects[] 可以单独指定 prompt 文件 GUID
    2. weekly_global[prompt_key]：全局默认 prompt 文件 GUID
    3. config[prompt_key]：兼容旧结构
    4. 代码内置 default_prompt

    prompt_key 示例：
    - weekly_extract_prompt_file_guid
    - weekly_trend_prompt_file_guid
    - weekly_final_prompt_file_guid
    - weekly_validation_prompt_file_guid
    - weekly_repair_prompt_file_guid
    """
    prompt_file_guid = get_weekly_config(project, prompt_key, None)
    return load_prompt_text(prompt_file_guid, default_prompt)

def get_last_week_info():
    """固定搜索上周一到上周日 7 天，覆盖节假日无日报、调休周末有日报的情况。"""
    today = datetime.now()
    last_monday = today - timedelta(days=today.weekday() + 7)
    week_dates = [last_monday + timedelta(days=i) for i in range(7)]
    return {
        "start_date": week_dates[0].strftime("%Y-%m-%d"),
        "end_date": week_dates[-1].strftime("%Y-%m-%d"),
        "start_title": week_dates[0].strftime("%Y/%m/%d"),
        "end_title": week_dates[-1].strftime("%Y/%m/%d"),
        "date_list": [d.strftime("%Y-%m-%d") for d in week_dates],
        "week_number": week_dates[0].isocalendar()[1],
    }


def build_weekly_note_title(week_info, dept_name):
    year = week_info["start_date"][:4]
    return f"{year}#W{week_info['week_number']:02d} {dept_name}周报"


def build_intermediate_json_file(project_guid, target_date_str, json_content, suffix=""):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    name_suffix = f"_{suffix}" if suffix else ""
    file_name = f"weekly_raw_{project_guid}_{target_date_str.replace('-', '')}{name_suffix}_{unique_suffix}.json"
    file_path = os.path.join(tmp_dir, file_name)
    with open(file_path, "w", encoding="utf-8") as output_fp:
        json.dump(json_content, output_fp, ensure_ascii=False, indent=2)
    return file_path


def build_intermediate_markdown_file(project_guid, target_date_str, markdown_content, suffix=""):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    name_suffix = f"_{suffix}" if suffix else ""
    file_name = f"weekly_raw_{project_guid}_{target_date_str.replace('-', '')}{name_suffix}_{unique_suffix}.md"
    file_path = os.path.join(tmp_dir, file_name)
    with open(file_path, "w", encoding="utf-8") as output_fp:
        output_fp.write(markdown_content)
    return file_path


def cleanup_temp_files(file_paths, project_name=""):
    for file_path in file_paths or []:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                prefix = f"[Cleanup][{project_name}]" if project_name else "[Cleanup]"
                print(f"{prefix} 🧹 已删除临时文件: {file_path}")
        except Exception as e:
            prefix = f"[Cleanup][{project_name}]" if project_name else "[Cleanup]"
            print(f"{prefix} ⚠️ 删除临时文件失败: {file_path}, error={e}")


# =============================================================================
# LLM 调用
# =============================================================================
def call_llm(messages, max_tokens=None, temperature=None, stream=True, max_retries=None, print_stream=None):
    max_tokens = max_tokens or LLM_MAX_TOKENS
    temperature = LLM_TEMPERATURE if temperature is None else temperature
    max_retries = max_retries or LLM_MAX_RETRIES
    should_print_stream = LLM_PRINT_STREAM if print_stream is None else bool(print_stream)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"    🔄 [LLM 尝试 {attempt}/{max_retries}] 调用 {LLM_MODEL} ...")
            if not stream:
                response = openai_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=False,
                )
                return (response.choices[0].message.content or "").strip()

            response = openai_client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )
            chunks = []
            if should_print_stream:
                print("    🟢 开始流式输出：")
            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content is None:
                    continue
                chunks.append(content)
                if should_print_stream:
                    print(content, end="", flush=True)
            if should_print_stream:
                print("\n    ✅ 流式输出结束")
            else:
                print("    ✅ LLM 流式调用完成")
            return "".join(chunks).strip()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait_time = min(2 ** (attempt - 1), 30)
                print(f"    ⚠️ LLM 调用失败: {e}. {wait_time}s 后重试...")
                time.sleep(wait_time)
            else:
                print(f"    ❌ LLM 连续 {max_retries} 次失败: {e}")
                raise last_error


# =============================================================================
# 原始日报查找与读取（替代 load_weekly_atomic_pool）
# =============================================================================
def _get_tree_node_title(node):
    for key in ("dataTitle", "title", "name", "fileName", "filename"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _get_tree_node_guid(node):
    for key in ("categoryGuid", "dataGuid", "guid", "fileGuid", "id"):
        value = node.get(key)
        if value:
            return value
    return ""


def _infer_date_from_title(title, date_list):
    title = title or ""
    for date_str in date_list:
        variants = [date_str, date_str.replace("-", "/"), date_str.replace("-", "."), date_str.replace("-", "")]
        if any(v in title for v in variants):
            return date_str
    return ""


def find_weekly_raw_daily_notes(user_guid, project_guid, folder_guid, date_list, title_keywords=None):
    """
    从日报文件夹中查找原始日报文档。
    默认用文件标题中的日期命中上周 7 天。
    title_keywords 可限制标题包含某些关键词，例如 ["项目日报"]。
    """
    response = requests.post(
        url=BASE_URL + DOC_TREE_ROUTE,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={"projectGuid": project_guid, "parentGuid": folder_guid},
        timeout=60,
    )
    response_json = response.json()
    note_list = response_json.get("data") or []
    matched = []

    keywords = title_keywords or []
    for node in note_list:
        title = _get_tree_node_title(node)
        guid = _get_tree_node_guid(node)
        if not title or not guid:
            continue

        inferred_date = _infer_date_from_title(title, date_list)
        if not inferred_date:
            continue

        if keywords and not any(k in title for k in keywords):
            continue

        matched.append({
            "date": inferred_date,
            "note_guid": guid,
            "note_title": title,
            "node_type": node.get("dataType", node.get("type")),
        })

    matched.sort(key=lambda x: (x["date"], x["note_title"]))
    return matched


# =============================================================================
# docJson -> block list -> PM日报 person_blocks
# =============================================================================
def iter_blocks(node, skip_highlight=True):
    """把 Workspace docJson 扁平化为可顺序扫描的块，默认跳过 highlightBlock 模板说明。"""
    if isinstance(node, list):
        for child in node:
            yield from iter_blocks(child, skip_highlight=skip_highlight)
        return

    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if skip_highlight and node_type == "highlightBlock":
        return

    if node_type in ("fheading", "heading", "paragraph", "bulletListItem", "numberedListItem", "codeBlock", "table"):
        yield node
        # 对普通块不再向下 yield 子 text，避免重复。
        if node_type != "table":
            return

    for child in node.get("content", []) or []:
        yield from iter_blocks(child, skip_highlight=skip_highlight)


def get_doc_content_root(raw_doc_json):
    if isinstance(raw_doc_json, dict) and isinstance(raw_doc_json.get("data"), dict):
        return raw_doc_json["data"].get("content", []) or []
    if isinstance(raw_doc_json, dict):
        return raw_doc_json.get("content", []) or []
    return []


def extract_mentions_from_inline(node):
    mentions = []
    if isinstance(node, dict):
        if node.get("type") == "mention":
            attrs = node.get("attrs", {}) or {}
            label = attrs.get("label", "")
            uid = attrs.get("uid", "")
            user_id = attrs.get("id", "")
            mentions.append({
                "uid": uid,
                "id": user_id,
                "label": label,
                "mention_md": f"[@{label}](mention:{uid}:{user_id})" if label else "",
            })
        for child in node.get("content", []) or []:
            mentions.extend(extract_mentions_from_inline(child))
    elif isinstance(node, list):
        for child in node:
            mentions.extend(extract_mentions_from_inline(child))
    return mentions


def node_to_text(node, keep_mentions=True):
    parts = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
            return
        if not isinstance(x, dict):
            return
        t = x.get("type")
        if t == "text":
            parts.append(x.get("text", ""))
            return
        if t == "mention":
            attrs = x.get("attrs", {}) or {}
            label = attrs.get("label", "")
            uid = attrs.get("uid", "")
            user_id = attrs.get("id", "")
            if keep_mentions and label:
                parts.append(f"[@{label}](mention:{uid}:{user_id})")
            elif label:
                parts.append(f"@{label}")
            return
        if t == "mentionUrl":
            attrs = x.get("attrs", {}) or {}
            content = attrs.get("content", "链接")
            original_url = attrs.get("originalUrl", "")
            uid = attrs.get("uid", "")
            data_type = attrs.get("dataType", 1)
            parts.append(f"[{content}](mentionUrl:{uid}:{data_type}:{original_url})")
            return
        if t == "hardBreak":
            parts.append("\n")
            return
        for child in x.get("content", []) or []:
            walk(child)

    walk(node)
    return "".join(parts).strip()


def get_block_level(block):
    if block.get("type") in ("heading", "fheading"):
        level = block.get("attrs", {}).get("level", 1)
        try:
            return int(level)
        except Exception:
            return 1
    return None


def normalize_heading_text(text):
    return re.sub(r"\s+", "", (text or "").strip()).lower()


def is_target_section_heading(block, target_sections):
    if block.get("type") != "heading":
        return False
    if get_block_level(block) != 2:
        return False
    text = node_to_text(block, keep_mentions=False)
    norm = normalize_heading_text(text)
    targets = [normalize_heading_text(x) for x in target_sections]
    return any(t and t in norm for t in targets)


def is_same_or_higher_level_heading(block, current_level):
    if block.get("type") != "heading":
        return False
    level = get_block_level(block)
    return level is not None and level <= current_level


def is_person_heading(block, person_heading_level=3):
    if block.get("type") != "heading":
        return False
    if get_block_level(block) != int(person_heading_level):
        return False
    return bool(extract_mentions_from_inline(block))


def parse_person_heading(block):
    mentions = extract_mentions_from_inline(block)
    plain_text = node_to_text(block, keep_mentions=False)
    role_text = plain_text
    for m in mentions:
        if m.get("label"):
            role_text = role_text.replace("@" + m["label"], "")
            role_text = role_text.replace(m["label"], "", 1) if role_text.strip().startswith(m["label"]) else role_text
    role_text = role_text.strip()
    role_text = re.sub(r"^[\s\-—–:：]+", "", role_text).strip()
    return mentions, role_text


def block_to_raw_line(block):
    text = node_to_text(block, keep_mentions=True).strip()
    if not text:
        return None

    block_type = block.get("type", "paragraph")
    depth = block.get("attrs", {}).get("depth", 0)
    try:
        depth = int(depth)
    except Exception:
        depth = 0

    return {
        "text": text,
        "block_type": block_type,
        "depth": depth,
        "mentions": extract_mentions_from_inline(block),
    }


def extract_project_title_info(blocks):
    """从 fheading 或一级标题中提取项目标题和状态行。"""
    for block in blocks:
        if block.get("type") in ("fheading", "heading") and get_block_level(block) == 1:
            text = node_to_text(block, keep_mentions=False)
            if text:
                # 例：V1项目日报: 🟡 6/30 WD 中风险. 🔴 12/31 ES 高风险
                if ":" in text:
                    left, right = text.split(":", 1)
                    return left.strip(), right.strip(), text.strip()
                if "：" in text:
                    left, right = text.split("：", 1)
                    return left.strip(), right.strip(), text.strip()
                return text.strip(), "", text.strip()
    return "", "", ""


def extract_section_blocks(blocks, target_sections):
    """抽取目标二级标题下的块，直到下一个 level<=2 标题。支持多个目标 section。"""
    selected = []
    capturing = False
    current_level = None

    for block in blocks:
        if is_target_section_heading(block, target_sections):
            capturing = True
            current_level = get_block_level(block)
            continue

        if capturing and is_same_or_higher_level_heading(block, current_level):
            capturing = False
            current_level = None
            continue

        if capturing:
            selected.append(block)

    return selected


def split_person_blocks(section_blocks, meta, person_heading_level=3, min_content_chars=2):
    person_blocks = []
    current = None

    for block in section_blocks:
        if is_person_heading(block, person_heading_level=person_heading_level):
            if current:
                person_blocks.append(current)
            mentions, role_text = parse_person_heading(block)
            current = {
                "block_id": str(uuid.uuid4()),
                "date": meta.get("date", ""),
                "dept_name": meta.get("dept_name", ""),
                "project_name": meta.get("project_name", ""),
                "platforms": meta.get("platforms", []) or [],
                "source": meta.get("source", {}) or {},
                "members": mentions,
                "role_text": role_text,
                "raw_lines": [],
            }
            continue

        if current is None:
            # PM日报标题下，若在第一个人头前有说明文字，不进入周报。
            continue

        raw_line = block_to_raw_line(block)
        if raw_line:
            raw_line["source_line_id"] = f"{current['block_id']}:{len(current['raw_lines']) + 1}"
            current["raw_lines"].append(raw_line)

    if current:
        person_blocks.append(current)

    # 过滤空人头块，避免把职责标题当日报内容。
    filtered = []
    for b in person_blocks:
        content_text = "\n".join(x.get("text", "") for x in b.get("raw_lines", []))
        if len(content_text.strip()) >= min_content_chars:
            filtered.append(b)
    return filtered


def parse_pm_daily_doc(raw_doc_json, note_meta, project):
    target_sections = get_weekly_config(project, "target_sections", ["PM日报"])
    if isinstance(target_sections, str):
        target_sections = [target_sections]
    skip_highlight = bool(get_weekly_config(project, "skip_highlight", True))
    person_heading_level = int(get_weekly_config(project, "person_heading_level", 3))
    min_content_chars = int(get_weekly_config(project, "min_person_content_chars", 2))

    root = get_doc_content_root(raw_doc_json)
    blocks = list(iter_blocks(root, skip_highlight=skip_highlight))
    title_project_name, project_status, title_raw = extract_project_title_info(blocks)

    if not note_meta.get("project_name"):
        note_meta["project_name"] = title_project_name or project.get("project_name", "未分类项目")
    note_meta["project_status"] = project_status
    note_meta["note_title_raw"] = title_raw

    section_blocks = extract_section_blocks(blocks, target_sections)
    person_blocks = split_person_blocks(
        section_blocks=section_blocks,
        meta=note_meta,
        person_heading_level=person_heading_level,
        min_content_chars=min_content_chars,
    )
    return person_blocks, {
        "block_count": len(blocks),
        "section_block_count": len(section_blocks),
        "person_block_count": len(person_blocks),
        "target_sections": target_sections,
        "project_status": project_status,
    }


# =============================================================================
# Prompt：person_blocks -> items
# =============================================================================
def get_default_extract_items_prompt():
    return """你是一个严谨的项目日报结构化抽取助手。

你会收到从原始 Workspace 日报中规则抽取出的 person_blocks。每个 person_block 来自某一天日报中 PM日报板块下的一个三级 mention 人头标题。

你的任务：将 person_blocks 中 raw_lines 的真实填写内容，抽取为可用于周报聚合的 items JSON。

# 重要背景
- members 表示该日报内容对应的人。
- role_text 是 mention 标题同行的职责/角色，例如 PM、Co-PM、RDPOM、Process reliability。它不是日报内容，但可以作为理解上下文。
- raw_lines 才是真正填写的日报内容。
- 如果 raw_lines 只是空白、模板占位、无实质内容，不要生成 item。

# 输出要求
必须只输出合法 JSON，不要输出代码块，不要输出解释文字。

# 输出结构
{
  "items": [
    {
      "date": "YYYY-MM-DD",
      "dept_name": "",
      "project_name": "",
      "platforms": [""],
      "section": "progress | issues_support | next_plan",
      "member": {
        "uid": "",
        "id": "",
        "label": "",
        "mention_md": "[@姓名](mention:uid:id)"
      },
      "role_text": "",
      "content": [
        {
          "text": "保留事实的中文/英文描述，不要编造",
          "block_type": "bullet|numbered|paragraph",
          "depth": 0,
          "mentions": []
        }
      ],
      "source": {
        "note_guid": "",
        "note_title": "",
        "source_url": ""
      }
    }
  ]
}

# section 判断规则
1. 已完成、推进、开发、验证、测试、交付、对齐、closed、done、finished、完成率、进展类内容 -> progress。
2. 风险、问题、困难、阻塞、延期、delay、block、hold、需要支持、Need Help、依赖、待协调 -> issues_support。
3. 计划、近期规划、后续、下一步、下周、todo、next、待推动 -> next_plan。
4. 同一 raw_line 同时包含进展和计划/风险时，可以拆成多条 item。

# 保真要求
1. 不允许编造项目名、平台、人员、数字、风险等级、结论。
2. project_name/platforms/date/source 必须来自输入。
3. member 必须来自输入 members；如果一个 person_block 有多个 members，可以复制为多条 item，或在主要 member 中保留第一个人，但不得编造。
4. role_text 原样保留。
5. 数量、case 名称、模块名、接口名、实验名、版本号、日期、风险等级必须尽量保留。
6. 不要把 role_text 当作 content 输出。
7. 不要把“填写说明、模板占位示例”输出为 item。

# 输入 person_blocks
{{person_blocks_json}}
"""


def split_list_into_batches(items, batch_size):
    batch_size = max(int(batch_size or 20), 1)
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]



def normalize_text_for_match(text):
    text = str(text or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    return text


def classify_section_by_text(text):
    """规则兜底分类，保证 LLM 抽取遗漏时仍能进入 timeline。"""
    t = str(text or "").lower()
    issue_keywords = [
        "风险", "问题", "困难", "阻塞", "延期", "延迟", "异常", "依赖", "需要支持", "需要协助",
        "need help", "needhelp", "help", "delay", "blocked", "blocker", "hold", "risk", "issue",
        "需协调", "待协调", "瓶颈", "外援", "未及时", "无法"
    ]
    next_keywords = [
        "计划", "近期规划", "后续", "下一步", "下周", "明日", "待推进", "待推动",
        "todo", "to do", "next", "plan", "follow up", "跟进"
    ]
    if any(k in t for k in issue_keywords):
        return "issues_support"
    if any(k in t for k in next_keywords):
        return "next_plan"
    return "progress"


def is_placeholder_raw_line(text):
    """过滤明显模板占位。注意不要过度过滤真实短句。"""
    t = str(text or "").strip()
    if not t:
        return True
    normalized = normalize_text_for_match(t)
    if normalized in {"无", "暂无", "na", "n/a", "-", "—", "none", "null"}:
        return True
    placeholder_patterns = [
        r"^事项\d+",
        r"如[“\"].+[”\"]",
        r"填写要求",
        r"文档说明",
        r"模板[:：]?$",
        r"更新时间",
        r"状态更新",
    ]
    return any(re.search(p, t, flags=re.IGNORECASE) for p in placeholder_patterns)


def primary_member_from_members(members):
    members = members or []
    if isinstance(members, list) and members:
        return members[0] or {}
    return {}


def build_rule_item_from_raw_line(person_block, raw_line):
    members = person_block.get("members", []) or []
    primary_member = primary_member_from_members(members)
    text = raw_line.get("text", "")
    return {
        "item_id": str(uuid.uuid4()),
        "date": person_block.get("date", ""),
        "dept_name": person_block.get("dept_name", ""),
        "project_name": person_block.get("project_name", ""),
        "platforms": person_block.get("platforms", []) or [],
        "section": classify_section_by_text(text),
        "member": primary_member,
        "members": members,
        "role_text": person_block.get("role_text", ""),
        "content": [
            {
                "text": text,
                "block_type": raw_line.get("block_type", "paragraph"),
                "depth": raw_line.get("depth", 0),
                "mentions": raw_line.get("mentions", []) or [],
                "source_line_id": raw_line.get("source_line_id", "")
            }
        ],
        "source": person_block.get("source", {}) or {},
        "source_block_id": person_block.get("block_id", ""),
        "source_line_ids": [raw_line.get("source_line_id", "")]
    }


def normalize_extracted_item(item):
    """统一 LLM 输出字段，避免 prompt 输出 members[]，但 timeline 只读 member{} 时丢人。"""
    if not isinstance(item, dict):
        return None
    members = item.get("members")
    member = item.get("member")
    if not member and isinstance(members, list) and members:
        item["member"] = members[0] or {}
    elif member and not members:
        item["members"] = [member]

    content = item.get("content")
    if isinstance(content, str):
        item["content"] = [{"text": content, "block_type": "paragraph", "depth": 0, "mentions": []}]
    elif isinstance(content, dict):
        item["content"] = [content]
    elif content is None:
        item["content"] = []

    item.setdefault("item_id", str(uuid.uuid4()))
    item["section"] = normalize_section(item.get("section"))
    return item


def patch_missing_raw_lines_into_items(person_blocks, extracted_items, project):
    """
    关键兜底：person_blocks 是规则解析的全量结果，LLM extract 可能合并/漏掉 raw_lines。
    为了保证 timeline 不少，把未被 items 覆盖的 raw_line 补成规则 item。
    """
    enable_patch = bool(get_weekly_config(project, "preserve_all_raw_lines_in_timeline", True))
    if not enable_patch:
        return extracted_items

    existing_text_blob = "\n".join(
        c.get("text", "")
        for item in extracted_items or []
        for c in (item.get("content", []) or [])
        if isinstance(c, dict)
    )
    existing_norm = normalize_text_for_match(existing_text_blob)

    patched = list(extracted_items or [])
    raw_line_count = 0
    added_count = 0

    for block in person_blocks or []:
        for raw_line in block.get("raw_lines", []) or []:
            raw_text = raw_line.get("text", "")
            if is_placeholder_raw_line(raw_text):
                continue

            raw_line_count += 1
            raw_norm = normalize_text_for_match(raw_text)

            # LLM 若原句保留，则不重复补；若高度改写，可能少量重复，但优先保证不漏。
            if raw_norm and raw_norm in existing_norm:
                continue

            patched.append(build_rule_item_from_raw_line(block, raw_line))
            added_count += 1

    print(
        f"    [Extract Coverage] raw_lines={raw_line_count}, "
        f"llm_items={len(extracted_items or [])}, patched_missing={added_count}, "
        f"final_items={len(patched)}"
    )
    return patched



def build_rule_items_from_person_blocks(person_blocks):
    """
    不调用 LLM 的结构化兜底：
    将 person_blocks.raw_lines 逐条转成 weekly item。
    用于 LLM JSON 解析失败、字段异常、模型超时等情况，保证 timeline 不断。
    """
    rule_items = []
    for block in person_blocks or []:
        for raw_line in block.get("raw_lines", []) or []:
            raw_text = raw_line.get("text", "")
            if is_placeholder_raw_line(raw_text):
                continue
            rule_items.append(build_rule_item_from_raw_line(block, raw_line))
    return rule_items


def save_bad_llm_output(project, batch_idx, raw_text):
    """
    结构化抽取失败时保存模型原始输出，便于回看 prompt / JSON 格式问题。
    """
    try:
        tmp_dir = tempfile.gettempdir()
        project_name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", project.get("project_name", "project"))
        file_path = os.path.join(tmp_dir, f"bad_extract_output_{project_name}_batch{batch_idx}_{uuid.uuid4().hex[:8]}.txt")
        with open(file_path, "w", encoding="utf-8") as fp:
            fp.write(raw_text or "")
        print(f"    ⚠️ LLM 结构化原始输出已保存: {file_path}")
        return file_path
    except Exception as e:
        print(f"    ⚠️ 保存 LLM 原始输出失败: {e}")
        return ""



def convert_person_blocks_to_items(person_blocks, project=None):
    """
    规则方式：person_blocks -> weekly_items。

    设计原则：
    1. 不让 LLM 决定哪些 raw_lines 保留，避免 timeline 少内容。
    2. 每条有效 raw_line 生成一条 weekly item。
    3. section 只做轻量规则分类：progress / issues_support / next_plan。
    4. 后续 trend/final 阶段再由 LLM 做周维度合并、去重和表达优化。
    """
    items = []
    for block in person_blocks or []:
        for raw_line in block.get("raw_lines", []) or []:
            raw_text = raw_line.get("text", "")
            if is_placeholder_raw_line(raw_text):
                continue
            items.append(build_rule_item_from_raw_line(block, raw_line))

    print(f"    [Rule Convert] person_blocks={len(person_blocks or [])}, weekly_items={len(items)}")
    return items


def llm_extract_items_from_person_blocks(person_blocks, project):
    """
    历史函数名保留，避免主链路改动太大。

    默认不做 LLM 前置抽取，而是直接规则全量转换：
    person_blocks -> weekly_items -> timeline

    如确实需要恢复 LLM 抽取，可在 config 中显式设置：
    "enable_llm_extract_items": true

    但即便开启 LLM，也建议保留 preserve_all_raw_lines_in_timeline=true 防止漏项。
    """
    enable_llm_extract = bool(get_weekly_config(project, "enable_llm_extract_items", False))

    if not enable_llm_extract:
        return convert_person_blocks_to_items(person_blocks, project)

    # 以下为可选 LLM 抽取路径：默认关闭。
    if not person_blocks:
        return []

    batch_size = int(get_weekly_config(project, "extract_batch_size", 20))
    prompt_template = get_prompt_text(project, "weekly_extract_prompt_file_guid", get_default_extract_items_prompt())
    all_items = []
    bad_outputs = []

    batches = list(split_list_into_batches(person_blocks, batch_size))
    for idx, batch in enumerate(batches, 1):
        print(f"    [Extract][{project.get('project_name', '')}] person_blocks batch {idx}/{len(batches)}, size={len(batch)}")

        project_name = project.get("project_name", "")
        dept_name = project.get("dept_name", "")
        platforms = project.get("platforms") or project.get("platform") or []
        if isinstance(platforms, str):
            platforms = [platforms]

        prompt_text = (
            prompt_template
            .replace("{{project_name}}", project_name)
            .replace("{{dept_name}}", dept_name)
            .replace("{{platforms_json}}", json.dumps(platforms, ensure_ascii=False))
            .replace("{{person_blocks_json}}", json.dumps(batch, ensure_ascii=False, indent=2))
        )

        batch_items = []
        raw_result = ""

        try:
            raw_result = call_llm(
                messages=[
                    {"role": "system", "content": "你是项目日报结构化抽取助手，只输出合法 JSON。"},
                    {"role": "user", "content": prompt_text},
                ],
                max_tokens=int(get_weekly_config(project, "weekly_extract_max_tokens", LLM_MAX_TOKENS)),
                temperature=float(get_weekly_config(project, "weekly_extract_temperature", 0.0)),
                stream=True,
                max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
            )

            parsed = safe_json_loads(raw_result)
            items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
            if not isinstance(items, list):
                raise ValueError(f"结构化抽取结果不是 items list: type={type(items)}")

            for item in items:
                item = normalize_extracted_item(item)
                if not item:
                    continue
                if not item.get("content"):
                    continue
                batch_items.append(item)

            print(f"    [Extract][batch {idx}] ✅ LLM items={len(batch_items)}")

        except Exception as e:
            print(f"    [Extract][batch {idx}] ⚠️ LLM 结构化抽取失败，改用规则兜底。error={e}")
            if raw_result:
                bad_path = save_bad_llm_output(project, idx, raw_result)
                if bad_path:
                    bad_outputs.append(bad_path)

            batch_items = build_rule_items_from_person_blocks(batch)
            print(f"    [Extract][batch {idx}] ✅ 规则兜底 items={len(batch_items)}")

        all_items.extend(batch_items)

    all_items = patch_missing_raw_lines_into_items(person_blocks, all_items, project)

    if bad_outputs:
        print(f"    ⚠️ 共 {len(bad_outputs)} 个 batch 发生 LLM 抽取异常，原始输出文件: {bad_outputs}")

    return all_items


# =============================================================================
# 非原子池日报 -> 临时周级 items pool
# =============================================================================
def load_weekly_raw_daily_pool(project):
    generated_files = []
    week_info = get_last_week_info()
    project_name = project.get("project_name", "Unknown")

    raw_project_guid = (
        project.get("raw_daily_project_guid")
        or project.get("daily_project_guid")
        or project.get("project_guid")
    )
    raw_parent_guid = (
        project.get("raw_daily_folder_guid")
        or project.get("daily_folder_guid")
        or project.get("work_log_folder_guid")
    )

    if not raw_project_guid:
        raise ValueError(f"配置错误: project '{project_name}' 缺少 raw_daily_project_guid / project_guid")
    if not raw_parent_guid:
        raise ValueError(f"配置错误: project '{project_name}' 缺少 raw_daily_folder_guid / work_log_folder_guid")

    project_user_guids = project.get(
        "user_guid_list",
        [project.get("user_guid") or project.get("leader_guid") or USER_GUID],
    )
    title_keywords = get_weekly_config(project, "raw_daily_title_keywords", [])

    print(f"[Step 1][{project_name}] 目标周期: {week_info['start_date']} ~ {week_info['end_date']}")
    print(f"[Step 1][{project_name}] 搜索上周 7 天: {week_info['date_list']}")
    print(f"[Step 1][{project_name}] 原始日报空间 raw_daily_project_guid: {raw_project_guid}")
    print(f"[Step 1][{project_name}] 原始日报目录 raw_daily_folder_guid: {raw_parent_guid}")

    matched_notes = []
    seen = set()
    for user_guid in project_user_guids:
        if not user_guid:
            continue
        notes = find_weekly_raw_daily_notes(
            user_guid=user_guid,
            project_guid=raw_project_guid,
            folder_guid=raw_parent_guid,
            date_list=week_info["date_list"],
            title_keywords=title_keywords,
        )
        for note in notes:
            if note["note_guid"] in seen:
                continue
            seen.add(note["note_guid"])
            note["user_guid"] = user_guid
            matched_notes.append(note)

    if not matched_notes:
        print(f"[Step 1][{project_name}] ❌ 未找到上周原始日报")
        return {}, False, [], []

    matched_notes.sort(key=lambda x: (x["date"], x["note_title"]))
    print(f"[Step 1][{project_name}] ✅ 找到 {len(matched_notes)} 份候选原始日报，开始解析 PM 日报板块...")

    all_person_blocks = []
    source_urls = OrderedDict()
    source_note_entries = []

    platforms = project.get("platforms") or project.get("platform") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    if not platforms:
        platforms = [project_name]

    for note in matched_notes:
        note_url = f"{BASE_URL}/workspace/{note['note_guid']}"
        source_urls[note["date"]] = note_url
        try:
            raw_json = get_note_json_content(user_guid=note["user_guid"], doc_id=note["note_guid"])
            note_meta = {
                "date": note["date"],
                "dept_name": project.get("dept_name", ""),
                "project_name": project_name,
                "platforms": platforms,
                "source": {
                    "note_guid": note["note_guid"],
                    "note_title": note["note_title"],
                    "source_url": note_url,
                },
            }
            person_blocks, parse_summary = parse_pm_daily_doc(raw_json, note_meta, project)
            print(
                f"    [Parse][{project_name}] {note['date']} {note['note_title']} -> "
                f"section_blocks={parse_summary['section_block_count']}, person_blocks={parse_summary['person_block_count']}"
            )
            all_person_blocks.extend(person_blocks)
            source_note_entries.append({
                "date": note["date"],
                "url": note_url,
                "note_guid": note["note_guid"],
                "note_title": note["note_title"],
                "person_block_count": len(person_blocks),
                "parse_summary": parse_summary,
            })
        except Exception as e:
            print(f"    [Skip][{project_name}] 原始日报解析失败: {note.get('note_title')} error={e}")
            traceback.print_exc()

    if not all_person_blocks:
        print(f"[Step 2][{project_name}] ❌ 未解析到有效 PM 日报内容")
        return {}, False, [], source_note_entries

    raw_blocks_path = build_intermediate_json_file(
        project_guid=raw_project_guid,
        target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
        json_content={"person_blocks": all_person_blocks, "source_note_entries": source_note_entries},
        suffix="person_blocks",
    )
    generated_files.append(raw_blocks_path)
    print(f"[Step 2][{project_name}] 📦 person_blocks 已生成: {raw_blocks_path}")

    items = llm_extract_items_from_person_blocks(all_person_blocks, project)
    if not items:
        print(f"[Step 2][{project_name}] ❌ LLM 未抽取到 weekly items")
        return {}, False, generated_files, source_note_entries

    actual_dates = sorted({item.get("date") for item in items if item.get("date")})
    if not actual_dates:
        actual_dates = sorted(source_urls.keys())

    week_number = week_info["week_number"]
    if actual_dates:
        try:
            week_number = datetime.strptime(actual_dates[0], "%Y-%m-%d").isocalendar()[1]
        except Exception:
            pass

    weekly_pool = {
        "metadata": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_type": "raw_pm_daily",
            "range_dates": actual_dates or week_info["date_list"],
            "source_urls": dict(source_urls),
            "week_number": week_number,
            "source_note_entries": source_note_entries,
            "total_person_blocks": len(all_person_blocks),
            "total_items": len(items),
        },
        "items": items,
    }

    items_path = build_intermediate_json_file(
        project_guid=raw_project_guid,
        target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
        json_content=weekly_pool,
        suffix="weekly_items",
    )
    generated_files.append(items_path)
    print(f"[Step 2][{project_name}] ✅ 临时 weekly_items 已生成: {items_path}, total_items={len(items)}")

    return weekly_pool, True, generated_files, source_note_entries


# =============================================================================
# items -> timeline state（复用原子池周报结构）
# =============================================================================
def normalize_section(section):
    section = (section or "progress").strip().lower()
    if section in ("progress", "main_progress", "done", "completed"):
        return "progress"
    if section in (
        "issue", "issues", "risk", "risks", "help", "issue_help", "issue_and_help",
        "issues_help", "risk_help", "support", "difficulty", "blocked", "blocker",
        "need_help", "needhelp", "issues_support",
    ):
        return "issues_support"
    if section in ("next", "next_focus", "next_plan", "plan", "todo", "future", "nextkeyfocus", "key_focus"):
        return "next_plan"
    return section


def restore_content_tree(content_blocks):
    roots = []
    stack = []
    for block in content_blocks or []:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        depth = block.get("depth")
        try:
            depth = int(depth)
        except Exception:
            depth = 0
        node = {
            "text": text,
            "block_type": block.get("block_type", "paragraph"),
            "depth": depth,
            "mentions": block.get("mentions", []) or [],
            "children": [],
        }
        while stack and depth <= stack[-1][0]:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((depth, node))
    return roots


def tree_to_markdown(nodes, level=0):
    lines = []
    indent = "  " * level
    for node in nodes or []:
        lines.append(f"{indent}- {node.get('text', '')}")
        if node.get("children"):
            lines.extend(tree_to_markdown(node["children"], level + 1))
    return lines


def get_item_project_name(item):
    direct_name = (item.get("project_name") or "").strip()
    if direct_name:
        return direct_name
    project_obj = item.get("project", {}) or {}
    return (project_obj.get("name") or "未分类项目").strip()


def atomic_item_to_tree_entry(item):
    # 名字沿用 atomic_item，是为了和原周报代码衔接；这里的 item 是临时 weekly item。
    member = item.get("member", {}) or {}
    if not member and isinstance(item.get("members"), list) and item.get("members"):
        member = item.get("members")[0] or {}
    source = item.get("source", {}) or {}
    return {
        "item_id": item.get("item_id") or str(uuid.uuid4()),
        "date": item.get("date", ""),
        "dept_name": item.get("dept_name", ""),
        "project_name": get_item_project_name(item),
        "project_name_source": item.get("project_name_source", "raw_pm_daily"),
        "member": {
            "uid": member.get("uid", ""),
            "id": member.get("id", ""),
            "label": member.get("label", ""),
            "mention_md": member.get("mention_md", ""),
        },
        "role_text": item.get("role_text", ""),
        "section": normalize_section(item.get("section")),
        "content_tree": restore_content_tree(item.get("content", []) or []),
        "source": {
            "note_guid": source.get("note_guid", ""),
            "note_title": source.get("note_title", ""),
            "source_url": source.get("source_url", ""),
        },
    }


def build_weekly_timeline_state(weekly_pool):
    items = weekly_pool.get("items", []) or []
    metadata = weekly_pool.get("metadata", {}) or {}
    platform_map = OrderedDict()

    for item in items:
        platforms = item.get("platforms") or ["未标注平台"]
        entry = atomic_item_to_tree_entry(item)
        project_name = entry["project_name"]
        section = entry["section"]
        date = entry["date"]

        for platform in platforms:
            platform = (platform or "未标注平台").strip()
            if platform not in platform_map:
                platform_map[platform] = OrderedDict()
            if project_name not in platform_map[platform]:
                platform_map[platform][project_name] = {
                    "project_name": project_name,
                    "sections": {
                        "progress": OrderedDict(),
                        "issues_support": OrderedDict(),
                        "next_plan": OrderedDict(),
                    },
                }
            if section not in platform_map[platform][project_name]["sections"]:
                platform_map[platform][project_name]["sections"][section] = OrderedDict()
            if date not in platform_map[platform][project_name]["sections"][section]:
                platform_map[platform][project_name]["sections"][section][date] = []
            platform_map[platform][project_name]["sections"][section][date].append(entry)

    platforms_out = []
    for platform_name, project_map in platform_map.items():
        projects_out = []
        for project_name, project_data in project_map.items():
            normalized_sections = {}
            for section_name, date_map in project_data["sections"].items():
                normalized_sections[section_name] = [
                    {"date": d, "items": date_map[d]}
                    for d in sorted(date_map.keys())
                ]
            projects_out.append({"project_name": project_name, "sections": normalized_sections})
        platforms_out.append({"platform": platform_name, "projects": projects_out})

    return {"metadata": metadata, "platforms": platforms_out}


def compact_timeline_state_for_llm(batch_state, max_text_len_per_tree=2000):
    compact = {"platform": batch_state.get("platform"), "projects": []}
    for project in batch_state.get("projects", []) or []:
        compact_project = {"project_name": project.get("project_name", "未分类项目"), "sections": {}}
        for section_name, date_entries in (project.get("sections", {}) or {}).items():
            section_days = []
            for day in date_entries or []:
                day_items = []
                for item in day.get("items", []) or []:
                    tree_md = "\n".join(tree_to_markdown(item.get("content_tree", [])))
                    if len(tree_md) > max_text_len_per_tree:
                        tree_md = tree_md[:max_text_len_per_tree] + "..."
                    member_text = item.get("member", {}).get("mention_md") or item.get("member", {}).get("label", "")
                    role_text = item.get("role_text", "")
                    if role_text:
                        member_text = f"{member_text}（{role_text}）" if member_text else role_text
                    day_items.append({
                        "item_id": item.get("item_id", ""),
                        "member": member_text,
                        "source_url": item.get("source", {}).get("source_url", ""),
                        "content_tree_markdown": tree_md,
                    })
                if day_items:
                    section_days.append({"date": day.get("date"), "items": day_items})
            compact_project["sections"][section_name] = section_days
        compact["projects"].append(compact_project)
    return compact


def split_platform_projects_into_batches(timeline_state, max_projects_per_batch=DEFAULT_MAX_PROJECTS_PER_BATCH):
    batches = []
    for platform_data in timeline_state.get("platforms", []) or []:
        platform_name = platform_data.get("platform", "未标注平台")
        projects_list = platform_data.get("projects", []) or []
        for i in range(0, len(projects_list), max_projects_per_batch):
            batches.append({"platform": platform_name, "projects": projects_list[i:i + max_projects_per_batch]})
    return batches


# =============================================================================
# Prompt：趋势分析 / 最终整理 / 覆盖校验 / 修复
# =============================================================================
def get_default_trend_prompt():
    return """你是项目周报趋势分析助手。

你会收到某一个 platform 下若干 project 的一周日报事项时间线。数据已经按 project / section / date 组织，并且 content_tree_markdown 中保留了从原始 PM 日报抽取出的事实。

你的任务不是简单逐条改写，而是理解一周内同一事项的推进趋势，并输出结构化 JSON，供后续拼接周报使用。

# 必须输出合法 JSON
不要输出代码块，不要输出解释文字。

# 输出结构
必须严格输出以下结构：
{
  "platform": "",
  "core_progress": [
    {
      "project_name": "",
      "subtopic": "",
      "items": [
        {
          "mention": "[@姓名](mention:uid:id)",
          "summary": "",
          "evidence_dates": ["YYYY-MM-DD"],
          "source_item_ids": [""]
        }
      ]
    }
  ],
  "main_progress": [],
  "issues_support": [],
  "next_plan": []
}

其中 main_progress / issues_support / next_plan 的元素结构与 core_progress 完全一致。

# 字段来源要求
1. platform 必须与输入 platform 完全一致。
2. project_name 必须严格来自输入 projects[].project_name，不允许根据正文重新命名。
3. subtopic 只能来自 content_tree_markdown 中明确出现的模块名、子项名、专题名或事项标题；无法识别时等于 project_name。
4. items[].mention 必须优先填写输入 item 中的 member 字段，例如 `[@姓名](mention:uid:id)`，可以保留括号中的职责。
5. 如果同一 subtopic 下有多位成员推进，必须分别作为多个 items[] 输出，不要合并后丢失 mention。
6. 如果输入 member 为空，mention 可以为空字符串，但不得编造 mention。

# 趋势理解规则
1. summary 要体现“周”的视角，例如：从A推进到B、完成A并进入B、围绕A持续验证、受B依赖影响待推进。
2. main_progress 要尽量覆盖每个 project 的主要进展；core_progress 只选本批次最重要的 2~6 条。
3. issues_support 只写明确困难、依赖、风险、支持需求，不要自行推断。
4. next_plan 只写明确计划，不要自行创造。
5. 如果某数组无内容，输出空数组 []。

# 量化与枚举要求
1. 如果输入中明确出现多个可枚举对象，例如算法、case、模块、平台、接口、实验、文档、任务项、缺陷类型等，必须尽量保留数量信息。
2. 1~5 个对象必须列出具体名称；6 个及以上必须写明数量，并尽量列出代表性对象。
3. 严禁在可以明确计数或枚举时使用“多个算法”“若干 case”“相关模块”等模糊表达。
4. 如果输入中出现完成数量、测试数量、case 数量、缺陷数量、文档数量、接口数量等数字，必须保留。

输入 JSON：
{{batch_json}}
"""


def get_default_final_weekly_prompt():
    return """# Role
你是一位严谨、客观、专业的项目周报整理助手。

你的任务是：将输入的“周报结构化汇总 Markdown”整理为最终可发布的团队周报正文。

# 核心目标
第一优先级是“主题覆盖完整”，不是语言优美。
输出结果必须尽可能保证：输入中每一个明确出现的 platform、project、subtopic、事项，都能在最终周报中找到对应落点。

# 最高优先级原则
1. 只允许基于输入内容进行整理、压缩、重组，不允许新增输入中不存在的事实。
2. 严禁编造人名、账号、mention、项目状态、风险等级、延期时间、里程碑、测试结果、资源申请等信息。
3. 严禁输出“日期范围、周数、源日报链接”等头部元信息，这些由系统自动补充。
4. 若输入中已有 `[@姓名](mention:uid:id)`，请尽量原样保留；若输入中没有明确 mention，不要补充虚构人名。
5. 不得将多个原本独立的主题粗暴合并成抽象上位类别，导致原主题名消失。
6. 严禁输出“项目X”“模块A”“子项B”“相关工作”“其他工作”等占位或推测名称。

# 输出结构
必须严格输出以下 4 个章节，不要新增其他一级章节：

### 🎉 本周关键进展
用一段 80~150 字的客观文字总结本周最核心的推进情况。不要写空泛评价。

### ✅ 本周主要进展
必须按 platform → project → 事项 的层级输出。

标准格式：
- **{Platform}**
    - **{Project}**
        - [@姓名](mention:uid:id) 具体进展事项

如果输入中明确存在模块/子项/专题名，可以增加一层 subtopic：
- **{Platform}**
    - **{Project}**
        - **{Subtopic}**
            - [@姓名](mention:uid:id) 具体进展事项

### ❗ 困难及所需帮助
必须按 platform → project → 事项 的层级输出。
如果无明确困难或帮助事项，输出：本周无明确阻塞性问题或外部协助事项。

### 🙌 下一步计划
必须按 platform → project → 事项 的层级输出。
如果无明确下一步计划，输出：本周无明确下一步计划。

# 量化表达要求
1. 最终周报必须尽量保留输入中的数量、枚举项和具体对象名称。
2. 1~5 个对象必须全部列出名称；6 个及以上必须写明数量，并尽量列出关键项或代表项。
3. 不得将“完成 A、B、C、D、E 算法”压缩成“完成多个算法”。
4. 如果输入中已有数字，最终输出必须保留该数字。

# 风格要求
客观、中性、专业、简洁。不要输出规则本身，不要输出代码块，不要输出 JSON。

# 输入内容
{{markdown_content}}
"""


def get_default_coverage_check_prompt():
    return """你是周报覆盖性校验助手。

你的任务是：对照“结构化汇总 Markdown 草稿”和“最终周报 Markdown”，检查最终周报是否遗漏了输入中明确出现的 platform / project / subtopic / 事项。

# 输出要求
必须只输出合法 JSON，不要输出代码块，不要输出解释文字。

# 输出结构
{
  "pass": true,
  "missing_items": [
    {
      "section": "本周主要进展 / 困难及所需帮助 / 下一步计划 / 其他",
      "platform": "",
      "project_name": "",
      "subtopic": "",
      "missing_fact": "",
      "suggested_insert_position": ""
    }
  ],
  "wrong_or_suspicious_items": [
    {
      "section": "",
      "issue": ""
    }
  ]
}

# 判为遗漏或信息损失
1. 草稿中存在某个 project，但最终周报完全没有出现。
2. 草稿中存在明确困难或依赖，但最终周报未放入“困难及所需帮助”。
3. 草稿中存在明确下一步计划，但最终周报未放入“下一步计划”。
4. 草稿中存在 mention，最终周报保留了事实但删除了 mention。
5. 草稿中明确列出了 A、B、C、D、E，但最终周报只写成“多个”。
6. 草稿中明确写了数量，例如“完成 8 个 case”，但最终周报删除了数量。
7. 最终周报出现了草稿中没有的项目名、模块名、人员或事实。

# 结构化草稿
{{structured_markdown}}

# 最终周报
{{final_markdown}}
"""


def get_default_repair_prompt():
    return """你是周报修复助手。

你会收到：
1. 结构化汇总 Markdown 草稿
2. 当前最终周报 Markdown
3. 覆盖性校验结果 JSON

你的任务是：在尽量保持当前最终周报结构和语言风格的前提下，把校验结果指出的遗漏项补回最终周报。

# 最高优先级原则
1. 只补充结构化草稿中明确存在的事实。
2. 不允许新增结构化草稿中不存在的信息。
3. 不允许编造人名、mention、项目名、模块名、状态、风险等级、延期时间、资源申请。
4. 不要大规模重写整篇周报。
5. 不要删除当前最终周报中已有的有效内容。
6. 不要输出日期范围、周数、源日报链接等头部信息。
7. 不要输出解释文字，不要输出 JSON，只输出修复后的 Markdown 正文。

# 输出结构
请保持以下 4 个章节：
### 🎉 本周关键进展
### ✅ 本周主要进展
### ❗ 困难及所需帮助
### 🙌 下一步计划

# 结构化草稿
{{structured_markdown}}

# 当前最终周报
{{final_markdown}}

# 校验结果 JSON
{{validation_json}}
"""


# =============================================================================
# LLM：趋势分析 JSON
# =============================================================================
def build_trend_prompt(batch_state, project):
    prompt_template = get_prompt_text(project, "weekly_trend_prompt_file_guid", get_default_trend_prompt())
    compact_state = compact_timeline_state_for_llm(batch_state)
    return prompt_template.replace("{{batch_json}}", json.dumps(compact_state, ensure_ascii=False, indent=2))


def fallback_analyze_batch(batch_state):
    platform = batch_state.get("platform", "未标注平台")
    result = {"platform": platform, "core_progress": [], "main_progress": [], "issues_support": [], "next_plan": []}
    for project in batch_state.get("projects", []) or []:
        project_name = project.get("project_name", "未分类项目")
        for section_name, target_key in [("progress", "main_progress"), ("issues_support", "issues_support"), ("next_plan", "next_plan")]:
            grouped_items = []
            for day in project.get("sections", {}).get(section_name, []) or []:
                for item in day.get("items", []) or []:
                    tree_md = "\n".join(tree_to_markdown(item.get("content_tree", [])))
                    if not tree_md:
                        continue
                    grouped_items.append({
                        "mention": item.get("member", {}).get("mention_md") or item.get("member", {}).get("label", ""),
                        "summary": tree_md[:500],
                        "evidence_dates": [day.get("date")] if day.get("date") else [],
                        "source_item_ids": [item.get("item_id", "")],
                    })
            if grouped_items:
                result[target_key].append({"project_name": project_name, "subtopic": project_name, "items": grouped_items[:10]})
    result["core_progress"] = result["main_progress"][:3]
    return result


def analyze_trend_batch(batch_idx, total_batches, batch_state, project):
    project_name = project.get("project_name", "Unknown")
    platform = batch_state.get("platform", "未标注平台")
    print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(project_name)}][{safe_log_text(platform)}] 开始趋势分析，项目数: {len(batch_state.get('projects', []))}")
    prompt_text = build_trend_prompt(batch_state, project)
    trend_max_tokens = int(get_weekly_config(project, "weekly_trend_max_tokens", LLM_MAX_TOKENS))

    try:
        llm_result = call_llm(
            messages=[
                {"role": "system", "content": "你是项目周报趋势分析助手，只输出合法 JSON。"},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=trend_max_tokens,
            temperature=float(get_weekly_config(project, "weekly_trend_temperature", 0.1)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
        )
        parsed = safe_json_loads(llm_result)
        parsed.setdefault("platform", platform)
        print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(platform)}] ✅ 趋势 JSON 解析完成")
        return batch_idx, parsed
    except Exception as first_error:
        # 如果首次失败，可能是输出被截断，用 2x max_tokens 重试一次
        retry_max_tokens = trend_max_tokens * 2
        print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(platform)}] ⚠️ 首次解析失败({first_error})，以 max_tokens={retry_max_tokens} 重试...")
        try:
            llm_result = call_llm(
                messages=[
                    {"role": "system", "content": "你是项目周报趋势分析助手，只输出合法 JSON。输出务必简洁，避免冗余字段。"},
                    {"role": "user", "content": prompt_text},
                ],
                max_tokens=retry_max_tokens,
                temperature=float(get_weekly_config(project, "weekly_trend_temperature", 0.1)),
                stream=True,
                max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
            )
            parsed = safe_json_loads(llm_result)
            parsed.setdefault("platform", platform)
            print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(platform)}] ✅ 重试成功，趋势 JSON 解析完成")
            return batch_idx, parsed
        except Exception as retry_error:
            print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(platform)}] ⚠️ 重试仍失败({retry_error})，使用保底逻辑")
            return batch_idx, fallback_analyze_batch(batch_state)


def analyze_trends_in_parallel(timeline_state, project, max_projects_per_batch=DEFAULT_MAX_PROJECTS_PER_BATCH, max_parallel=10):
    batches = split_platform_projects_into_batches(timeline_state, max_projects_per_batch=max_projects_per_batch)
    total_batches = len(batches)
    if total_batches == 0:
        return []
    actual_parallel = min(max_parallel, total_batches)
    project_name = project.get("project_name", "Unknown")
    print(f"[Step 4][{project_name}] 开始 platform/project 分块趋势分析，共 {total_batches} 批，并行数: {actual_parallel}")
    indexed_results = {}
    with ThreadPoolExecutor(max_workers=actual_parallel) as executor:
        future_to_idx = {
            executor.submit(analyze_trend_batch, idx, total_batches, batch_state, project): idx
            for idx, batch_state in enumerate(batches, 1)
        }
        for future in as_completed(future_to_idx):
            batch_idx = future_to_idx[future]
            try:
                finished_idx, result = future.result()
                indexed_results[finished_idx] = result
            except Exception as e:
                print(f"    [Trend Batch {batch_idx}/{total_batches}] ❌ 处理失败: {e}")
                raise
    return [indexed_results[i] for i in range(1, total_batches + 1)]


# =============================================================================
# 趋势 JSON 合并与 Markdown 草稿生成
# =============================================================================
def normalize_analysis_item(item):
    project_name = (item.get("project_name") or "未分类项目").strip()
    subtopic = (item.get("subtopic") or project_name).strip()
    normalized_items = []
    if isinstance(item.get("items"), list) and item.get("items"):
        for child in item.get("items", []):
            summary = (child.get("summary") or "").strip()
            if not summary:
                continue
            normalized_items.append({
                "mention": (child.get("mention") or "").strip(),
                "summary": summary,
                "evidence_dates": child.get("evidence_dates", []) or item.get("evidence_dates", []) or [],
                "source_item_ids": child.get("source_item_ids", []) or item.get("source_item_ids", []) or [],
            })
    else:
        summary = (item.get("summary") or "").strip()
        if summary:
            normalized_items.append({
                "mention": (item.get("mention") or "").strip(),
                "summary": summary,
                "evidence_dates": item.get("evidence_dates", []) or [],
                "source_item_ids": item.get("source_item_ids", []) or [],
            })
    return {"project_name": project_name, "subtopic": subtopic, "items": normalized_items}


def merge_trend_results(batch_results):
    platform_map = OrderedDict()
    section_keys = ["core_progress", "main_progress", "issues_support", "next_plan"]
    for result in batch_results or []:
        platform = result.get("platform") or "未标注平台"
        if platform not in platform_map:
            platform_map[platform] = {key: [] for key in section_keys}
        for key in section_keys:
            for item in result.get(key, []) or []:
                normalized = normalize_analysis_item(item)
                if normalized.get("items"):
                    platform_map[platform][key].append(normalized)
    return platform_map


def dedupe_items(items):
    seen = set()
    deduped = []
    for item in items or []:
        item_key = (
            item.get("project_name"),
            item.get("subtopic"),
            tuple((x.get("mention", ""), x.get("summary", "")) for x in item.get("items", [])),
        )
        if item_key in seen:
            continue
        seen.add(item_key)
        deduped.append(item)
    return deduped


def render_platform_section(platform_map, section_key, empty_text="本周暂无明确内容。"):
    parts = []
    any_content = False
    for platform, sections in platform_map.items():
        items = dedupe_items(sections.get(section_key, []) or [])
        if not items:
            continue
        any_content = True
        parts.append(f"- **{platform}**")
        grouped_by_project = OrderedDict()
        for item in items:
            project_name = item.get("project_name", "未分类项目")
            grouped_by_project.setdefault(project_name, [])
            grouped_by_project[project_name].append(item)
        for project_name, project_items in grouped_by_project.items():
            parts.append(f"    - **{project_name}**")
            grouped_by_subtopic = OrderedDict()
            for item in project_items:
                subtopic = item.get("subtopic") or project_name
                grouped_by_subtopic.setdefault(subtopic, [])
                grouped_by_subtopic[subtopic].extend(item.get("items", []))
            for subtopic, action_items in grouped_by_subtopic.items():
                if subtopic and subtopic != project_name:
                    parts.append(f"        - **{subtopic}**")
                    leaf_indent = "            "
                else:
                    leaf_indent = "        "
                seen_actions = set()
                for action in action_items:
                    mention = (action.get("mention") or "").strip()
                    summary = (action.get("summary") or "").strip()
                    if not summary:
                        continue
                    action_key = (mention, summary)
                    if action_key in seen_actions:
                        continue
                    seen_actions.add(action_key)
                    parts.append(f"{leaf_indent}- {mention + ' ' if mention else ''}{summary}")
        parts.append("")
    if not any_content:
        return empty_text
    return "\n".join(parts).strip()


def build_structured_body_markdown(platform_map):
    parts = []
    parts.append("### 🎉 本周关键进展")
    parts.append(render_platform_section(platform_map, "core_progress", empty_text="本周暂无核心产出。"))
    parts.append("")
    parts.append("### ✅ 本周主要进展")
    parts.append(render_platform_section(platform_map, "main_progress", empty_text="本周暂无明确主要进展。"))
    parts.append("")
    parts.append("### ❗ 困难及所需帮助")
    parts.append(render_platform_section(platform_map, "issues_support", empty_text="本周无明确阻塞性问题或外部协助事项。"))
    parts.append("")
    parts.append("### 🙌 下一步计划")
    parts.append(render_platform_section(platform_map, "next_plan", empty_text="本周无明确下一步计划。"))
    return "\n".join(parts).strip()


def build_header_markdown(weekly_pool):
    metadata = weekly_pool.get("metadata", {}) or {}
    range_dates = metadata.get("range_dates", []) or []
    source_urls = metadata.get("source_urls", {}) or {}
    week_number = metadata.get("week_number", "")
    start_date = range_dates[0] if range_dates else ""
    end_date = range_dates[-1] if range_dates else ""
    header_parts = [
        f"**日期范围：** {start_date} 至 {end_date} | **周数：** 第 {week_number} 周",
        "",
        "**源日报链接：**",
    ]
    for report_date in sorted(source_urls.keys()):
        source_url = source_urls.get(report_date, "")
        header_parts.append(f"- {report_date}: [{source_url}]({source_url})")
    header_parts.append("")
    header_parts.append("---")
    return "\n".join(header_parts).strip()


def generate_final_weekly_body(structured_body_markdown, project):
    prompt_template = get_prompt_text(project, "weekly_final_prompt_file_guid", get_default_final_weekly_prompt())
    prompt_text = prompt_template.replace("{{markdown_content}}", structured_body_markdown)
    try:
        result = call_llm(
            messages=[
                {"role": "system", "content": "你是严谨、客观、专业的项目周报整理助手，请只输出 Markdown 正文。"},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=int(get_weekly_config(project, "weekly_final_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(get_weekly_config(project, "weekly_final_temperature", 0.2)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
        )
        cleaned = strip_markdown_wrapper(result)
        return cleaned or structured_body_markdown
    except Exception as e:
        print(f"    ⚠️ 最终周报整理模型调用失败，使用结构化草稿作为正文: {e}")
        return structured_body_markdown


def validate_weekly_coverage(structured_body_markdown, final_body_markdown, project):
    prompt_template = get_prompt_text(project, "weekly_validation_prompt_file_guid", get_default_coverage_check_prompt())
    prompt_text = prompt_template.replace("{{structured_markdown}}", structured_body_markdown).replace("{{final_markdown}}", final_body_markdown)
    try:
        result = call_llm(
            messages=[
                {"role": "system", "content": "你是周报覆盖性校验助手，只输出合法 JSON。"},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=int(get_weekly_config(project, "weekly_validation_max_tokens", 2048)),
            temperature=float(get_weekly_config(project, "weekly_validation_temperature", 0.0)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
        )
        parsed = safe_json_loads(result)
        parsed.setdefault("pass", True)
        parsed.setdefault("missing_items", [])
        parsed.setdefault("wrong_or_suspicious_items", [])
        return parsed
    except Exception as e:
        print(f"    ⚠️ 覆盖性校验失败，跳过校验: {e}")
        return {"pass": True, "missing_items": [], "wrong_or_suspicious_items": [], "skipped": True}


def repair_weekly_body(structured_body_markdown, final_body_markdown, validation_result, project):
    prompt_template = get_prompt_text(project, "weekly_repair_prompt_file_guid", get_default_repair_prompt())
    prompt_text = (
        prompt_template
        .replace("{{structured_markdown}}", structured_body_markdown)
        .replace("{{final_markdown}}", final_body_markdown)
        .replace("{{validation_json}}", json.dumps(validation_result, ensure_ascii=False, indent=2))
    )
    try:
        result = call_llm(
            messages=[
                {"role": "system", "content": "你是周报修复助手，请只输出修复后的 Markdown 正文。"},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=int(get_weekly_config(project, "weekly_repair_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(get_weekly_config(project, "weekly_repair_temperature", 0.1)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
        )
        cleaned = strip_markdown_wrapper(result)
        return cleaned or final_body_markdown
    except Exception as e:
        print(f"    ⚠️ 周报修复模型调用失败，保留原最终正文: {e}")
        return final_body_markdown


def generate_checked_final_body(structured_body_markdown, project):
    final_body = generate_final_weekly_body(structured_body_markdown, project)
    enable_check = get_weekly_config(project, "enable_weekly_coverage_check", True)
    if not enable_check:
        return final_body, {"pass": True, "missing_items": [], "wrong_or_suspicious_items": [], "skipped": True}
    validation_result = validate_weekly_coverage(structured_body_markdown, final_body, project)
    missing_items = validation_result.get("missing_items", []) or []
    suspicious_items = validation_result.get("wrong_or_suspicious_items", []) or []
    if validation_result.get("pass", True) and not missing_items and not suspicious_items:
        print("    ✅ 覆盖性校验通过")
        return final_body, validation_result
    print(f"    ⚠️ 覆盖性校验发现遗漏/可疑项：missing={len(missing_items)}, suspicious={len(suspicious_items)}，开始修复")
    repaired_body = repair_weekly_body(structured_body_markdown, final_body, validation_result, project)
    if get_weekly_config(project, "enable_weekly_second_validation", False):
        second_validation = validate_weekly_coverage(structured_body_markdown, repaired_body, project)
        return repaired_body, second_validation
    return repaired_body, validation_result


def build_final_markdown(weekly_pool, platform_map, project=None):
    structured_body = build_structured_body_markdown(platform_map)
    if project is None:
        final_body = structured_body
        validation_result = {"pass": True, "missing_items": [], "wrong_or_suspicious_items": [], "skipped": True}
    else:
        final_body, validation_result = generate_checked_final_body(structured_body, project)
    header = build_header_markdown(weekly_pool)
    return (header + "\n\n" + final_body.strip()).strip(), structured_body, validation_result


# =============================================================================
# 笔记创建、写入、消息推送
# =============================================================================
def insert_markdown_to_note(user_guid, note_guid, markdown_content, max_retries=3):
    """
    写入 Markdown 到 Workspace 笔记。

    对齐原子池周报脚本接口要求：
    POST /middle/server/api/file/md/insert
    payload:
    {
        "note_guid": note_guid,
        "markdown_content": html_content,
        "mode": "w",
        "location": 1
    }
    """
    clean_content = strip_markdown_wrapper(markdown_content)
    html_content = _convert_special_nodes(clean_content)

    response = _request_with_retry(
        "post",
        BASE_URL + MD_INSERT_ROUTE,
        max_retries=max_retries,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={
            "note_guid": note_guid,
            "markdown_content": html_content,
            "mode": "w",
            "location": 1
        },
        timeout=60,
    )

    if response.status_code != 200:
        raise Exception(f"写入笔记失败: {response.text}")

    return response.json()


def create_workspace_note(user_guid, project_guid, parent_guid, title, tags=None, content=""):
    """
    创建 Workspace 周报文档。

    对齐原子池周报脚本接口要求：
    POST /middle/server/api/workspace/save
    payload:
    {
        "project_guid": project_guid,
        "parent_guid": parent_guid,
        "target": {
            "name": title,
            "type": 1,
            "tags": tags
        },
        "creator_guid": user_guid
    }

    如果传入 content，则创建后立即写入；当前主流程一般是先创建，再调用 insert_markdown_to_note 写入。
    """
    tags = tags or ["周报", "AI"]
    creator_guid = user_guid or USER_GUID

    if not project_guid:
        raise ValueError("target_project_guid 不能为空！")

    headers = get_headers_with_ak()
    headers["X-User-GUID"] = creator_guid

    response = _request_with_retry(
        "post",
        BASE_URL + WORKSPACE_SAVE_ROUTE,
        max_retries=3,
        headers=headers,
        json={
            "project_guid": project_guid,
            "parent_guid": parent_guid,
            "target": {
                "name": title,
                "type": 1,
                "tags": tags
            },
            "creator_guid": creator_guid
        },
        timeout=60,
    )

    result = response.json()
    if response.status_code != 200 or not result.get("data"):
        raise Exception(f"创建笔记 API 返回错误: {result}")

    note_guid = (result.get("data") or {}).get("guid")
    if not note_guid:
        raise Exception(f"创建周报文档失败或无法识别 note_guid: {result}")

    if content:
        try:
            insert_markdown_to_note(creator_guid, note_guid, content, max_retries=5)
        except Exception as e:
            print(f"    ⚠️ 笔记已创建(note_guid={note_guid})但内容写入失败: {e}")
            print("    → 将在 5s 后单独重试写入.")
            time.sleep(5)
            try:
                insert_markdown_to_note(creator_guid, note_guid, content, max_retries=5)
                print("    ✅ 重试写入成功")
            except Exception as e2:
                print(f"    ❌ 重试写入仍失败: {e2}，笔记已创建但内容为空，note_guid={note_guid}")

    return note_guid, result


def get_or_create_weekly_note(project, week_info):
    dept_name = project.get("dept_name", "") or project.get("project_name", "Unknown")
    title = build_weekly_note_title(week_info, dept_name)
    user_guid = project.get("weekly_target_user_guid") or project.get("user_guid") or project.get("leader_guid") or USER_GUID

    # 优先直接写入指定 weekly_note_guid。
    if project.get("weekly_note_guid"):
        return project["weekly_note_guid"], title, f"{BASE_URL}/workspace/{project['weekly_note_guid']}"

    target_project_guid = (
        project.get("weekly_target_project_guid")
        # 兼容旧字段，建议新配置统一使用 weekly_target_project_guid
        or project.get("weekly_output_project_guid")
        or project.get("output_project_guid")
        or project.get("project_guid")
    )
    target_parent_guid = project.get("weekly_target_parent_guid") or "0"
    if get_weekly_config(project, "write_back", True) and not project.get("weekly_target_parent_guid"):
        print(f"    ⚠️ 未配置 weekly_target_parent_guid，将默认创建到根目录 parent_guid=0")
    if not target_project_guid:
        raise ValueError("未配置 weekly_note_guid，且缺少 weekly_target_project_guid，无法创建周报")

    note_guid, _ = create_workspace_note(
        user_guid=user_guid,
        project_guid=target_project_guid,
        parent_guid=target_parent_guid,
        title=title,
        tags=get_weekly_config(project, "weekly_note_tags", ["周报", "AI"])
    )
    return note_guid, title, f"{BASE_URL}/workspace/{note_guid}"


def build_message_text(note_title, note_url):
    return f"【{note_title}】已生成，请点击查看。\n<a href='{note_url}'>点击查看详情</a>"


def send_message_api(receiver_guids, title, content, sender_guid="", interactive_content=None):
    payload = {
        "template_id": MESSAGE_TEMPLATE_ID,
        "receiver_guid": receiver_guids,
        "content": content,
        "org_guid": ORG_GUID,
        "title": title,
        "platform_type": PLATFORM_TYPE,
    }
    if interactive_content is not None:
        payload["interactive_content"] = json.dumps(interactive_content, ensure_ascii=False)
    return requests.post(
        url=BASE_URL + MESSAGE_SEND_ROUTE,
        headers=get_headers_with_ak(user_guid=sender_guid),
        json=payload,
        timeout=30,
    )


def get_default_card_prompt():
    return """# Role
你是一位资深项目经理助手，专门将完整项目周报压缩为适合飞书卡片展示的短摘要。

# Task
你会收到一份完整项目周报 Markdown。你的任务是将其改写为适合飞书卡片展示的精简摘要。

# Input
{{weekly_markdown}}

# Output
只输出 Markdown 文本，不要输出代码块，不要输出解释，不要输出 JSON。

# 最高优先级原则
1. 绝对匿名：删除所有人名、普通 @姓名、以及 `[@姓名](mention:uid:id)` mention。
2. 使用无主语句，例如“完成 XX 验证”“推进 XX 联调”“识别 XX 风险”。
3. 卡片适配：短句、少层级、每条 bullet 尽量一行。
4. 覆盖核心，不求全量：优先保留关键完成事项、关键里程碑、明确量化结果、关键风险、下周重点计划。
5. 不得编造周报中不存在的事实、风险、数量、项目名或平台名。
6. 如果完整周报中出现明确数字，卡片中应尽量保留。

# 推荐输出结构
请严格使用以下结构：

### 📌 本周亮点
- ...

### ⚠️ 风险 / 需关注
- ...

### 📝 下周重点
- ...

# 约束
1. 本周亮点最多 5 条。
2. 风险 / 需关注最多 3 条；如果无明确风险，输出“- 暂无明确阻塞性风险”。
3. 下周重点最多 4 条。
4. 不要输出日期范围、源日报链接、source_item_ids、evidence_dates。
5. 不要保留任何人名或 mention。
"""


def generate_card_content(project, weekly_markdown, week_info=None):
    """使用 weekly_card_prompt_file_guid 将完整周报压缩为飞书卡片摘要。"""
    if week_info is None:
        week_info = get_last_week_info()
    start_date = week_info["start_date"]
    end_date = week_info["end_date"]
    summary_prefix = f"**本周摘要 | {start_date} 至 {end_date}**\n\n"

    meta_header = f"时间范围：{start_date} 至 {end_date} | 第{week_info['week_number']}周"

    prompt_template = get_prompt_text(project, "weekly_card_prompt_file_guid", get_default_card_prompt())
    card_input_limit = int(get_weekly_config(project, "weekly_card_input_max_chars", 0))
    if card_input_limit and card_input_limit > 0:
        card_source_markdown = weekly_markdown[:card_input_limit]
    else:
        card_source_markdown = weekly_markdown
    card_input_markdown = f"{meta_header}\n\n{card_source_markdown}"
    prompt_text = (
        prompt_template
        .replace("{{weekly_markdown}}", card_input_markdown)
        .replace("{{markdown_content}}", card_input_markdown)
    )

    try:
        result = call_llm(
            messages=[
                {"role": "system", "content": "你是飞书卡片摘要助手，请输出匿名、简洁、适合卡片展示的 Markdown。"},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=int(get_weekly_config(project, "weekly_card_max_tokens", 2048)),
            temperature=float(get_weekly_config(project, "weekly_card_temperature", 0.2)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES)),
        )
        cleaned = strip_markdown_wrapper(result)
        return summary_prefix + (cleaned or fallback_card_content(weekly_markdown))
    except Exception as e:
        print(f"    ⚠️ 飞书卡片摘要生成失败，使用兜底摘要: {e}")
        return summary_prefix + fallback_card_content(weekly_markdown)


def fallback_card_content(weekly_markdown, max_len=1200):
    """兜底：不调用模型时的卡片正文。注意：无法严格匿名，仅用于模型失败兜底。"""
    content = strip_markdown_wrapper(weekly_markdown)
    # 尽量移除 mention markdown，避免卡片暴露人员信息
    content = re.sub(r"\[@[^\]]+\]\(mention:[^)]+\)", "", content)
    content = re.sub(r"@[^\s，。；、:：)）]+", "", content)
    content = re.sub(r"\n{2,}", "\n", content).strip()
    return content[:max_len] + ("\n..." if len(content) > max_len else "")


def build_feishu_card(title, note_url, card_summary, source_note_entries=None):
    elements = [
        {
            "tag": "markdown",
            "content": card_summary,
            "margin": "0px",
            "text_size": "normal",
        }
    ]

    if source_note_entries:
        elements.append({"tag": "hr"})
        total_count = len(source_note_entries)
        display_entries = source_note_entries[:5]
        has_more = total_count > 5

        elements.append({
            "tag": "markdown",
            "content": f"**源日报入口**（共 {total_count} 篇）",
            "margin": "0px",
            "text_size": "normal",
        })

        button_items = []
        for item in display_entries:
            date_text = item.get("date", "")
            short_date = date_text[5:] if len(date_text) >= 10 else date_text
            button_items.append({
                "text": f"{short_date} 日报",
                "url": item.get("url", ""),
                "type": "default",
            })

        if has_more:
            button_items.append({
                "text": "更多日报",
                "url": note_url,
                "type": "default",
            })

        for i in range(0, len(button_items), 2):
            pair = button_items[i:i + 2]
            columns = []
            for btn in pair:
                columns.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "type": btn.get("type", "default"),
                            "width": "fill",
                            "margin": "4px 0px 4px 0px",
                            "text": {
                                "tag": "plain_text",
                                "content": btn.get("text", "查看"),
                            },
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": btn.get("url", ""),
                                }
                            ],
                        }
                    ],
                })

            if len(columns) == 1:
                columns.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [],
                })

            elements.append({
                "tag": "column_set",
                "flex_mode": "stretch",
                "horizontal_spacing": "8px",
                "margin": "0px",
                "columns": columns,
            })

    elements.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "8px",
        "margin": "8px 0px 0px 0px",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "button",
                        "type": "primary_filled",
                        "width": "fill",
                        "margin": "4px 0px 4px 0px",
                        "text": {
                            "tag": "plain_text",
                            "content": "查看完整周报",
                        },
                        "behaviors": [
                            {
                                "type": "open_url",
                                "default_url": note_url,
                            }
                        ],
                    }
                ],
            }
        ],
    })

    return {
        "schema": "2.0",
        "header": {
            "padding": "12px 8px 12px 8px",
            "template": "orange",
            "title": {"content": title, "tag": "plain_text"},
        },
        "body": {
            "vertical_spacing": "12px",
            "elements": elements,
        },
    }


def send_webhook(webhook_url, card):
    response = requests.post(
        url=webhook_url,
        headers={"Content-Type": "application/json"},
        json={"msg_type": "interactive", "card": card},
        timeout=30,
    )
    return response.json()


def send_weekly_messages(note_url, note_title, project, final_markdown, source_note_entries=None, week_info=None):
    try:
        webhook_urls = normalize_to_list(project.get("weekly_webhook_url", []))
        receiver_guids = normalize_to_list(project.get("weekly_sender_guid", []))
        sender_guid = project.get("weekly_target_user_guid", "") or project.get("user_guid") or USER_GUID
        if not webhook_urls and not receiver_guids:
            return

        card_summary = generate_card_content(project, final_markdown, week_info=week_info)
        card = build_feishu_card(note_title, note_url, card_summary, source_note_entries=source_note_entries)

        if webhook_urls:
            for webhook_url in webhook_urls:
                try:
                    result = send_webhook(webhook_url, card)
                    print(f"    ✅ Webhook 推送完成: {result}")
                except Exception as e:
                    print(f"    ⚠️ Webhook 推送失败: {e}")

        if receiver_guids:
            content = build_message_text(note_title, note_url)
            response = send_message_api(receiver_guids, note_title, content, sender_guid=sender_guid, interactive_content=card)
            try:
                print(f"    ✅ 消息推送完成: {response.json()}")
            except Exception:
                print(f"    ✅ 消息推送完成: status={response.status_code}")
    except Exception as e:
        print(f"    ⚠️ 消息推送失败: {e}")


# =============================================================================
# 单项目处理主链路
# =============================================================================
def process_weekly_project(project):
    project_name = project.get("project_name", "Unknown")
    generated_files = []
    print("=" * 80)
    print(f"🚀 开始处理非原子池周报项目: {safe_log_text(project_name)}")

    try:
        week_info = get_last_week_info()
        weekly_pool, ok, files, source_note_entries = load_weekly_raw_daily_pool(project)
        generated_files.extend(files)
        if not ok:
            print(f"[Skip][{project_name}] 未生成 weekly_pool，跳过")
            return False

        print(f"[Step 3][{project_name}] 构建 timeline_state")
        timeline_state = build_weekly_timeline_state(weekly_pool)
        timeline_path = build_intermediate_json_file(
            project_guid=project.get("project_guid") or project.get("raw_daily_project_guid") or "project",
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            json_content=timeline_state,
            suffix="timeline_state",
        )
        generated_files.append(timeline_path)
        print(f"[Step 3][{project_name}] 📦 timeline_state 已生成: {timeline_path}")

        max_projects_per_batch = int(get_weekly_config(project, "weekly_projects_per_batch", DEFAULT_MAX_PROJECTS_PER_BATCH))
        max_parallel = int(get_weekly_config(project, "weekly_max_parallel", 10))
        batch_results = analyze_trends_in_parallel(
            timeline_state=timeline_state,
            project=project,
            max_projects_per_batch=max_projects_per_batch,
            max_parallel=max_parallel,
        )
        platform_map = merge_trend_results(batch_results)

        trend_path = build_intermediate_json_file(
            project_guid=project.get("project_guid") or project.get("raw_daily_project_guid") or "project",
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            json_content={"batch_results": batch_results, "platform_map": platform_map},
            suffix="trend_results",
        )
        generated_files.append(trend_path)
        print(f"[Step 5][{project_name}] 📦 trend_results 已生成: {trend_path}")

        final_markdown, structured_body, validation_result = build_final_markdown(weekly_pool, platform_map, project)
        structured_path = build_intermediate_markdown_file(
            project_guid=project.get("project_guid") or project.get("raw_daily_project_guid") or "project",
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            markdown_content=structured_body,
            suffix="structured_body",
        )
        final_path = build_intermediate_markdown_file(
            project_guid=project.get("project_guid") or project.get("raw_daily_project_guid") or "project",
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            markdown_content=final_markdown,
            suffix="final_weekly",
        )
        generated_files.extend([structured_path, final_path])
        print(f"[Step 6][{project_name}] 📄 结构化草稿: {structured_path}")
        print(f"[Step 6][{project_name}] 📄 最终周报: {final_path}")
        print(f"[Step 6][{project_name}] coverage_result={json.dumps(validation_result, ensure_ascii=False)[:500]}")

        should_write_back = bool(get_weekly_config(project, "write_back", True))
        if should_write_back:
            note_guid, note_title, note_url = get_or_create_weekly_note(project, week_info)
            user_guid = project.get("weekly_target_user_guid") or project.get("user_guid") or project.get("leader_guid") or USER_GUID
            insert_result = insert_markdown_to_note(user_guid=user_guid, note_guid=note_guid, markdown_content=final_markdown)
            print(f"[Step 7][{project_name}] ✅ 周报写入完成: {note_url}, result={insert_result}")
            send_weekly_messages(note_url, note_title, project, final_markdown, source_note_entries=source_note_entries, week_info=week_info)
        else:
            print(f"[Step 7][{project_name}] write_back=false，仅生成本地临时文件")

        if bool(get_weekly_config(project, "cleanup_temp_files", False)):
            cleanup_temp_files(generated_files, project_name=project_name)

        print(f"✅ 项目处理完成: {safe_log_text(project_name)}")
        return True

    except Exception as e:
        print(f"❌ 项目处理失败: {safe_log_text(project_name)}, error={e}")
        traceback.print_exc()
        return False


# =============================================================================
# 主入口
# =============================================================================

# =============================================================================
# 云端平台入口：直接顺序执行，不使用 main()
# =============================================================================
if not projects:
    raise ValueError("config.projects 不能为空")

success = 0
failed = 0
for project in projects:
    ok = process_weekly_project(project)
    if ok:
        success += 1
    else:
        failed += 1

print("=" * 80)
print(f"📌 非原子池周报任务完成：success={success}, failed={failed}")
