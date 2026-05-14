# 非原子池 PM 日报原文生成周报：Prompt、Config 与每一步 Input/Output

## 1. 设计目标

这个版本固定生成周报，不再使用 `generate_type`。

它参考基于原子池周报脚本的整体流程和 config 结构：

```text
config_file.path
  ↓
读取 ak / sk / org_guid / user_guid / llm_global / weekly_global / projects
  ↓
固定取上周一到上周日 7 天
  ↓
按项目读取数据
  ↓
生成中间态 items
  ↓
build_weekly_timeline_state
  ↓
platform/project 分批趋势分析
  ↓
生成结构化草稿
  ↓
最终周报整理
  ↓
coverage check
  ↓
repair
  ↓
写回 Workspace / 推送消息
```

与原子池版本唯一核心区别是：

```text
原子池版本：读取 atomic_pool.json → items
非原子池版本：读取原始日报 docJson → PM日报 person_blocks → LLM 抽取 items
```

---

## 2. Config 示例

### 2.1 推荐完整 config

```json
{
  "ak": "你的AK",
  "sk": "你的SK",
  "org_guid": "组织GUID",
  "user_guid": "默认执行人GUID",

  "llm_global": {
    "base_url": "http://agi-gateway.cxmt.com/cloud/v1",
    "api_key": "你的豆包API Key",
    "model": "doubao-seed-2.0-pro",
    "temperature": 0.3,
    "max_tokens": 4096,
    "print_stream": false,
    "max_retries": 5
  },

  "weekly_global": {
    "batch_number": 20,
    "target_sections": ["PM日报"],
    "skip_highlight": true,
    "person_heading_level": 3,
    "min_person_content_chars": 2,

    "extract_batch_size": 20,
    "weekly_extract_temperature": 0.0,
    "weekly_extract_max_tokens": 4096,

    "weekly_projects_per_batch": 8,
    "weekly_max_parallel": 10,
    "weekly_trend_temperature": 0.1,
    "weekly_trend_max_tokens": 4096,

    "weekly_final_temperature": 0.2,
    "weekly_final_max_tokens": 8192,

    "enable_weekly_coverage_check": true,
    "enable_weekly_second_validation": false,
    "weekly_validation_temperature": 0.0,
    "weekly_validation_max_tokens": 2048,
    "weekly_repair_temperature": 0.1,
    "weekly_repair_max_tokens": 4096,

    "write_back": true,
    "cleanup_temp_files": false
  },

  "projects": [
    {
      "project_name": "V1项目日报",
      "dept_name": "V1",
      "platforms": ["V1"],

      "raw_daily_project_guid": "原始日报所在项目空间GUID",
      "raw_daily_folder_guid": "原始日报所在目录GUID",
      "raw_daily_title_keywords": ["项目日报"],

      "weekly_target_project_guid": "周报输出项目空间GUID",
      "weekly_target_folder_guid": "周报输出目录GUID",

      "weekly_target_user_guid": "写入/创建周报使用的用户GUID",
      "weekly_sender_guid": ["接收人GUID1", "接收人GUID2"],
      "weekly_webhook_url": ["飞书机器人Webhook URL"],

      "weekly_extract_prompt_file_guid": "可选：本项目专用抽取prompt文件GUID",
      "weekly_trend_prompt_file_guid": "可选：本项目专用趋势prompt文件GUID",
      "weekly_final_prompt_file_guid": "可选：本项目专用最终整理prompt文件GUID",
      "weekly_validation_prompt_file_guid": "可选：本项目专用校验prompt文件GUID",
      "weekly_repair_prompt_file_guid": "可选：本项目专用修复prompt文件GUID"
    }
  ]
}
```

### 2.2 最小 config

如果只是测试、不写回、不推送，可以这样：

```json
{
  "ak": "xxx",
  "sk": "xxx",
  "org_guid": "xxx",
  "user_guid": "xxx",

  "llm_global": {
    "base_url": "http://agi-gateway.cxmt.com/cloud/v1",
    "api_key": "xxx",
    "model": "doubao-seed-2.0-pro"
  },

  "weekly_global": {
    "write_back": false,
    "target_sections": ["PM日报"],
    "skip_highlight": true
  },

  "projects": [
    {
      "project_name": "V1项目日报",
      "platforms": ["V1"],
      "raw_daily_project_guid": "xxx",
      "raw_daily_folder_guid": "xxx"
    }
  ]
}
```

---

## 3. 每一步 Input / Output 示例

## Step 1：查找上周原始日报

### Input

