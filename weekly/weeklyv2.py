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
# 日志编码兜底：尽量避免中文日志在平台环境中出现 mojibake
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

generate_type = "weekly"


# =============================================================================
# 配置读取：全局默认 + project 覆盖
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
    """
    兼容字符串/列表：
    - "" / None -> []
    - "abc" -> ["abc"]
    - ["a", "b"] -> ["a", "b"]
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [x for x in value if x]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return []


# =============================================================================
# OpenAI SDK 直连模型配置
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

openai_client = OpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
)


# =============================================================================
# API 路由
# =============================================================================
ACCESS_TOKEN_ROUTE = "/api/user/platform/getAccessToken"
NOTE_JSON_ROUTE = "/platform/ws/noteInfo/getDocJson"
DOC_TREE_ROUTE = "/platform/api/main/doc/treeList"
SIGNED_URL_ROUTE = "/platform/api/main/storage/getSignedUrl"

WORKSPACE_SAVE_ROUTE = "/middle/server/api/workspace/save"
MD_INSERT_ROUTE = "/middle/server/api/file/md/insert"
MESSAGE_SEND_ROUTE = "/middle/server/api/msg/send"


# =============================================================================
# 默认业务参数
# =============================================================================
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
        timeout=30
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


def get_note_json_content(user_guid="", doc_id=""):
    headers = get_headers_with_ak(user_guid=user_guid, doc_id=doc_id)
    response = requests.get(
        url=BASE_URL + NOTE_JSON_ROUTE,
        headers=headers,
        params={"docId": doc_id},
        timeout=60
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


def safe_json_loads(text):
    clean_text = strip_markdown_wrapper(text)

    try:
        return json.loads(clean_text)
    except Exception:
        pass

    # 优先匹配对象，其次匹配数组，兼容模型输出解释文字
    object_match = re.search(r"(\{.*\})", clean_text, flags=re.DOTALL)
    if object_match:
        return json.loads(object_match.group(1))

    array_match = re.search(r"(\[.*\])", clean_text, flags=re.DOTALL)
    if array_match:
        return json.loads(array_match.group(1))

    raise ValueError("无法解析 JSON")


def get_json_file_content(category_guid):
    """
    读取 treeList 中 dataType == 5 的 .json 文件。
    dataType == 5 通常是文件节点，因此走 getSignedUrl 下载文件内容。
    """
    if not category_guid:
        raise ValueError("category_guid 不能为空")

    signed_url_response = requests.get(
        BASE_URL + SIGNED_URL_ROUTE,
        headers=get_headers_with_ak(),
        params={"categoryGuid": category_guid},
        timeout=30
    )
    signed_url_json = signed_url_response.json()
    signed_url = (signed_url_json.get("data") or {}).get("signedUrl")
    if not signed_url:
        raise Exception(f"获取 JSON 文件 signedUrl 失败: {signed_url_json}")

    file_response = requests.get(signed_url, timeout=60)
    if file_response.status_code != 200:
        raise Exception(f"下载 JSON 文件失败: status={file_response.status_code}, text={file_response.text[:300]}")

    text = file_response.text.strip()
    if not text:
        raise ValueError("JSON 文件内容为空")

    return safe_json_loads(text)


def _convert_special_nodes(content):
    """
    将 markdown mention / mentionUrl 转换为 workspace 可识别的节点。
    """
    content = re.sub(
        r"\[@([^\]]*)\]\(mention:[^:]+:([^)]+)\)",
        lambda m: f'<span data-node-type="mention" data-guid="{m.group(2)}"></span>',
        content
    )

    content = re.sub(
        r"\[([^\]]+)\]\(mentionUrl:[^:]+:[^:]+:([^)]+)\)",
        lambda m: f'<a data-node-type="mentionUrl" data-url="{m.group(2)}">{m.group(1)}</a>',
        content
    )

    content = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        content
    )

    content = re.sub(
        r":::highlight\[[^\]]*\]\n(.*?):::",
        lambda m: f"<div data-node-type='highlightBlock' data-content-markdown>\n{m.group(1).rstrip()}\n</div>",
        content,
        flags=re.DOTALL
    )

    return content


def build_message_text(note_title, note_url):
    return f"【{note_title}】已生成，请点击查看。\n<a href='{note_url}'>点击查看详情</a>"


def load_prompt_text(prompt_file_guid, default_prompt):
    if not prompt_file_guid:
        return default_prompt

    try:
        signed_url_response = requests.get(
            BASE_URL + SIGNED_URL_ROUTE,
            headers=get_headers_with_ak(),
            params={"categoryGuid": prompt_file_guid},
            timeout=30
        )
        signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")
        if not signed_url:
            return default_prompt

        return requests.get(signed_url, timeout=20).text
    except Exception as e:
        print(f"⚠️ Prompt 文件读取失败，使用默认 prompt: {e}")
        return default_prompt


def get_last_week_info():
    """
    固定搜索上周一到上周日 7 天，覆盖节假日无日报、调休周末有日报的情况。
    """
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


def build_weekly_note_title(week_info, project_name):
    year = week_info["start_date"][:4]
    week_number = week_info["week_number"]
    return f"{year}#W{week_number:02d} {project_name}周报"


def build_intermediate_markdown_file(project_guid, target_date_str, markdown_content):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    file_name = f"weekly_atomic_{project_guid}_{target_date_str.replace('-', '')}_{unique_suffix}.md"
    file_path = os.path.join(tmp_dir, file_name)

    with open(file_path, "w", encoding="utf-8") as output_fp:
        output_fp.write(markdown_content)

    return file_path


def build_intermediate_json_file(project_guid, target_date_str, json_content, suffix=""):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    name_suffix = f"_{suffix}" if suffix else ""
    file_name = f"weekly_atomic_{project_guid}_{target_date_str.replace('-', '')}{name_suffix}_{unique_suffix}.json"
    file_path = os.path.join(tmp_dir, file_name)

    with open(file_path, "w", encoding="utf-8") as output_fp:
        json.dump(json_content, output_fp, ensure_ascii=False, indent=2)

    return file_path


def cleanup_temp_files(file_paths, project_name=""):
    if not file_paths:
        return

    for file_path in file_paths:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                prefix = f"[Cleanup][{project_name}]" if project_name else "[Cleanup]"
                print(f"{prefix} 🧹 已删除临时文件: {file_path}")
        except Exception as e:
            prefix = f"[Cleanup][{project_name}]" if project_name else "[Cleanup]"
            print(f"{prefix} ⚠️ 删除临时文件失败: {file_path}, error={e}")


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


# =============================================================================
# OpenAI SDK 模型调用
# =============================================================================
def call_llm(
    messages,
    max_tokens=None,
    temperature=None,
    stream=True,
    max_retries=None,
    print_stream=None,
):
    """
    OpenAI SDK 直连模型调用。
    stream=True 时，一边接收 delta，一边拼接完整结果返回。
    """
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
# 原子池读取与解析
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


def _is_json_atomic_file_node(node):
    node_type = node.get("dataType", node.get("type"))
    try:
        is_file = int(node_type) == 5
    except Exception:
        is_file = str(node_type) == "5"

    title = _get_tree_node_title(node)
    return is_file and title.lower().endswith(".json")


def _infer_date_from_title(title, date_list):
    title = title or ""
    for date_str in date_list:
        variants = [
            date_str,
            date_str.replace("-", "/"),
            date_str.replace("-", "."),
            date_str.replace("-", ""),
        ]
        if any(v in title for v in variants):
            return date_str
    return ""


def find_weekly_atomic_notes(user_guid, project_guid, folder_guid, date_list):
    """
    从目标文件夹中查找原子池 JSON 文件：
    - dataType == 5
    - .json 后缀
    - 文件名命中上周 7 天日期时先记录日期；否则后续用 meta.date / items.date 判断
    """
    response = requests.post(
        url=BASE_URL + DOC_TREE_ROUTE,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={"projectGuid": project_guid, "parentGuid": folder_guid},
        timeout=60
    )

    response_json = response.json()
    note_list = response_json.get("data") or []
    matched_notes = []

    for note in note_list:
        if not _is_json_atomic_file_node(note):
            continue

        note_title = _get_tree_node_title(note)
        note_guid = _get_tree_node_guid(note)
        if not note_guid:
            continue

        inferred_date = _infer_date_from_title(note_title, date_list)
        matched_notes.append({
            "date": inferred_date,
            "categoryGuid": note_guid,
            "dataTitle": note_title,
            "node_type": note.get("dataType", note.get("type"))
        })

    return matched_notes


def extract_text_from_note_json_node(node, parts):
    """
    兼容历史：如果原子池写在文档正文而不是 dataType==5 文件，尽量抽取正文文本解析 JSON。
    """
    if isinstance(node, dict):
        node_type = node.get("type")

        if node_type == "text":
            text = node.get("text", "")
            if text:
                parts.append(text)
            return

        if node_type == "mention":
            attrs = node.get("attrs", {}) or {}
            label = attrs.get("label", "?")
            uid = attrs.get("uid", "")
            user_id = attrs.get("id", "")
            parts.append(f"[@{label}](mention:{uid}:{user_id})")
            return

        if node_type == "mentionUrl":
            attrs = node.get("attrs", {}) or {}
            content = attrs.get("content", "")
            original_url = attrs.get("originalUrl", "")
            uid = attrs.get("uid", "")
            data_type = attrs.get("dataType", 1)
            parts.append(f"[{content}](mentionUrl:{uid}:{data_type}:{original_url})")
            return

        if node_type in ("paragraph", "heading", "fheading", "bulletListItem", "numberedListItem", "codeBlock"):
            before_len = len(parts)
            for child in node.get("content", []) or []:
                extract_text_from_note_json_node(child, parts)
            if len(parts) > before_len:
                parts.append("\n")
            return

        for key in ("text", "code"):
            if isinstance(node.get(key), str) and node.get(key).strip():
                parts.append(node.get(key))
                parts.append("\n")
                return

        for child in node.get("content", []) or []:
            extract_text_from_note_json_node(child, parts)

    elif isinstance(node, list):
        for child in node:
            extract_text_from_note_json_node(child, parts)


def extract_text_from_note_json(raw_note_json):
    root = raw_note_json.get("data", {}).get("content", []) or raw_note_json.get("content", [])
    parts = []
    extract_text_from_note_json_node(root, parts)
    return "".join(parts).strip()


def parse_atomic_pool_from_note_json(raw_note_json):
    if isinstance(raw_note_json, dict) and "items" in raw_note_json:
        return raw_note_json
    if isinstance(raw_note_json.get("data"), dict) and "items" in raw_note_json.get("data", {}):
        return raw_note_json["data"]

    note_text = extract_text_from_note_json(raw_note_json)
    if not note_text:
        raise ValueError("笔记内容为空，无法解析原子池 JSON")

    return safe_json_loads(note_text)


def load_weekly_atomic_pool(project):
    generated_files = []
    week_info = get_last_week_info()
    project_name = project.get("project_name", "Unknown")

    state_project_guid = (
        project.get("state_project_guid")
        or project.get("state_target_project_guid")
        or project.get("weekly_atomic_pool_project_guid")
        or project.get("atomic_pool_project_guid")
        or project.get("project_guid")
    )

    state_parent_guid = (
        project.get("state_parent_guid")
        or project.get("state_target_parent_guid")
        or project.get("weekly_atomic_pool_folder_guid")
        or project.get("atomic_pool_folder_guid")
        or project.get("work_log_folder_guid")
    )

    if not state_project_guid:
        raise ValueError(f"配置错误: project '{project_name}' 缺少 state_project_guid / project_guid")
    if not state_parent_guid:
        raise ValueError(f"配置错误: project '{project_name}' 缺少 state_parent_guid / weekly_atomic_pool_folder_guid")

    project_user_guids = project.get(
        "user_guid_list",
        [project.get("user_guid") or project.get("leader_guid") or USER_GUID]
    )

    print(f"[Step 1][{project_name}] 目标周期: {week_info['start_date']} ~ {week_info['end_date']}")
    print(f"[Step 1][{project_name}] 搜索上周 7 天: {week_info['date_list']}")
    print(f"[Step 1][{project_name}] 原子池空间 state_project_guid: {state_project_guid}")
    print(f"[Step 1][{project_name}] 原子池目录 state_parent_guid: {state_parent_guid}")

    matched_notes = []
    seen_note_guids = set()

    for user_guid in project_user_guids:
        if not user_guid:
            continue

        notes = find_weekly_atomic_notes(
            user_guid=user_guid,
            project_guid=state_project_guid,
            folder_guid=state_parent_guid,
            date_list=week_info["date_list"]
        )

        for note in notes:
            note_guid = note["categoryGuid"]
            if note_guid in seen_note_guids:
                continue
            seen_note_guids.add(note_guid)
            matched_notes.append({
                "date": note["date"],
                "user_guid": user_guid,
                "note_guid": note_guid,
                "note_title": note["dataTitle"],
                "node_type": note.get("node_type")
            })

    if not matched_notes:
        print(f"[Step 1][{project_name}] ❌ 未找到上周原子池 JSON 文件")
        return {}, False, [], []

    matched_notes.sort(key=lambda x: (x["date"], x["note_title"]))
    print(f"[Step 1][{project_name}] ✅ 找到 {len(matched_notes)} 份候选原子池 JSON，解析中...")

    all_items = []
    source_urls = OrderedDict()
    atomic_note_entries = []

    for note in matched_notes:
        try:
            if str(note.get("node_type")) == "5":
                atomic_pool = get_json_file_content(note["note_guid"])
            else:
                raw_json = get_note_json_content(user_guid=note["user_guid"], doc_id=note["note_guid"])
                atomic_pool = parse_atomic_pool_from_note_json(raw_json)
        except Exception as e:
            print(f"    [Skip][{project_name}] 原子池读取失败: {note.get('note_title')} error={e}")
            continue

        items = atomic_pool.get("items", []) or []
        meta = atomic_pool.get("meta", {}) or {}
        note_date = meta.get("date") or note.get("date")

        if note_date and note_date not in week_info["date_list"]:
            print(f"    [Skip][{project_name}] JSON 日期不在目标周内: {note.get('note_title')} -> {note_date}")
            continue

        if not note_date:
            item_dates = sorted({x.get("date") for x in items if x.get("date")})
            week_item_dates = [d for d in item_dates if d in week_info["date_list"]]
            if week_item_dates:
                note_date = week_item_dates[0]
            else:
                print(f"    [Skip][{project_name}] 无法识别目标周日期: {note.get('note_title')}")
                continue

        json_note_url = meta.get("json_note_url") or f"{BASE_URL}/workspace/{note['note_guid']}"

        # ✅ 源日报链接优先取原始日报 meta.source_url。
        # 如果没有 source_url，再兼容旧字段 markdown_note_url；最后才兜底到原子池文件链接。
        source_report_url = (
            meta.get("source_url")
            or meta.get("markdown_note_url")
            or json_note_url
        )
        source_urls[note_date] = source_report_url

        for item in items:
            if not item.get("date"):
                item["date"] = note_date
            # 严格只纳入上周 7 天 items
            if item.get("date") in week_info["date_list"]:
                all_items.append(item)

        atomic_note_entries.append({
            "date": note_date,
            "url": source_report_url,
            "json_url": json_note_url,
            "note_guid": note["note_guid"],
            "note_title": meta.get("note_title") or note["note_title"],
            "item_count": len(items)
        })

    actual_dates = sorted({item.get("date") for item in all_items if item.get("date")})
    if not actual_dates:
        actual_dates = sorted(source_urls.keys())

    expected_dates = set(week_info["date_list"])
    actual_dates_set = set(actual_dates)
    missing_dates = sorted(expected_dates - actual_dates_set)
    extra_dates = sorted(actual_dates_set - expected_dates)

    print(f"[Step 1][{project_name}] 实际纳入日期: {actual_dates}")
    if missing_dates:
        print(f"[Step 1][{project_name}] ℹ️ 以下日期未找到原子池，可能为节假日/未填写日报: {missing_dates}")
    if extra_dates:
        print(f"[Step 1][{project_name}] ⚠️ 存在目标周外日期，已过滤: {extra_dates}")

    week_number = None
    if actual_dates:
        first_date = datetime.strptime(actual_dates[0], "%Y-%m-%d")
        week_number = first_date.isocalendar()[1]
    else:
        week_number = week_info["week_number"]

    weekly_atomic_pool = {
        "metadata": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "range_dates": actual_dates or week_info["date_list"],
            "source_urls": dict(source_urls),
            "week_number": week_number,
            "atomic_note_entries": atomic_note_entries,
            "total_items": len(all_items)
        },
        "items": all_items
    }

    raw_json_path = build_intermediate_json_file(
        project_guid=state_project_guid,
        target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
        json_content=weekly_atomic_pool,
        suffix="atomic_raw"
    )
    generated_files.append(raw_json_path)
    print(f"[Step 1][{project_name}] 📦 周级原子池 JSON 已生成: {raw_json_path}")

    return weekly_atomic_pool, True, generated_files, atomic_note_entries


# =============================================================================
# 原子池 -> depth 树结构 -> platform/project/date timeline
# =============================================================================
def normalize_section(section):
    section = (section or "progress").strip().lower()

    if section in ("progress", "main_progress", "done", "completed"):
        return "progress"
    if section in ("issue", "issues", "risk", "risks", "help", "issue_help", "support", "difficulty", "blocked"):
        return "issues_support"
    if section in ("next", "next_focus", "next_plan", "plan", "todo", "future"):
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
            "children": []
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


def atomic_item_to_tree_entry(item):
    member = item.get("member", {}) or {}
    source = item.get("source", {}) or {}
    project = item.get("project", {}) or {}

    return {
        "item_id": item.get("item_id") or str(uuid.uuid4()),
        "date": item.get("date", ""),
        "dept_name": item.get("dept_name", ""),
        "project_name": (project.get("name") or "未分类项目").strip(),
        "project_name_source": project.get("name_source", "inferred"),
        "member": {
            "uid": member.get("uid", ""),
            "id": member.get("id", ""),
            "label": member.get("label", ""),
            "mention_md": member.get("mention_md", "")
        },
        "section": normalize_section(item.get("section")),
        "content_tree": restore_content_tree(item.get("content", []) or []),
        "source": {
            "note_guid": source.get("note_guid", ""),
            "note_title": source.get("note_title", ""),
            "source_url": source.get("source_url", "")
        }
    }


def build_weekly_timeline_state(weekly_atomic_pool):
    items = weekly_atomic_pool.get("items", []) or []
    metadata = weekly_atomic_pool.get("metadata", {}) or {}
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
                    }
                }

            if section not in platform_map[platform][project_name]["sections"]:
                platform_map[platform][project_name]["sections"][section] = OrderedDict()

            if date not in platform_map[platform][project_name]["sections"][section]:
                platform_map[platform][project_name]["sections"][section][date] = []

            platform_map[platform][project_name]["sections"][section][date].append(entry)

    platforms = []
    for platform_name, project_map in platform_map.items():
        projects_for_platform = []
        for project_name, project_data in project_map.items():
            normalized_sections = {}
            for section_name, date_map in project_data["sections"].items():
                normalized_sections[section_name] = [
                    {
                        "date": d,
                        "items": date_map[d]
                    }
                    for d in sorted(date_map.keys())
                ]

            projects_for_platform.append({
                "project_name": project_name,
                "sections": normalized_sections
            })

        platforms.append({
            "platform": platform_name,
            "projects": projects_for_platform
        })

    return {
        "metadata": metadata,
        "platforms": platforms
    }


def compact_timeline_state_for_llm(batch_state, max_text_len_per_tree=10000):
    compact = {
        "platform": batch_state.get("platform"),
        "projects": []
    }

    for project in batch_state.get("projects", []) or []:
        compact_project = {
            "project_name": project.get("project_name", "未分类项目"),
            "sections": {}
        }

        for section_name, date_entries in (project.get("sections", {}) or {}).items():
            section_days = []
            for day in date_entries or []:
                day_items = []
                for item in day.get("items", []) or []:
                    tree_md = "\n".join(tree_to_markdown(item.get("content_tree", [])))
                    if len(tree_md) > max_text_len_per_tree:
                        tree_md = tree_md[:max_text_len_per_tree] + "..."
                    day_items.append({
                        "item_id": item.get("item_id", ""),
                        "member": item.get("member", {}).get("mention_md") or item.get("member", {}).get("label", ""),
                        "source_url": item.get("source", {}).get("source_url", ""),
                        "content_tree_markdown": tree_md
                    })

                if day_items:
                    section_days.append({
                        "date": day.get("date"),
                        "items": day_items
                    })

            compact_project["sections"][section_name] = section_days

        compact["projects"].append(compact_project)

    return compact


def split_platform_projects_into_batches(timeline_state, max_projects_per_batch=DEFAULT_MAX_PROJECTS_PER_BATCH):
    batches = []

    for platform_data in timeline_state.get("platforms", []) or []:
        platform_name = platform_data.get("platform", "未标注平台")
        projects_list = platform_data.get("projects", []) or []

        for i in range(0, len(projects_list), max_projects_per_batch):
            batches.append({
                "platform": platform_name,
                "projects": projects_list[i:i + max_projects_per_batch]
            })

    return batches


# =============================================================================
# Prompt：趋势分析 / 最终整理 / 覆盖校验 / 修复
# =============================================================================
def get_default_trend_prompt():
    return """你是项目周报趋势分析助手。

