import builtins
import sys
import os

if not getattr(builtins.print, '_patched_flush', False):
    _original_print = builtins.print

    def print(*args, **kwargs):
        kwargs.setdefault('flush', True)
        _original_print(*args, **kwargs)

    print._patched_flush = True
    builtins.print = print

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from zenv import get_zdkit_env
from zdbase import ZFile  # 保留平台兼容；本脚本主体不直接依赖
from openai import OpenAI
import requests
import json
import time
import re
import uuid
import tempfile
import traceback
from collections import OrderedDict, defaultdict

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

BATCH_NUMBER = min(int(config.get("batch_number", 20)), 50)
generate_type = "weekly"

# =============================================================================
# OpenAI SDK 直连模型配置
# =============================================================================
# 建议在 config 中配置：
# {
#   "llm_base_url": "http://xxx/cloud/v1",
#   "llm_api_key": "xxx",
#   "llm_model": "doubao-seed-2.0-pro",
#   "llm_temperature": 0.3,
#   "llm_max_tokens": 4096
# }
LLM_BASE_URL = config.get("llm_base_url", "")
LLM_API_KEY = config.get("llm_api_key", "")
LLM_MODEL = config.get("llm_model", "doubao-seed-2.0-pro")
LLM_TEMPERATURE = float(config.get("llm_temperature", 0.3))
LLM_MAX_TOKENS = int(config.get("llm_max_tokens", 4096))
# 是否在日志中打印模型流式输出。生产环境建议 false，调试时可改为 true。
LLM_PRINT_STREAM = bool(config.get("llm_print_stream", False))

if not LLM_BASE_URL or not LLM_API_KEY:
    raise ValueError("请在 config 中配置 llm_base_url 和 llm_api_key")

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

# =============================================================================
# 通用工具函数
# =============================================================================
def get_headers_with_ak(user_guid="", doc_id=""):
    response = requests.post(
        url=BASE_URL + ACCESS_TOKEN_ROUTE,
        json={"ak": AK, "sk": SK}
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
        params={"docId": doc_id}
    )
    return response.json()


def get_json_file_content(category_guid):
    """
    读取 treeList 中 type == 5 的 .json 文件。

    注意：type == 5 通常是文件节点，不一定能通过 getDocJson 读取，
    因此这里走 getSignedUrl，再下载 JSON 文件内容。
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

    # 优先匹配对象，其次匹配数组，避免模型输出解释文字时失败
    object_match = re.search(r'(\{.*\})', clean_text, flags=re.DOTALL)
    if object_match:
        return json.loads(object_match.group(1))

    array_match = re.search(r'(\[.*\])', clean_text, flags=re.DOTALL)
    if array_match:
        return json.loads(array_match.group(1))

    raise ValueError("无法解析 JSON")


def _convert_special_nodes(content):
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
        lambda m: f'<div data-node-type=\'highlightBlock\' data-content-markdown>\n{m.group(1).rstrip()}\n</div>',
        content,
        flags=re.DOTALL
    )

    return content


def normalize_receiver_guids(receiver_guids_raw):
    if isinstance(receiver_guids_raw, str):
        return [receiver_guids_raw] if receiver_guids_raw else []
    return receiver_guids_raw or []


def build_message_text(note_title, note_url):
    return f"【{note_title}】已生成，请点击查看。\n<a href='{note_url}'>点击查看详情</a>"


def load_prompt_text(prompt_file_guid, default_prompt):
    if not prompt_file_guid:
        return default_prompt

    try:
        signed_url_response = requests.get(
            BASE_URL + SIGNED_URL_ROUTE,
            headers=get_headers_with_ak(),
            params={"categoryGuid": prompt_file_guid}
        )
        signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")
        if not signed_url:
            return default_prompt

        return requests.get(signed_url, timeout=10).text
    except Exception:
        return default_prompt


def get_last_week_info():
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
    max_retries=5,
    print_stream=None,
):
    """
    OpenAI SDK 直连模型调用。

    - stream=True 时：使用流式返回；可选打印每个 delta；同时拼接完整文本并 return。
    - stream=False 时：一次性返回完整文本。

    生产环境通常建议：
        config["llm_print_stream"] = false
    调试时可以打开：
        config["llm_print_stream"] = true
    """
    max_tokens = max_tokens or LLM_MAX_TOKENS
    temperature = LLM_TEMPERATURE if temperature is None else temperature
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
    """兼容 treeList 返回中不同字段名的标题/文件名。"""
    for key in ("dataTitle", "title", "name", "fileName", "filename"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _get_tree_node_guid(node):
    """兼容 treeList 返回中不同字段名的文件 guid。"""
    for key in ("categoryGuid", "guid", "fileGuid", "dataGuid", "id"):
        value = node.get(key)
        if value:
            return value
    return ""


def _is_json_atomic_file_node(node):
    """
    原子池优先按文件节点识别：
    - type == 5 表示文件；
    - 标题/文件名必须是 .json 后缀；
    这样不再依赖日报标题格式。
    """
    node_type = node.get("type")
    try:
        is_file = int(node_type) == 5
    except Exception:
        is_file = str(node_type) == "5"

    title = _get_tree_node_title(node)
    return is_file and title.lower().endswith(".json")


def _infer_date_from_title(title, date_list):
    """从 json 文件名中匹配周内日期，匹配不到则返回空字符串。"""
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
    从目标文件夹中查找原子池 JSON 文件。

    这里使用 treeList 接口列出 parentGuid 下的文件，然后筛选：
    1. type == 5
    2. 文件名/标题以 .json 结尾
    3. 文件名中能匹配到目标周日期时，先用文件名日期；匹配不到也保留，后续用 JSON meta.date 再判断

    保留匹配不到标题日期的 json，是为了兼容文件名不含日期、但 JSON 内部 meta.date 正确的情况。
    """
    response = requests.post(
        url=BASE_URL + DOC_TREE_ROUTE,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={"projectGuid": project_guid, "parentGuid": folder_guid}
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
            "node_type": note.get("type")
        })

    return matched_notes