```json
{
  "project_name": "V1项目日报",
  "raw_daily_project_guid": "489xxx",
  "raw_daily_folder_guid": "490xxx",
  "date_list": [
    "2026-05-04",
    "2026-05-05",
    "2026-05-06",
    "2026-05-07",
    "2026-05-08",
    "2026-05-09",
    "2026-05-10"
  ]
}
```

### Output

```json
[
  {
    "date": "2026-05-04",
    "note_guid": "501xxx",
    "note_title": "2026-05-04#W19-V1项目日报",
    "user_guid": "执行人GUID"
  },
  {
    "date": "2026-05-05",
    "note_guid": "502xxx",
    "note_title": "2026-05-05#W19-V1项目日报",
    "user_guid": "执行人GUID"
  }
]
```

---

## Step 2：读取 docJson 并抽取 PM日报 person_blocks

### Input：Workspace docJson 片段

```json
{
  "type": "heading",
  "attrs": {"level": "2"},
  "content": [{"type": "text", "text": "PM日报"}]
}
```

```json
{
  "type": "heading",
  "attrs": {"level": "3"},
  "content": [
    {
      "type": "mention",
      "attrs": {
        "uid": "ab77...",
        "id": "421662430627799125",
        "label": "杜国安"
      }
    },
    {"type": "text", "text": " - PM"}
  ]
}
```

```json
{
  "type": "bulletListItem",
  "content": [{"type": "text", "text": "近期规划（一星期）：完成 ES 高风险项梳理并推动 WD 里程碑对齐"}]
}
```

### Output：person_blocks

```json
[
  {
    "block_id": "uuid",
    "date": "2026-05-04",
    "dept_name": "V1",
    "project_name": "V1项目日报",
    "platforms": ["V1"],
    "source": {
      "note_guid": "501xxx",
      "note_title": "2026-05-04#W19-V1项目日报",
      "source_url": "https://workspace.cxmt.com/workspace/501xxx"
    },
    "members": [
      {
        "uid": "ab77...",
        "id": "421662430627799125",
        "label": "杜国安",
        "mention_md": "[@杜国安](mention:ab77...:421662430627799125)"
      }
    ],
    "role_text": "PM",
    "raw_lines": [
      {
        "text": "近期规划（一星期）：完成 ES 高风险项梳理并推动 WD 里程碑对齐",
        "block_type": "bulletListItem",
        "depth": 0,
        "mentions": []
      }
    ]
  }
]
```

---

## Step 3：LLM 抽取 weekly_items

### Prompt：person_blocks -> weekly_items

```text
你是一个严谨的项目日报结构化抽取助手。

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
```

### Input

```json
{
  "person_blocks": [
    {
      "date": "2026-05-04",
      "project_name": "V1项目日报",
      "platforms": ["V1"],
      "members": [
        {
          "label": "杜国安",
          "uid": "ab77...",
          "id": "421662430627799125",
          "mention_md": "[@杜国安](mention:ab77...:421662430627799125)"
        }
      ],
      "role_text": "PM",
      "raw_lines": [
        {
          "text": "近期规划（一星期）：完成 ES 高风险项梳理并推动 WD 里程碑对齐"
        }
      ]
    }
  ]
}
```

### Output

```json
{
  "items": [
    {
      "date": "2026-05-04",
      "dept_name": "V1",
      "project_name": "V1项目日报",
      "platforms": ["V1"],
      "section": "next_plan",
      "member": {
        "uid": "ab77...",
        "id": "421662430627799125",
        "label": "杜国安",
        "mention_md": "[@杜国安](mention:ab77...:421662430627799125)"
      },
      "role_text": "PM",
      "content": [
        {
          "text": "计划在一周内完成 ES 高风险项梳理，并推动 WD 里程碑对齐。",
          "block_type": "bullet",
          "depth": 0,
          "mentions": []
        }
      ],
      "source": {
        "note_guid": "501xxx",
        "note_title": "2026-05-04#W19-V1项目日报",
        "source_url": "https://workspace.cxmt.com/workspace/501xxx"
      }
    }
  ]
}
```

---

## Step 4：weekly_items -> timeline_state

### Input

```json
{
  "items": [
    {
      "date": "2026-05-04",
      "project_name": "V1项目日报",
      "platforms": ["V1"],
      "section": "next_plan",
      "member": {
        "mention_md": "[@杜国安](mention:ab77...:421662430627799125)"
      },
      "content": [
        {"text": "计划在一周内完成 ES 高风险项梳理，并推动 WD 里程碑对齐。", "depth": 0}
      ]
    }
  ]
}
```

### Output