你会收到某一个 platform 下若干 project 的一周原子数据时间线。数据已经按 project / section / date 组织，并且 content_tree_markdown 中保留了原始日报的层级结构：
- 浅层 bullet 通常是主题或主事项；
- 深层 bullet 通常是该主题下的细节、子任务、验证方向、量化对象、困难、依赖或后续动作。

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
  "main_progress": [
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
  "issues_support": [
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
  "next_plan": [
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
  ]
}

# 字段来源要求
1. platform 必须与输入 platform 完全一致。
2. project_name 必须来自输入 projects[].project_name，不允许自行创造。
3. subtopic 表示 project 下的模块、子项、专题或事项主题；只能来自 content_tree_markdown 中明确出现的模块名、子项名、专题名或事项标题。
4. 如果无法识别明确 subtopic，则 subtopic 必须等于 project_name，不允许编造“项目X”“模块A”“相关工作”“其他事项”等名称。
5. items[].mention 必须优先填写输入 item 中的 member 字段，例如 `[@姓名](mention:uid:id)`。
6. 如果同一 subtopic 下有多位成员推进，必须分别作为多个 items[] 输出，不要合并后丢失 mention。
7. 如果输入 member 为空，mention 可以为空字符串，但不得编造 mention。

# 趋势理解规则
1. 每个 summary 要体现“周”的视角，例如：从A推进到B、完成A并进入B、围绕A持续验证、受B依赖影响待推进。
2. 不要把 content_tree_markdown 中的子项当成独立主项目；它们应作为父事项的细节被综合进 summary。
3. main_progress 要尽量覆盖每个 project 的主要进展；core_progress 只选本批次最重要的 2~6 条。
4. issues_support 只写明确困难、依赖、风险、支持需求，不要自行推断。
5. next_plan 只写明确计划，不要自行创造。
6. 如果某数组无内容，输出空数组 []。

# 量化与枚举要求
1. 如果输入中明确出现多个可枚举对象，例如算法、case、模块、平台、接口、实验、文档、任务项、缺陷类型等，必须尽量保留数量信息。
2. 当对象数量为 1~5 个时，summary 中必须列出具体名称，例如“完成 A、B、C 算法验证”。
3. 当对象数量为 6 个及以上时，summary 中必须写明数量，例如“完成 8 个算法验证”，并尽量列出关键或代表性对象，例如“包括 A、B、C 等”。
4. 严禁在可以明确计数或枚举时使用“多个算法”“若干 case”“相关模块”“部分任务”等模糊表达。
5. 如果输入中没有明确数量，但列出了对象名称，应根据列出的名称进行计数。
6. 如果输入中只写了“多个”“若干”，且没有具体名称，则不得自行推断数量，只能保留原文模糊表达。
7. 如果输入中出现完成数量、测试数量、case 数量、缺陷数量、文档数量、接口数量等数字，必须保留。

输入 JSON：
{{batch_json}}
"""

def get_default_final_weekly_prompt():
    return """# Role
你是一位严谨、客观、专业的项目周报整理助手。

你的任务是：将输入的“周报结构化汇总 Markdown”整理为最终可发布的团队周报正文。

输入内容已经是结构化事实草稿，因此你的职责不是重新分析事实，而是：
- 合并重复表达
- 按主题重组内容
- 压缩冗余描述
- 保留关键事实与 mention
- 输出结构清晰的最终周报正文

# 核心目标
本任务的第一优先级不是语言优美，而是“主题覆盖完整”。

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
用一段 80~150 字的客观文字总结本周最核心的推进情况。

要求：
- 必须来自输入中已有的关键进展归纳，不允许额外创造事实。
- 只做概括，不要求覆盖所有细项，但不得与下文详细内容冲突。
- 不要写空泛评价。
- 不要写“整体进展顺利”“符合预期”“状态良好”等输入中没有明确出现的判断。

### ✅ 本周主要进展
必须按 platform → project → 事项 的层级输出。

标准格式：

- **{Platform}**
    - **{Project}**
        - [@姓名](mention:uid:id) 具体进展事项
        - [@姓名](mention:uid:id) 具体进展事项

如果输入中明确存在模块/子项/专题名，可以增加一层 subtopic：

- **{Platform}**
    - **{Project}**
        - **{Subtopic}**
            - [@姓名](mention:uid:id) 具体进展事项

字段来源要求：
1. {Platform} 只能来自输入中已有的 platform 标题，不允许自行创造。
2. {Project} 只能来自输入中已有的项目名，不允许自行创造。
3. {Subtopic} 只能来自输入中明确出现的模块名、子项名、专题名或事项标题。
4. 如果输入中没有明确 Subtopic，不要生成 Subtopic 层级。
5. 每条具体事项如果输入中有 mention，必须尽量以原始 mention 开头。
6. 不要按日期逐条流水账输出。
7. 每条只保留一个明确事实，避免把多个无关动作塞进同一条。
8. 即使某主题内容较少，只要输入中明确提到，也必须保留。

### ❗ 困难及所需帮助
必须按 platform → project → 事项 的层级输出。

标准格式：

- **{Platform}**
    - **{Project}**
        - [@姓名](mention:uid:id) 具体困难或所需帮助

如果无明确困难或帮助事项，输出：
本周无明确阻塞性问题或外部协助事项。

要求：
1. 仅列出输入中明确存在的风险、困难、阻塞项、依赖项、所需接口、所需资源、所需协助、跨团队支持。
2. 不要推断影响程度。
3. 不要补充风险趋势。
4. 不要编造未明确提出的资源需求。
5. 若输入中有 mention，必须尽量保留原始 mention。

### 🙌 下一步计划
必须按 platform → project → 事项 的层级输出。

标准格式：

- **{Platform}**
    - **{Project}**
        - [@姓名](mention:uid:id) 具体下一步计划

如果无明确下一步计划，输出：
本周无明确下一步计划。

要求：
1. 仅保留输入中明确存在的下一步推进方向、后续开发计划、后续验证计划、后续联调计划、后续优化方向。
2. 不要凭空推测未来规划。
3. 不要将“困难”误写成“下一步计划”。
4. 不要把“Need Help”直接改写成计划。
5. 若输入中有 mention，必须尽量保留原始 mention。

# 量化表达要求
1. 最终周报必须尽量保留输入中的数量、枚举项和具体对象名称。
2. 对于输入中明确列出的算法、case、模块、接口、实验、文档、任务项等：
   - 1~5 个：必须全部列出名称；
   - 6 个及以上：必须写明数量，并尽量列出关键项或代表项。
3. 不得将“完成 A、B、C、D、E 算法”压缩成“完成多个算法”。
4. 不得将“完成 12 个 case”压缩成“完成多个 case”。
5. 可以压缩语言，但不能丢失可量化结果。
6. 如果输入中已有数字，最终输出必须保留该数字。
7. 如果输入中只有模糊量词，没有具体名称或数字，不要自行编造数量。

# 风格要求
1. 客观、中性、专业、简洁。
2. 不要空泛表述，不要主观评价。
3. 不要输出规则本身。
4. 输出必须是 Markdown 正文。
5. 不要输出代码块。
6. 不要输出 JSON。
7. 不要输出日期范围、周数、源日报链接。
8. 不要输出 source_item_ids、evidence_dates、source_url 等中间态字段。

# 输入内容
{{markdown_content}}
"""

def get_default_coverage_check_prompt():
    return """你是周报覆盖性校验助手。

你的任务是：对照“结构化汇总 Markdown 草稿”和“最终周报 Markdown”，检查最终周报是否遗漏了输入中明确出现的 platform / project / subtopic / 事项。

你只负责校验，不负责重新写周报。

# 校验重点
1. 结构化草稿中明确出现的 platform，在最终周报中是否仍然存在。
2. 结构化草稿中明确出现的 project，在最终周报中是否有对应落点。
3. 结构化草稿中明确出现的 subtopic / 模块 / 子项，在最终周报中是否有对应落点；若最终周报没有 subtopic 层级，但事实完整保留，不算遗漏。
4. 结构化草稿中明确出现的进展，是否进入最终周报的“本周主要进展”。
5. 结构化草稿中明确出现的困难、阻塞、依赖、所需支持，是否进入最终周报的“困难及所需帮助”。
6. 结构化草稿中明确出现的下一步计划，是否进入最终周报的“下一步计划”。
7. mention 是否被明显丢失或错误替换。
8. 明确数量或枚举项是否被压缩成“多个”“若干”“相关”等模糊表达。

# 不要误判
以下情况不算遗漏：
1. 最终周报换了等价表达，但 platform / project / 事实仍然可识别。
2. 多条重复内容被合并，但没有丢失关键事实、mention 和数量。
3. source_item_ids、evidence_dates、source_url 被删除，不算遗漏。
4. subtopic 被省略，但相关事实仍完整保留在对应 project 下，不算遗漏。

# 应判为遗漏或信息损失
1. 草稿中存在某个 project，但最终周报完全没有出现。
2. 草稿中存在明确困难或依赖，但最终周报未放入“困难及所需帮助”。
3. 草稿中存在明确下一步计划，但最终周报未放入“下一步计划”。
4. 草稿中存在 mention，最终周报保留了事实但删除了 mention。
5. 草稿中明确列出了 A、B、C、D、E，但最终周报只写成“多个”。
6. 草稿中明确写了数量，例如“完成 8 个 case”，但最终周报删除了数量。
7. 最终周报出现了草稿中没有的项目名、模块名、人员或事实。

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

# 判断规则
如果没有明显遗漏，输出：
{
  "pass": true,
  "missing_items": [],
  "wrong_or_suspicious_items": []
}

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

# 修复方式
你只能进行以下操作：
1. 在对应章节中插入遗漏 platform / project / 事实。
2. 在已有 project 下补充遗漏事实。
3. 补回被遗漏的 mention。
4. 对明显放错章节的内容进行小范围移动。
5. 在不改变事实的前提下做轻微语言衔接。

# 层级要求
请保持以下层级：
- **{Platform}**
    - **{Project}**
        - [@姓名](mention:uid:id) 具体事项

只有当结构化草稿中明确存在 Subtopic 时，才增加一层：
- **{Platform}**
    - **{Project}**
        - **{Subtopic}**
            - [@姓名](mention:uid:id) 具体事项

严禁为了补齐格式而新增“项目X”“模块A”“相关工作”“其他事项”等推测层级。

# 量化信息修复要求
如果校验结果指出数量、枚举项或具体对象名称丢失，修复时必须补回：
- 1~5 个对象：补回全部名称；
- 6 个及以上对象：补回数量，并尽量补回关键名称；
- 已有数字必须保留；
- 不得继续使用“多个”“若干”“相关”等模糊表达替代明确数量。

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
    prompt_file_guid = project.get("weekly_trend_prompt_file_guid")
    prompt_template = load_prompt_text(prompt_file_guid, get_default_trend_prompt())
    compact_state = compact_timeline_state_for_llm(batch_state)
    return prompt_template.replace("{{batch_json}}", json.dumps(compact_state, ensure_ascii=False, indent=2))


def fallback_analyze_batch(batch_state):
    platform = batch_state.get("platform", "未标注平台")
    result = {
        "platform": platform,
        "core_progress": [],
        "main_progress": [],
        "issues_support": [],
        "next_plan": []
    }

    for project in batch_state.get("projects", []) or []:
        project_name = project.get("project_name", "未分类项目")

        for section_name, target_key in [
            ("progress", "main_progress"),
            ("issues_support", "issues_support"),
            ("next_plan", "next_plan"),
        ]:
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
                        "source_item_ids": [item.get("item_id", "")]
                    })

            if grouped_items:
                result[target_key].append({
                    "project_name": project_name,
                    "subtopic": project_name,
                    "items": grouped_items[:10]
                })

    result["core_progress"] = result["main_progress"][:3]
    return result