def extract_text_from_note_json_node(node, parts):
    """尽量从平台文档 JSON 中抽取纯文本。兼容 text/codeBlock/mention/mentionUrl。"""
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

        # codeBlock/paragraph/heading/listItem 等可能有 content
        if node_type in ("paragraph", "heading", "fheading", "bulletListItem", "numberedListItem", "codeBlock"):
            before_len = len(parts)
            for child in node.get("content", []) or []:
                extract_text_from_note_json_node(child, parts)
            if len(parts) > before_len:
                parts.append("\n")
            return

        # 有些 codeBlock 可能直接存在 text/code 字段
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
    # 如果接口直接返回的就是原子池 JSON，则直接使用
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

    # 原子池文件所在位置：按你的新配置，优先使用 state_project_guid + state_parent_guid。
    # 其中：
    # - state_project_guid：原子池所在空间/项目 GUID，用于 treeList 的 projectGuid
    # - state_parent_guid：原子池 JSON 文件所在文件夹 GUID，用于 treeList 的 parentGuid
    # 保留旧字段作为兼容兜底，避免历史配置失效。
    state_project_guid = (
        project.get("state_project_guid")
        or project.get("weekly_atomic_pool_project_guid")
        or project.get("atomic_pool_project_guid")
        or project.get("project_guid")
    )

    state_parent_guid = (
        project.get("state_parent_guid")
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
        print(f"[Step 1][{project_name}] ❌ 未找到上周原子池笔记")
        return {}, False, [], []

    matched_notes.sort(key=lambda x: (x["date"], x["note_title"]))
    print(f"[Step 1][{project_name}] ✅ 找到 {len(matched_notes)} 份原子池笔记，解析中...")

    all_items = []
    source_urls = OrderedDict()
    atomic_note_entries = []

    for note in matched_notes:
        try:
            node_type = note.get("node_type")
            if str(node_type) == "5":
                # type == 5 是 JSON 文件，优先走 signedUrl 下载文件内容
                atomic_pool = get_json_file_content(note["note_guid"])
            else:
                # 兼容历史：如果原子池是笔记正文，则继续走 getDocJson
                raw_json = get_note_json_content(user_guid=note["user_guid"], doc_id=note["note_guid"])
                atomic_pool = parse_atomic_pool_from_note_json(raw_json)
        except Exception as e:
            print(f"    [Skip][{project_name}] 原子池读取失败: {note.get('note_title')} error={e}")
            continue

        items = atomic_pool.get("items", []) or []
        meta = atomic_pool.get("meta", {}) or {}
        note_date = meta.get("date") or note.get("date")

        # 如果文件名没有日期，则依赖 JSON 内 meta.date / item.date 判断；如果不属于目标周，跳过。
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
        markdown_note_url = meta.get("markdown_note_url") or json_note_url
        source_urls[note_date] = markdown_note_url

        for item in items:
            if not item.get("date"):
                item["date"] = note_date
            all_items.append(item)

        atomic_note_entries.append({
            "date": note_date,
            "url": markdown_note_url,
            "json_url": json_note_url,
            "note_guid": note["note_guid"],
            "note_title": note["note_title"],
            "item_count": len(items)
        })

    actual_dates = sorted({item.get("date") for item in all_items if item.get("date")})
    if not actual_dates:
        actual_dates = sorted(source_urls.keys())

    week_number = None
    if actual_dates:
        first_date = datetime.strptime(actual_dates[0], "%Y-%m-%d")
        week_number = first_date.isocalendar()[1]

    weekly_atomic_pool = {
        "metadata": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "range_dates": actual_dates,
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
# 原子池 -> 保留 depth 的树结构 -> platform/project/date timeline
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
    """
    使用 depth 还原父子关系：
    - depth 更大的 block 归属于最近一个 depth 更小的上级 block
    - 若 depth 缺失，则按 0 处理
    """
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
        projects = []
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

            projects.append({
                "project_name": project_name,
                "sections": normalized_sections
            })

        platforms.append({
            "platform": platform_name,
            "projects": projects
        })

    return {
        "metadata": metadata,
        "platforms": platforms
    }


def compact_timeline_state_for_llm(batch_state, max_text_len_per_tree=2000):
    """压缩 prompt 输入，但保留 depth 恢复后的层级结构。"""
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
# LLM：趋势分析 JSON
# =============================================================================
def get_default_trend_prompt():
    return """你是项目周报趋势分析助手。

你会收到某一个 platform 下若干 project 的一周原子数据时间线。数据已经按 project / section / date 组织，并且 content_tree_markdown 中保留了原始日报的层级结构：
- 浅层 bullet 通常是主题或主事项；
- 深层 bullet 通常是该主题下的细节、子任务、验证方向或依赖说明。

你的任务不是简单逐条改写，而是理解五天内同一事项的推进趋势，并输出结构化 JSON，供后续拼接周报使用。

# 必须输出合法 JSON，不要输出代码块，不要输出解释文字

# 输出结构
{
  "platform": "",
  "core_progress": [
    {
      "project_name": "",
      "summary": "一句话总结本周最重要的阶段性进展，必须体现趋势或阶段变化",
      "evidence_dates": ["YYYY-MM-DD"],
      "source_item_ids": [""]
    }
  ],
  "main_progress": [
    {
      "project_name": "",
      "summary": "项目本周主要进展，尽量覆盖输入中明确存在的重要进展，不要遗漏项目",
      "evidence_dates": ["YYYY-MM-DD"],
      "source_item_ids": [""]
    }
  ],
  "issues_support": [
    {
      "project_name": "",
      "summary": "困难、风险、阻塞、依赖或所需支持；仅基于输入明确内容",
      "evidence_dates": ["YYYY-MM-DD"],
      "source_item_ids": [""]
    }
  ],
  "next_plan": [
    {
      "project_name": "",
      "summary": "下一步计划或下周重点；仅基于输入明确内容",
      "evidence_dates": ["YYYY-MM-DD"],
      "source_item_ids": [""]
    }
  ]
}

# 规则
1. 必须保留 platform 字段，值与输入一致。
2. 每个 summary 要体现“周”的视角，例如：从A推进到B、完成A并进入B、围绕A持续验证、受B依赖影响待推进。
3. 不要把 content_tree_markdown 中的子项当成独立主项目；它们应作为父事项的细节被综合进 summary。
4. main_progress 要尽量覆盖每个 project 的主要进展；core_progress 只选本批次最重要的 2~6 条。
5. issues_support 只写明确困难/依赖/风险/支持需求，不要自行推断。
6. next_plan 只写明确计划，不要自行创造。
7. 如果某数组无内容，输出空数组 []。
8. summary 语言精炼，适合写入管理周报。

输入 JSON：
{{batch_json}}
"""


def build_trend_prompt(batch_state, project):
    prompt_file_guid = project.get("weekly_trend_prompt_file_guid")
    prompt_template = load_prompt_text(prompt_file_guid, get_default_trend_prompt())
    compact_state = compact_timeline_state_for_llm(batch_state)
    return prompt_template.replace("{{batch_json}}", json.dumps(compact_state, ensure_ascii=False, indent=2))


def fallback_analyze_batch(batch_state):
    """LLM JSON 失败时的保底：不做趋势抽象，只按 section 输出，避免整条链路中断。"""
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
            summaries = []
            dates = []
            ids = []
            for day in project.get("sections", {}).get(section_name, []) or []:
                dates.append(day.get("date"))
                for item in day.get("items", []) or []:
                    ids.append(item.get("item_id", ""))
                    text = "；".join([n.get("text", "") for n in item.get("content_tree", [])])
                    if text:
                        summaries.append(text)

            if summaries:
                summary = "；".join(summaries[:5])
                if len(summary) > 500:
                    summary = summary[:500] + "..."
                result[target_key].append({
                    "project_name": project_name,
                    "summary": summary,
                    "evidence_dates": sorted(set([d for d in dates if d])),
                    "source_item_ids": [x for x in ids if x]
                })

    result["core_progress"] = result["main_progress"][:3]
    return result


def analyze_trend_batch(batch_idx, total_batches, batch_state, project):
    project_name = project.get("project_name", "Unknown")
    platform = batch_state.get("platform", "未标注平台")
    print(f"    [Trend Batch {batch_idx}/{total_batches}][{project_name}][{platform}] 开始趋势分析，项目数: {len(batch_state.get('projects', []))}")

    prompt_text = build_trend_prompt(batch_state, project)
    messages = [
        {"role": "system", "content": "你是项目周报趋势分析助手，只输出合法 JSON。"},
        {"role": "user", "content": prompt_text}
    ]

    try:
        llm_result = call_llm(
            messages=messages,
            max_tokens=int(project.get("weekly_trend_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(project.get("weekly_trend_temperature", 0.2)),
            stream=True,
            max_retries=int(project.get("llm_max_retries", 5))
        )
        parsed = safe_json_loads(llm_result)
        parsed.setdefault("platform", platform)
        print(f"    [Trend Batch {batch_idx}/{total_batches}][{platform}] ✅ 趋势 JSON 解析完成")
        return batch_idx, parsed
    except Exception as e:
        print(f"    [Trend Batch {batch_idx}/{total_batches}][{platform}] ⚠️ 趋势分析失败，使用保底逻辑: {e}")
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
# 趋势 JSON 合并与周报 Markdown 生成
# =============================================================================
def normalize_analysis_item(item):
    return {
        "project_name": (item.get("project_name") or "未分类项目").strip(),
        "summary": (item.get("summary") or "").strip(),
        "evidence_dates": item.get("evidence_dates", []) or [],
        "source_item_ids": item.get("source_item_ids", []) or [],
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
                if normalized["summary"]:
                    platform_map[platform][key].append(normalized)

    return platform_map


def dedupe_items(items):
    seen = set()
    deduped = []
    for item in items or []:
        key = (item.get("project_name"), item.get("summary"))
        if key in seen:
            continue
        seen.add(key)
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
        parts.append(f"#### {platform}")

        grouped_by_project = OrderedDict()
        for item in items:
            project_name = item.get("project_name", "未分类项目")
            grouped_by_project.setdefault(project_name, [])
            grouped_by_project[project_name].append(item)

        for project_name, project_items in grouped_by_project.items():
            parts.append(f"- **{project_name}**")
            for item in project_items:
                dates = item.get("evidence_dates") or []
                date_suffix = f"（{'、'.join(dates)}）" if dates else ""
                parts.append(f"  * {item.get('summary', '')}{date_suffix}")
        parts.append("")

    if not any_content:
        return empty_text

    return "\n".join(parts).strip()


def get_default_final_weekly_prompt():
    return """# Role
你是一位严谨、客观、专业的项目周报整理助手。

你的任务是：将输入的“周报结构化汇总 Markdown”整理为最终可发布的团队周报正文。

输入内容已经是结构化事实草稿，因此你的职责不是重新分析事实，而是：
- 合并重复表达
- 按 platform / 主题 / 项目重组内容
- 压缩冗余描述
- 保留关键事实与 mention
- 输出结构清晰的最终周报正文

---

# 核心目标
本任务的第一优先级不是语言优美，而是**主题覆盖完整**。
输出结果必须尽可能保证：**输入中每一个明确出现的 platform / 项目 / 主题 / 事项，都能在最终周报中找到对应落点**。

换句话说：
- 可以压缩语言
- 可以合并重复描述
- 但不能漏掉某个已经明确出现的 platform / 项目 / 主题
- 不能为了简洁，把多个独立项目合并成一个抽象大类，导致原项目消失

---

# 最高优先级原则
1. 只允许基于输入内容进行整理、压缩、重组，不允许新增输入中不存在的事实。
2. 严禁编造人名、账号、mention、项目状态、风险等级、延期时间、里程碑、测试结果、资源申请等信息。
3. 严禁输出“日期范围、周数、源日报链接”等头部元信息，这些由系统自动补充。
4. 严禁输出任何模板说明、格式约束、处理流程、注意事项、规则说明。
5. 若输入中没有明确提到的信息，宁可省略，不可猜测。
6. 若输入中已有 `[@姓名](mention:uid:id)`，请尽量原样保留；若输入中没有明确 mention，不要补充虚构人名。
7. 不得遗漏输入中已明确出现的主题/项目/事项。
8. 不得将多个原本独立的主题粗暴合并成抽象上位类别，导致原主题名消失。
9. 若输入中存在 platform 信息，输出时必须保留 platform 维度；建议使用“项目名 / platform”作为主题名。

---

# 输出结构（严格遵守）

### 🎉 本周关键进展
- 用一段 80~150 字的客观文字总结本周最核心的推进情况。
- 这一段必须来自输入中已有的“核心进展/关键进展”归纳，不允许额外创造事实。
- 只做概括，不要求覆盖所有细项，但不得与下文详细内容冲突。
- 不要写空泛评价，不要写“整体进展顺利/符合预期/状态良好”等未在输入中明确出现的判断。

### ✅ 本周核心进展
- 必须按“主题/项目 / platform”分组输出，而不是按人分组。
- 这一章节用于呈现本周最重要的阶段性推进。
- 即使某个核心主题内容很少，只要输入中明确出现，也必须保留。
- 输出形式尽量为：

- **主题A / platform**
    - [@姓名](mention:uid:id) ...
    - [@姓名](mention:uid:id) ...
- **主题B / platform**
    - [@姓名](mention:uid:id) ...

### 📌 本周主要进展
- 必须按“主题/项目 / platform”分组输出。
- 这是完整覆盖章节，必须尽可能覆盖输入中所有明确存在的进展主题。
- 不要按日期逐条流水账输出。
- 每条只保留一个明确事实，避免把多个无关动作塞进同一条。
- 若某主题只有 1 条进展，也必须单独保留，不得并入其他主题。

### ❗ 困难和所需支持
- 必须按“主题/项目 / platform”分组输出（若有）。
- 仅列出输入中明确存在的风险、困难、阻塞、依赖和支持需求。
- 不要推断影响程度，不要补充风险趋势。
- 若无明确困难或支持需求，输出：
  本周无明确阻塞性问题或外部支持需求。

### 🙌 下一步计划
- 必须按“主题/项目 / platform”分组输出（若有）。
- 仅列出输入中明确提出的下一步计划、下周重点、后续动作。
- 若无明确下一步计划，输出：
  暂无明确下一步计划，按既定路线推进。

---

# 组织原则
1. 优先按主题聚合，而不是按人逐个汇总。
2. 在同一主题下，应体现该主题本周的连续推进情况，但不得编造不存在的阶段变化。
3. 如果多个成员共同推进同一主题，请在同一主题下集中展示。
4. 若某主题同时存在 progress / risk / help / next_plan，应分别放入对应章节，不要混写。
5. 如果输入中某主题信息很少，也不要强行扩写，但必须保留。
6. 输出时优先保证“完整覆盖”，再做“语言压缩”。
7. 主题名尽量沿用输入中的原始表述，不要过度抽象改写。
8. 若输入中存在项目名、模块名、专题名，应优先保留这些名称。

---

# 风格要求
1. 客观、中性、专业、简洁。
2. 不要空泛表述，不要主观评价。
3. 不要输出规则本身。
4. 输出必须是 Markdown 正文。
5. 不要输出 JSON。

---

# 最终自检要求
在生成结果前，请自行检查：
1. 输入中出现的每个明确 platform / 主题 / 项目，是否都在输出中有落点。
2. 输入中明确存在的困难或支持需求，是否都进入“困难和所需支持”。
3. 输入中明确存在的下一步计划，是否都进入“下一步计划”。
4. 是否有任何主题因内容较少而被遗漏。
5. 是否存在为了压缩而丢失 mention 或关键事实的情况。
6. 是否存在将多个独立主题合并后导致原主题消失的情况。
7. 若发现遗漏主题，优先补回，不要为了篇幅继续压缩。

---

# 输入内容
{{markdown_content}}
"""


def get_default_coverage_check_prompt():
    return """你是周报覆盖性校验助手。

你的任务是：对照“结构化汇总 Markdown 草稿”和“最终周报 Markdown”，检查最终周报是否遗漏了输入中明确出现的 platform / 项目 / 主题 / 事项。

你只负责校验，不负责重新写周报。

# 校验重点
1. 结构化草稿中明确出现的项目/主题，在最终周报中是否有对应落点。
2. 结构化草稿中明确出现的困难、阻塞、依赖、所需支持，是否进入最终周报的“困难和所需支持”。
3. 结构化草稿中明确出现的下一步计划，是否进入最终周报的“下一步计划”。
4. mention 是否被明显丢失或错误替换。
5. platform 维度是否被保留。

# 输出要求
必须只输出合法 JSON，不要输出代码块，不要输出解释文字。

# 输出结构
{
  "pass": true,
  "missing_items": [
    {
      "section": "本周主要进展 / 困难和所需支持 / 下一步计划 / 其他",
      "platform": "",
      "project_name": "",
      "missing_fact": "结构化草稿中存在但最终周报没有覆盖的事实",
      "suggested_insert_position": "建议插入到哪个章节或主题下"
    }
  ],
  "wrong_or_suspicious_items": [
    {
      "section": "",
      "issue": "最终周报中可能新增、歪曲或不准确的内容"
    }
  ]
}

# 判断规则
- 如果没有明显遗漏，pass=true，missing_items=[]。
- 只有当结构化草稿中明确存在、但最终周报没有对应落点时，才记为遗漏。
- 不要因为最终周报语言压缩就误判遗漏；只要事实有等价表达即可视为覆盖。

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

# 要求
1. 只补充结构化草稿中明确存在的事实，不允许新增事实。
2. 不要输出日期范围、周数、源日报链接等头部信息。
3. 不要输出解释文字，不要输出 JSON，只输出修复后的 Markdown 正文。
4. 保留 mention 原样。
5. 保留 platform / project / topic 可见。
6. 不要为了压缩再次删除其他主题。

# 结构化草稿
{{structured_markdown}}

# 当前最终周报
{{final_markdown}}

# 校验结果 JSON
{{validation_json}}
"""


def build_header_markdown(weekly_atomic_pool):
    """保留原 weekly header 日期和源日报链接格式。"""
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


def build_structured_body_markdown(platform_map):
    """
    将趋势 JSON 合并结果先拼为“结构化事实草稿”。
    注意：这一步不是最终周报，只是给 final writer 的 markdown_content。
    """
    parts = []

    parts.append("### 🎉 本周关键进展")
    parts.append(render_platform_section(platform_map, "core_progress", empty_text="本周暂无核心产出。"))
    parts.append("")

    parts.append("### ✅ 本周核心进展")
    parts.append(render_platform_section(platform_map, "core_progress", empty_text="本周暂无核心产出。"))
    parts.append("")

    parts.append("### 📌 本周主要进展")
    parts.append(render_platform_section(platform_map, "main_progress", empty_text="本周暂无明确主要进展。"))
    parts.append("")

    parts.append("### ❗ 困难和所需支持")
    parts.append(render_platform_section(platform_map, "issues_support", empty_text="本周无明确阻塞性问题或外部支持需求。"))
    parts.append("")

    parts.append("### 🙌 下一步计划")
    parts.append(render_platform_section(platform_map, "next_plan", empty_text="暂无明确下一步计划，按既定路线推进。"))
    parts.append("")

    return "\n".join(parts).strip()


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
            max_tokens=int(project.get("weekly_final_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(project.get("weekly_final_temperature", 0.2)),
            stream=True,
            max_retries=int(project.get("llm_max_retries", 5))
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
            max_tokens=int(project.get("weekly_validation_max_tokens", 2048)),
            temperature=float(project.get("weekly_validation_temperature", 0.0)),
            stream=True,
            max_retries=int(project.get("llm_max_retries", 5))
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
            max_tokens=int(project.get("weekly_repair_max_tokens", LLM_MAX_TOKENS)),
            temperature=float(project.get("weekly_repair_temperature", 0.1)),
            stream=True,
            max_retries=int(project.get("llm_max_retries", 5))
        )
        cleaned = strip_markdown_wrapper(result)
        return cleaned or final_body_markdown
    except Exception as e:
        print(f"    ⚠️ 周报修复模型调用失败，保留原最终正文: {e}")
        return final_body_markdown


def generate_checked_final_body(structured_body_markdown, project):
    final_body = generate_final_weekly_body(structured_body_markdown, project)

    enable_check = project.get("enable_weekly_coverage_check", config.get("enable_weekly_coverage_check", True))
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

    if project.get("enable_weekly_second_validation", False):
        second_validation = validate_weekly_coverage(structured_body_markdown, repaired_body, project)
        return repaired_body, second_validation

    return repaired_body, validation_result


def build_final_markdown(weekly_atomic_pool, platform_map, project=None):
    """
    最终周报构建：
    1. 代码拼 header；
    2. 代码拼结构化事实草稿；
    3. final writer 整理正文；
    4. coverage checker 校验遗漏，必要时 repair；
    5. header + body 合并。
    """
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
        }
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


def write_debug_note_to_worklog_folder(project, title, markdown_content, extra_tags=None):
    project_name = project.get("project_name", "")
    project_guid = project.get("project_guid")
    work_log_folder_guid = project.get("work_log_folder_guid")
    creator_guid = project.get("weekly_target_user_guid") or USER_GUID

    tags = ["周报", "调试"]
    if extra_tags:
        tags.extend(extra_tags)

    doc_id = create_note_api(
        content=markdown_content,
        title=title,
        project_guid=project_guid,
        parent_guid=work_log_folder_guid,
        tags=tags,
        creator_guid=creator_guid
    )

    if doc_id:
        debug_url = f"{BASE_URL}/workspace/{doc_id}"
        print(f"[Debug][{project_name}] 🧪 调试笔记已写回 work log folder: {debug_url}")
        return debug_url

    return ""


def create_final_weekly_note(content, project, week_info):
    try:
        project_name = project.get("project_name", "")
        target_project_guid = project.get("weekly_target_project_guid")
        target_parent_guid = project.get("weekly_target_parent_guid", "0")
        target_user_guid = project.get("weekly_target_user_guid")

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
def generate_card_content(project, long_markdown, week_info=None):
    project_name = project.get("project_name", "")
    card_prompt_file_guid = project.get(f"{generate_type}_card_prompt_guid")

    default_prompt = config.get(
        "card_prompt_default",
        "请将以下周报内容整理为简洁的飞书消息卡片正文。"
        "要求：禁止使用标题语法（#、##）；使用项目符号（•）组织；突出核心进展、风险和下一步；"
        "不超过 300 字；不要输出 Markdown 代码块。\n\n{{markdown_content}}"
    )

    prompt_text = load_prompt_text(card_prompt_file_guid, default_prompt)

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
    card_input_markdown = f"{meta_header}\n\n{long_markdown[:8000]}"
    user_content = prompt_text.replace("{{markdown_content}}", card_input_markdown)

    try:
        llm_result = call_llm(
            messages=[
                {"role": "system", "content": "你是内容整理助手，请输出适合飞书卡片展示的精炼正文，不要输出代码块。"},
                {"role": "user", "content": user_content}
            ],
            max_tokens=int(project.get("weekly_card_max_tokens", 1024)),
            temperature=float(project.get("weekly_card_temperature", 0.3)),
            stream=True,
            max_retries=int(project.get("llm_max_retries", 5))
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
        json={"msg_type": "interactive", "card": card}
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
        payload["interactive_content"] = json.dumps(interactive_content)

    return requests.post(
        url=BASE_URL + MESSAGE_SEND_ROUTE,
        headers=get_headers_with_ak(user_guid=sender_guid),
        json=payload
    )


def step6_send_messages(note_url_list, note_title_list, project, content_list, week_info=None, source_note_entries=None):
    try:
        project_name = project.get("project_name", "")

        raw_webhook_config = project.get(f"{generate_type}_webhook_url", [])
        if isinstance(raw_webhook_config, str):
            webhook_urls = [raw_webhook_config] if raw_webhook_config else []
        elif isinstance(raw_webhook_config, list):
            webhook_urls = raw_webhook_config
        else:
            webhook_urls = []

        receiver_guids = normalize_receiver_guids(project.get(f"{generate_type}_sender_guid", []))
        sender_guid = project.get(f"{generate_type}_target_user_guid", "") or USER_GUID

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
                            print(f"  -> ❌ 群消息发送失败 ({url}): {webhook_result}")
                    except Exception as e:
                        print(f"  -> ❌ 群消息发送异常 ({url}): {e}")
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
print(f"开始执行周报工作流（原子池 platform 趋势+最终校验版 v2）| 项目数: {len(projects)}")
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
        # Step 1: 查找并加载上周每日原子池 JSON
        weekly_atomic_pool, found, step1_temp_files, source_note_entries = load_weekly_atomic_pool(project)
        temp_files.extend(step1_temp_files)

        if not found:
            print(f"  ⚠️ 跳过 {project_name}")
            cleanup_temp_files(temp_files, project_name=project_name)
            continue

        if not weekly_atomic_pool.get("items"):
            raise Exception("原子池中没有找到任何 items")

        # Step 2: 原子池 -> 保留 depth 的周级 timeline state
        timeline_state = build_weekly_timeline_state(weekly_atomic_pool)
        timeline_json_path = build_intermediate_json_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}",
            timeline_state,
            suffix="timeline_state"
        )
        temp_files.append(timeline_json_path)
        print(f"[Step 2][{project_name}] 📦 Timeline State 已生成: {timeline_json_path}")

        # Step 3: 按 platform/project 分块做趋势分析 JSON
        max_projects_per_batch = int(project.get("weekly_projects_per_batch", DEFAULT_MAX_PROJECTS_PER_BATCH))
        max_parallel_batches = min(int(project.get("batch_number", BATCH_NUMBER)), 50)

        trend_batch_results = analyze_trends_in_parallel(
            timeline_state=timeline_state,
            project=project,
            max_projects_per_batch=max_projects_per_batch,
            max_parallel=max_parallel_batches
        )

        trend_json_path = build_intermediate_json_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}",
            {"batches": trend_batch_results},
            suffix="trend_batches"
        )
        temp_files.append(trend_json_path)
        print(f"[Step 3][{project_name}] 📦 趋势分析 JSON 已生成: {trend_json_path}")

        # Step 4: 合并趋势 JSON，拼接最终周报 Markdown
        platform_map = merge_trend_results(trend_batch_results)
        merged_trend_path = build_intermediate_json_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}",
            platform_map,
            suffix="trend_merged"
        )
        temp_files.append(merged_trend_path)
        print(f"[Step 4][{project_name}] 📦 合并趋势 JSON 已生成: {merged_trend_path}")

        final_weekly_markdown, structured_body_markdown, validation_result = build_final_markdown(
            weekly_atomic_pool,
            platform_map,
            project=project
        )
        if not final_weekly_markdown:
            raise Exception("最终周报内容为空")

        structured_md_path = build_intermediate_markdown_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}_structured_draft",
            structured_body_markdown
        )
        temp_files.append(structured_md_path)
        print(f"[Step 4][{project_name}] 📝 结构化草稿 Markdown 已生成: {structured_md_path}")

        validation_json_path = build_intermediate_json_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}",
            validation_result,
            suffix="coverage_validation"
        )
        temp_files.append(validation_json_path)
        print(f"[Step 4][{project_name}] 📦 覆盖性校验 JSON 已生成: {validation_json_path}")

        final_md_path = build_intermediate_markdown_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}_final",
            final_weekly_markdown
        )
        temp_files.append(final_md_path)
        print(f"[Step 4][{project_name}] 📝 最终 Markdown 已生成: {final_md_path}")

        if project.get("write_weekly_debug_note", False):
            debug_title = build_weekly_note_title(week_info, project_name) + " 调试版"
            write_debug_note_to_worklog_folder(project, debug_title, final_weekly_markdown, extra_tags=["原子池"])

        # Step 5: 创建正式周报笔记
        note_urls, note_titles = create_final_weekly_note(
            final_weekly_markdown,
            project,
            week_info
        )

        # Step 6: 发消息
        step6_send_messages(
            note_urls,
            note_titles,
            project,
            [final_weekly_markdown],
            week_info=week_info,
            source_note_entries=source_note_entries[:5]
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