```json
{
  "metadata": {
    "source_type": "raw_pm_daily",
    "range_dates": ["2026-05-04", "2026-05-05"],
    "week_number": 19,
    "total_items": 1
  },
  "platforms": [
    {
      "platform": "V1",
      "projects": [
        {
          "project_name": "V1项目日报",
          "sections": {
            "progress": [],
            "issues_support": [],
            "next_plan": [
              {
                "date": "2026-05-04",
                "items": []
              }
            ]
          }
        }
      ]
    }
  ]
}
```

---

## Step 5：趋势分析 Prompt

```text
你是项目周报趋势分析助手。

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
```

### Output 示例

```json
{
  "platform": "V1",
  "core_progress": [
    {
      "project_name": "V1项目日报",
      "subtopic": "ES/WD 风险与里程碑推进",
      "items": [
        {
          "mention": "[@杜国安](mention:ab77...:421662430627799125)",
          "summary": "本周围绕 ES 高风险项和 WD 里程碑推进规划，完成风险项梳理并明确后续对齐方向。",
          "evidence_dates": ["2026-05-04"],
          "source_item_ids": ["uuid"]
        }
      ]
    }
  ],
  "main_progress": [],
  "issues_support": [],
  "next_plan": []
}
```

---

## Step 6：结构化草稿 Markdown

### Output

```markdown
### 🎉 本周关键进展
- **V1**
    - **V1项目日报**
        - **ES/WD 风险与里程碑推进**
            - [@杜国安](mention:ab77...:421662430627799125) 本周围绕 ES 高风险项和 WD 里程碑推进规划，完成风险项梳理并明确后续对齐方向。

### ✅ 本周主要进展
本周暂无明确主要进展。

### ❗ 困难及所需帮助
本周无明确阻塞性问题或外部协助事项。

### 🙌 下一步计划
- **V1**
    - **V1项目日报**
        - [@杜国安](mention:ab77...:421662430627799125) 下周继续推动 WD 里程碑对齐，并跟进 ES 高风险项闭环。
```

---

## Step 7：最终周报 Prompt

```text
# Role
你是一位严谨、客观、专业的项目周报整理助手。

你的任务是：将输入的“周报结构化汇总 Markdown”整理为最终可发布的团队周报正文。

# 核心目标
第一优先级是“主题覆盖完整”，不是语言优美。
输出结果必须尽可能保证：输入中每一个明确出现的 platform、project、subtopic、事项，都能在最终周报中找到对应落点。

# 输出结构
必须严格输出以下 4 个章节，不要新增其他一级章节：

### 🎉 本周关键进展
### ✅ 本周主要进展
### ❗ 困难及所需帮助
### 🙌 下一步计划
```

### Output 示例

```markdown
**日期范围：** 2026-05-04 至 2026-05-10 | **周数：** 第 19 周

**源日报链接：**
- 2026-05-04: [https://workspace.cxmt.com/workspace/501xxx](https://workspace.cxmt.com/workspace/501xxx)

---

### 🎉 本周关键进展
本周 V1 项目重点围绕 ES 高风险项和 WD 里程碑推进展开，PM 侧完成风险项梳理，并明确后续里程碑对齐方向。

### ✅ 本周主要进展
- **V1**
    - **V1项目日报**
        - [@杜国安](mention:ab77...:421662430627799125) 围绕 ES 高风险项和 WD 里程碑推进规划，完成风险项梳理并明确后续对齐方向。

### ❗ 困难及所需帮助
本周无明确阻塞性问题或外部协助事项。

### 🙌 下一步计划
- **V1**
    - **V1项目日报**
        - [@杜国安](mention:ab77...:421662430627799125) 继续推动 WD 里程碑对齐，并跟进 ES 高风险项闭环。
```

---

## 4. 这版和原子池代码的对应关系

| 原子池周报代码 | 非原子池周报代码 |
|---|---|
| `load_weekly_atomic_pool(project)` | `load_weekly_raw_daily_pool(project)` |
| 读取 `.json` 原子池文件 | 读取原始日报 `docJson` |
| `atomic_pool.items` | LLM 从 `person_blocks` 抽取的临时 `items` |
| `build_weekly_timeline_state()` | 保持一致 |
| `analyze_trends_in_parallel()` | 保持一致 |
| `build_final_markdown()` | 保持一致 |
| `coverage_check / repair` | 保持一致 |
| `insert_markdown_to_note()` | 保持一致 |
| `weekly_webhook_url / weekly_sender_guid` | 保持一致 |

---

## 5. 最关键的设计点

这个版本不是直接把原文扔给大模型总结，而是先做了一个“临时原子池”：

```text
原始 PM 日报人头块
  ↓
person_blocks
  ↓
LLM 抽取 weekly_items
  ↓
复用原子池周报链路
```

这样可以最大程度复用原来的周报生成、校验和修复逻辑，同时适配对方没有原子池的输入现状。