def analyze_trend_batch(batch_idx, total_batches, batch_state, project):
    project_name = project.get("project_name", "Unknown")
    platform = batch_state.get("platform", "未标注平台")
    print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(project_name)}][{safe_log_text(platform)}] 开始趋势分析，项目数: {len(batch_state.get('projects', []))}")

    prompt_text = build_trend_prompt(batch_state, project)
    messages = [
        {"role": "system", "content": "你是项目周报趋势分析助手，只输出合法 JSON。"},
        {"role": "user", "content": prompt_text}
    ]

    try:
        llm_result = call_llm(
            messages=messages,
            max_tokens=int(get_weekly_config(project, "weekly_trend_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(get_weekly_config(project, "weekly_trend_temperature", 0.1)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES))
        )
        parsed = safe_json_loads(llm_result)
        parsed.setdefault("platform", platform)
        print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(platform)}] ✅ 趋势 JSON 解析完成")
        return batch_idx, parsed
    except Exception as e:
        print(f"    [Trend Batch {batch_idx}/{total_batches}][{safe_log_text(platform)}] ⚠️ 趋势分析失败，使用保底逻辑: {e}")
        return batch_idx, fallback_analyze_batch(batch_state)


def analyze_trends_in_parallel(timeline_state, project, max_projects_per_batch=DEFAULT_MAX_PROJECTS_PER_BATCH, max_parallel=10):
    batches = split_platform_projects_into_batches(timeline_state, max_projects_per_batch=max_projects_per_batch)
    total_batches = len(batches)

    if total_batches == 0:
        return []

    actual_parallel = min(max_parallel, total_batches)
    project_name = project.get("project_name", "Unknown")
    print(f"[Step 3][{project_name}] 开始 platform/project 分块趋势分析，共 {total_batches} 批，并行数: {actual_parallel}")

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
    """
    兼容两种 trend 输出：
    1. 新结构：project_name + subtopic + items[]，可稳定保留 mention
    2. 旧结构：project_name + summary，作为兜底兼容
    """
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

    return {
        "project_name": project_name,
        "subtopic": subtopic,
        "items": normalized_items
    }


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
            tuple(
                (x.get("mention", ""), x.get("summary", ""))
                for x in item.get("items", [])
            )
        )
        if item_key in seen:
            continue
        seen.add(item_key)
        deduped.append(item)
    return deduped


def render_platform_section(platform_map, section_key, empty_text="本周暂无明确内容。"):
    """
    输出层级：
    - **{Platform}**
        - **{Project}**
            - [@Name](mention:uid:id) 完成了 XXX
    如果 trend 阶段明确给出 subtopic，则输出：
    - **{Platform}**
        - **{Project}**
            - **{Subtopic}**
                - [@Name](mention:uid:id) 完成了 XXX
    """
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
                # subtopic 和 project_name 一样时不重复显示一层，避免模型被迫生成假子项。
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

                    if mention:
                        parts.append(f"{leaf_indent}- {mention} {summary}")
                    else:
                        parts.append(f"{leaf_indent}- {summary}")

        parts.append("")

    if not any_content:
        return empty_text

    return "\n".join(parts).strip()

def build_structured_body_markdown(platform_map):
    """
    将趋势 JSON 合并结果拼成结构化事实草稿。
    这是 final writer 的输入，不是最终周报。
    """
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
    parts.append("")

    return "\n".join(parts).strip()


def build_header_markdown(weekly_atomic_pool):
    metadata = weekly_atomic_pool.get("metadata", {}) or {}
    range_dates = metadata.get("range_dates", []) or []
    source_urls = metadata.get("source_urls", {}) or {}
    week_number = metadata.get("week_number", "")

    start_date = range_dates[0] if range_dates else ""
    end_date = range_dates[-1] if range_dates else ""

    header_parts = [
        f"**日期范围：** {start_date} 至 {end_date} | **周数：** 第 {week_number} 周",
        "",
        "**源日报链接：**"
    ]

    for report_date in sorted(source_urls.keys()):
        source_url = source_urls.get(report_date, "")
        header_parts.append(f"- {report_date}: [{source_url}]({source_url})")

    header_parts.append("")
    header_parts.append("---")
    return "\n".join(header_parts).strip()


def generate_final_weekly_body(structured_body_markdown, project):
    prompt_template = load_prompt_text(
        project.get("weekly_final_prompt_file_guid"),
        get_default_final_weekly_prompt()
    )
    prompt_text = prompt_template.replace("{{markdown_content}}", structured_body_markdown)

    try:
        result = call_llm(
            messages=[
                {"role": "system", "content": "你是严谨、客观、专业的项目周报整理助手，请只输出 Markdown 正文。"},
                {"role": "user", "content": prompt_text}
            ],
            max_tokens=int(get_weekly_config(project, "weekly_final_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(get_weekly_config(project, "weekly_final_temperature", 0.2)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES))
        )
        cleaned = strip_markdown_wrapper(result)
        return cleaned or structured_body_markdown
    except Exception as e:
        print(f"    ⚠️ 最终周报整理模型调用失败，使用结构化草稿作为正文: {e}")
        return structured_body_markdown


def validate_weekly_coverage(structured_body_markdown, final_body_markdown, project):
    prompt_template = load_prompt_text(
        project.get("weekly_validation_prompt_file_guid"),
        get_default_coverage_check_prompt()
    )
    prompt_text = (
        prompt_template
        .replace("{{structured_markdown}}", structured_body_markdown)
        .replace("{{final_markdown}}", final_body_markdown)
    )

    try:
        result = call_llm(
            messages=[
                {"role": "system", "content": "你是周报覆盖性校验助手，只输出合法 JSON。"},
                {"role": "user", "content": prompt_text}
            ],
            max_tokens=int(get_weekly_config(project, "weekly_validation_max_tokens", 2048)),
            temperature=float(get_weekly_config(project, "weekly_validation_temperature", 0.0)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES))
        )
        parsed = safe_json_loads(result)
        parsed.setdefault("pass", True)
        parsed.setdefault("missing_items", [])
        parsed.setdefault("wrong_or_suspicious_items", [])
        return parsed
    except Exception as e:
        print(f"    ⚠️ 覆盖性校验失败，跳过校验: {e}")
        return {"pass": True, "missing_items": [], "wrong_or_suspicious_items": []}


def repair_weekly_body(structured_body_markdown, final_body_markdown, validation_result, project):
    prompt_template = load_prompt_text(
        project.get("weekly_repair_prompt_file_guid"),
        get_default_repair_prompt()
    )
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
                {"role": "user", "content": prompt_text}
            ],
            max_tokens=int(get_weekly_config(project, "weekly_repair_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(get_weekly_config(project, "weekly_repair_temperature", 0.1)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES))
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


def build_final_markdown(weekly_atomic_pool, platform_map, project=None):
    structured_body = build_structured_body_markdown(platform_map)

    if project is None:
        final_body = structured_body
        validation_result = {"pass": True, "missing_items": [], "wrong_or_suspicious_items": [], "skipped": True}
    else:
        final_body, validation_result = generate_checked_final_body(structured_body, project)

    header = build_header_markdown(weekly_atomic_pool)
    final_markdown = header + "\n\n" + final_body.strip()

    return final_markdown.strip(), structured_body, validation_result


# =============================================================================
# 笔记创建与写入
# =============================================================================
def insert_markdown_to_note(user_guid, note_guid, markdown_content, max_retries=3):
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
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"写入笔记失败: {response.text}")

    return response.json()


def create_note_api(content, title, project_guid, parent_guid, tags, creator_guid=None):
    creator_guid = creator_guid or USER_GUID
    headers = get_headers_with_ak()
    headers["X-User-GUID"] = creator_guid

    if not project_guid:
        raise ValueError("target_project_guid 不能为空！")

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
        timeout=60
    )

    response_json = response.json()
    if response.status_code != 200 or not response_json.get("data"):
        raise Exception(f"创建笔记 API 返回错误: {response_json}")

    doc_id = response_json.get("data", {}).get("guid")

    if doc_id and content:
        try:
            insert_markdown_to_note(creator_guid, doc_id, content, max_retries=5)
        except Exception as e:
            print(f"    ⚠️ 笔记已创建(doc_id={doc_id})但内容写入失败: {e}")
            print("    → 将在 5s 后单独重试写入...")
            time.sleep(5)
            try:
                insert_markdown_to_note(creator_guid, doc_id, content, max_retries=5)
                print("    ✅ 重试写入成功")
            except Exception as e2:
                print(f"    ❌ 重试写入仍失败: {e2}，笔记已创建但内容为空，doc_id={doc_id}")

    return doc_id


def create_final_weekly_note(content, project, week_info):
    try:
        project_name = project.get("project_name", "")
        target_project_guid = project.get("weekly_target_project_guid")
        target_parent_guid = project.get("weekly_target_parent_guid", "0")
        target_user_guid = project.get("weekly_target_user_guid") or USER_GUID

        if not target_project_guid:
            raise ValueError(f"配置错误: project '{project_name}' 的 weekly_target_project_guid 为空！")

        print(f"[Step 5][{project_name}] 正在创建正式周报笔记...")

        title = build_weekly_note_title(week_info, project_name)
        doc_id = create_note_api(
            content=content,
            title=title,
            project_guid=target_project_guid,
            parent_guid=target_parent_guid,
            tags=["周报", "AI", "原子池"],
            creator_guid=target_user_guid
        )

        if not doc_id:
            return [], []

        note_url = f"{BASE_URL}/workspace/{doc_id}"
        print(f"[Step 5][{project_name}] ✅ 正式周报笔记创建完成: {note_url}")
        return [note_url], [title]

    except Exception as e:
        print(f"[Step 5] ❌ 发生异常: {e}")
        traceback.print_exc()
        return [], []


# =============================================================================
# 飞书卡片与消息发送
# =============================================================================
def get_default_card_prompt():
    return """你是飞书卡片摘要助手。

你的任务是：将输入的完整团队周报压缩成适合飞书消息卡片展示的简洁正文。

# 输出目标
输出一段 200~350 字以内的卡片摘要，重点让读者快速知道：
1. 本周最重要的进展是什么
2. 当前有哪些明确困难或所需帮助
3. 下一步计划是什么

# 输出要求
1. 不要输出 Markdown 标题语法，例如 #、##、###。
2. 可以使用加粗强调关键词。
3. 使用项目符号 `•` 组织内容。
4. 不要输出代码块。
5. 不要输出 JSON。
6. 不要输出日期范围和源日报链接。
7. 不要展开所有项目细节，只保留最高优先级信息。
8. 不要编造输入中不存在的事实。
9. 如果无明确困难，写“暂无明确阻塞性问题”。
10. 如果无明确下一步计划，写“暂无明确下一步计划”。
11. 如输入中存在关键数量或明确枚举成果，优先保留数量，例如“完成 8 个算法验证”，不要写成“完成多个算法”。

# 推荐格式
**本周重点**
• ...
• ...

**困难/帮助**
• ...

**下一步**
• ...

# 输入周报
{{markdown_content}}
"""


def generate_card_content(project, long_markdown, week_info=None):
    project_name = project.get("project_name", "")
    card_prompt_file_guid = project.get(f"{generate_type}_card_prompt_guid")
    prompt_text = load_prompt_text(card_prompt_file_guid, get_default_card_prompt())

    def fallback_format_content(content, max_len=20000):
        content = re.sub(
            r"^###\s+(.+?)\s*$",
            lambda m: f"**{m.group(1).strip()}**",
            content,
            flags=re.MULTILINE
        )
        if len(content) > max_len:
            return content[:max_len] + "\n\n......\n[系统提示：AI 生成失败，此为自动截断的格式化预览]"
        return content

    if week_info is None:
        week_info = get_last_week_info()

    start_date = week_info["start_date"]
    end_date = week_info["end_date"]
    summary_prefix = f"**本周摘要 | {start_date} 至 {end_date}**\n\n"

    meta_header = f"时间范围：{start_date} 至 {end_date} | 第{week_info['week_number']}周"
    card_input_markdown = f"{meta_header}\n\n{long_markdown}"
    user_content = prompt_text.replace("{{markdown_content}}", card_input_markdown)

    try:
        llm_result = call_llm(
            messages=[
                {"role": "system", "content": "你是内容整理助手，请输出适合飞书卡片展示的精炼正文，不要输出代码块。"},
                {"role": "user", "content": user_content}
            ],
            max_tokens=int(get_weekly_config(project, "weekly_card_max_tokens", 1024)),
            temperature=float(get_weekly_config(project, "weekly_card_temperature", 0.3)),
            stream=True,
            max_retries=int(get_weekly_config(project, "llm_max_retries", LLM_MAX_RETRIES))
        )
        return summary_prefix + strip_markdown_wrapper(llm_result)

    except Exception as e:
        print(f"⚠️ [Card][{project_name}] AI 生成失败，使用 fallback: {e}")
        return summary_prefix + fallback_format_content(card_input_markdown, max_len=20000)


def build_feishu_card(title, card_content, note_url, source_note_entries=None):
    elements = [
        {
            "tag": "markdown",
            "content": card_content,
            "margin": "0px",
            "text_size": "normal"
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
            "text_size": "normal"
        })

        button_items = []
        for item in display_entries:
            date_text = item.get("date", "")
            short_date = date_text[5:] if len(date_text) >= 10 else date_text
            button_items.append({
                "text": f"{short_date} 日报",
                "url": item.get("url", ""),
                "type": "default"
            })

        if has_more:
            button_items.append({
                "text": "更多日报",
                "url": note_url,
                "type": "default"
            })

        for i in range(0, len(button_items), 2):
            pair = button_items[i:i + 2]
            columns = []
            for item in pair:
                columns.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "type": item.get("type", "default"),
                            "width": "fill",
                            "margin": "4px 0px 4px 0px",
                            "text": {
                                "tag": "plain_text",
                                "content": item.get("text", "查看")
                            },
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": item.get("url", "")
                                }
                            ]
                        }
                    ]
                })

            if len(columns) == 1:
                columns.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": []
                })

            elements.append({
                "tag": "column_set",
                "flex_mode": "stretch",
                "horizontal_spacing": "8px",
                "margin": "0px",
                "columns": columns
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
                            "content": "查看完整周报"
                        },
                        "behaviors": [
                            {
                                "type": "open_url",
                                "default_url": note_url
                            }
                        ]
                    }
                ]
            }
        ]
    })

    return {
        "schema": "2.0",
        "header": {
            "padding": "12px 8px 12px 8px",
            "template": "orange",
            "title": {
                "content": title,
                "tag": "plain_text"
            }
        },
        "body": {
            "vertical_spacing": "12px",
            "elements": elements
        }
    }


def send_webhook(webhook_url, card):
    response = requests.post(
        url=webhook_url,
        headers={"Content-Type": "application/json"},
        json={"msg_type": "interactive", "card": card},
        timeout=30
    )
    return response.json()


def send_message_api(receiver_guids, title, content, sender_guid="", interactive_content=None):
    payload = {
        "template_id": MESSAGE_TEMPLATE_ID,
        "receiver_guid": receiver_guids,
        "content": content,
        "org_guid": ORG_GUID,
        "title": title,
        "platform_type": PLATFORM_TYPE
    }

    if interactive_content is not None:
        payload["interactive_content"] = json.dumps(interactive_content, ensure_ascii=False)

    return requests.post(
        url=BASE_URL + MESSAGE_SEND_ROUTE,
        headers=get_headers_with_ak(user_guid=sender_guid),
        json=payload,
        timeout=30
    )


def step6_send_messages(note_url_list, note_title_list, project, content_list, week_info=None, source_note_entries=None):
    """
    weekly_webhook_url：支持字符串或列表，向多个群发送。
    weekly_sender_guid：支持字符串或列表，向多个人发送。
    """
    try:
        project_name = project.get("project_name", "")

        webhook_urls = normalize_to_list(project.get(f"{generate_type}_webhook_url", []))
        receiver_guids = normalize_to_list(project.get(f"{generate_type}_sender_guid", []))
        sender_guid = project.get(f"{generate_type}_target_user_guid", "") or project.get("weekly_target_user_guid", "") or USER_GUID

        if not note_url_list:
            print(f"[Step 6][{project_name}] ⚠️ 没有 URL 可发送")
            return

        for note_title, note_url, full_content in zip(note_title_list, note_url_list, content_list):
            card_summary = generate_card_content(project, full_content, week_info=week_info)
            card = build_feishu_card(
                note_title,
                card_summary,
                note_url,
                source_note_entries=source_note_entries
            )

            has_sent_any = False

            if webhook_urls:
                for idx, url in enumerate(webhook_urls, 1):
                    try:
                        print(f"[Step 6][{project_name}] 📢 正在发送群消息 (Webhook {idx}/{len(webhook_urls)})...")
                        webhook_result = send_webhook(url, card)

                        if webhook_result.get("code") == 0 or webhook_result.get("StatusCode") == 0:
                            print(f"  -> ✅ 群消息发送成功: {url[:30]}...")
                            has_sent_any = True
                        else:
                            print(f"  -> ❌ 群消息发送失败 ({url[:30]}...): {webhook_result}")
                    except Exception as e:
                        print(f"  -> ❌ 群消息发送异常 ({url[:30]}...): {e}")
            else:
                print(f"[Step 6][{project_name}] 📢 未配置 Webhook 地址，跳过群消息发送")

            if receiver_guids:
                try:
                    print(f"[Step 6][{project_name}] 📩 正在发送个人消息给 {len(receiver_guids)} 人...")
                    text_content = build_message_text(note_title, note_url)
                    response = send_message_api(
                        receiver_guids=receiver_guids,
                        title=note_title,
                        content=text_content,
                        sender_guid=sender_guid,
                        interactive_content=card
                    )
                    if response.status_code == 200 and response.json().get("data"):
                        print("  -> ✅ 个人消息发送成功")
                        has_sent_any = True
                    else:
                        print(f"  -> ❌ 个人消息发送失败: {response.text}")
                except Exception as e:
                    print(f"  -> ❌ 个人消息发送异常: {e}")
            else:
                print(f"[Step 6][{project_name}] 📩 未配置个人接收人，跳过个人消息发送")

            if not has_sent_any and not webhook_urls and not receiver_guids:
                print(f"[Step 6][{project_name}] ⚠️ 未配置 Webhook 且未配置接收人，跳过发送步骤")

        print(f"[Step 6][{project_name}] ✅ 消息分发流程结束")

    except Exception as e:
        print(f"[Step 6] ❌ 发生异常: {e}")
        traceback.print_exc()


# =============================================================================
# 主执行流程
# =============================================================================
print("=" * 60)
print(f"开始执行周报工作流（原子池版）| 项目数: {len(projects)}")
print("=" * 60)

for project in projects:
    project_name = project.get("project_name", "Unknown")
    enable_ai = project.get("enable_weekly_summary", True)

    if not enable_ai:
        print(f"\n⏭ 跳过项目: {project_name} (enable_weekly_summary=False)")
        continue

    print(f"\n▶ 处理项目: {project_name}")
    temp_files = []
    week_info = get_last_week_info()

    try:
        # Step 1: 查找并合并上周 7 天原子池 JSON
        weekly_atomic_pool, found, step1_temp_files, atomic_note_entries = load_weekly_atomic_pool(project)
        temp_files.extend(step1_temp_files)
        if not found:
            print(f"  ⚠️ 跳过 {project_name}")
            cleanup_temp_files(temp_files, project_name=project_name)
            continue

        if not weekly_atomic_pool.get("items"):
            raise Exception("周级原子池中没有任何 items")

        # Step 2: 原子池 -> timeline state
        timeline_state = build_weekly_timeline_state(weekly_atomic_pool)

        timeline_json_path = build_intermediate_json_file(
            project_guid=(project.get("state_project_guid") or project.get("project_guid") or "unknown"),
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            json_content=timeline_state,
            suffix="timeline"
        )
        temp_files.append(timeline_json_path)
        print(f"[Step 2][{project_name}] 📦 timeline_state 已生成: {timeline_json_path}")

        # Step 3: 分 batch 趋势分析
        max_projects_per_batch = int(
            get_weekly_config(project, "weekly_projects_per_batch", DEFAULT_MAX_PROJECTS_PER_BATCH)
        )
        max_parallel_batches = min(
            int(get_weekly_config(project, "batch_number", DEFAULT_BATCH_NUMBER)),
            50
        )

        batch_results = analyze_trends_in_parallel(
            timeline_state=timeline_state,
            project=project,
            max_projects_per_batch=max_projects_per_batch,
            max_parallel=max_parallel_batches
        )

        trend_json_path = build_intermediate_json_file(
            project_guid=(project.get("state_project_guid") or project.get("project_guid") or "unknown"),
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            json_content={"batches": batch_results},
            suffix="trend_batches"
        )
        temp_files.append(trend_json_path)
        print(f"[Step 3][{project_name}] 📦 趋势分析 batch JSON 已生成: {trend_json_path}")

        # Step 4: 合并趋势结果 + 生成最终周报
        platform_map = merge_trend_results(batch_results)
        final_weekly_markdown, structured_body_markdown, validation_result = build_final_markdown(
            weekly_atomic_pool=weekly_atomic_pool,
            platform_map=platform_map,
            project=project
        )

        structured_md_path = build_intermediate_markdown_file(
            project_guid=(project.get("state_project_guid") or project.get("project_guid") or "unknown"),
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}_structured",
            markdown_content=structured_body_markdown
        )
        temp_files.append(structured_md_path)

        validation_json_path = build_intermediate_json_file(
            project_guid=(project.get("state_project_guid") or project.get("project_guid") or "unknown"),
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            json_content=validation_result,
            suffix="validation"
        )
        temp_files.append(validation_json_path)

        final_md_path = build_intermediate_markdown_file(
            project_guid=(project.get("state_project_guid") or project.get("project_guid") or "unknown"),
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}_final",
            markdown_content=final_weekly_markdown
        )
        temp_files.append(final_md_path)

        if not final_weekly_markdown:
            raise Exception("最终周报内容为空")

        # Step 5: 创建正式周报笔记
        note_urls, note_titles = create_final_weekly_note(
            final_weekly_markdown,
            project,
            week_info
        )

        # 源日报入口最多展示 5 个
        source_note_entries = [
            {"date": item.get("date", ""), "url": item.get("url", "")}
            for item in sorted(atomic_note_entries, key=lambda x: x.get("date", ""))
        ][:5]

        # Step 6: 发送群消息 / 个人消息
        step6_send_messages(
            note_urls,
            note_titles,
            project,
            [final_weekly_markdown],
            week_info=week_info,
            source_note_entries=source_note_entries
        )

        cleanup_temp_files(temp_files, project_name=project_name)
        print(f"✅ {project_name} 周报流程结束")

    except Exception as e:
        cleanup_temp_files(temp_files, project_name=project_name)
        print(f"❌ {project_name} 周报流程中断: {e}")
        traceback.print_exc()

print("\n" + "=" * 60)
print("全部周报任务执行完毕")
print("=" * 60)

# path = Path("/mnt/data/weekly_atomic_report_full.py")
# path.write_text(code, encoding="utf-8")

# # Compile check
# import py_compile
# py_compile.compile(str(path), doraise=True)

# print(f"已生成并通过语法检查: {path}")
