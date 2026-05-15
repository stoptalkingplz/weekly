================================================================================
🚀 开始处理非原子池周报项目: V1项目日报
[Step 1][V1项目日报] 目标周期: 2026-05-04 ~ 2026-05-10
[Step 1][V1项目日报] 搜索上周 7 天: ['2026-05-04', '2026-05-05', '2026-05-06', '2026-05-07', '2026-05-08', '2026-05-09', '2026-05-10']
[Step 1][V1项目日报] 原始日报空间 raw_daily_project_guid: 456098699202928712
[Step 1][V1项目日报] 原始日报目录 raw_daily_folder_guid: 489023062509928505
[Step 1][V1项目日报] ✅ 找到 5 份候选原始日报，开始解析 PM 日报板块...
    [Parse][V1项目日报] 2026-05-04 2026-05-04#W19-V1项目日报 -> section_blocks=14, person_blocks=2
    [Parse][V1项目日报] 2026-05-06 2026-05-06#W19-V1项目日报 -> section_blocks=35, person_blocks=5
    [Parse][V1项目日报] 2026-05-07 2026-05-07#W19-V1项目日报 -> section_blocks=27, person_blocks=6
    [Parse][V1项目日报] 2026-05-08 2026-05-08#W19-V1项目日报 -> section_blocks=26, person_blocks=5
    [Parse][V1项目日报] 2026-05-09 2026-05-09#W19-V1项目日报 -> section_blocks=64, person_blocks=5
[Step 2][V1项目日报] 📦 person_blocks 已生成: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_person_blocks_154c351b.json
    [Extract][V1项目日报] person_blocks batch 1/2, size=20
    🔄 [LLM 尝试 1/5] 调用 doubao-seed-2.0-pro ...
    ✅ LLM 流式调用完成
❌ 项目处理失败: V1项目日报, error=Expecting ',' delimiter: line 1 column 7349 (char 7348)
Traceback (most recent call last):
  File "/tmp/ipykernel_23/3224400871.py", line 2280, in process_weekly_project
    weekly_pool, ok, files, source_note_entries = load_weekly_raw_daily_pool(project)
  File "/tmp/ipykernel_23/3224400871.py", line 1209, in load_weekly_raw_daily_pool
    items = llm_extract_items_from_person_blocks(all_person_blocks, project)
  File "/tmp/ipykernel_23/3224400871.py", line 1074, in llm_extract_items_from_person_blocks
    parsed = safe_json_loads(result)
  File "/tmp/ipykernel_23/3224400871.py", line 264, in safe_json_loads
    return json.loads(object_match.group(1))
  File "/usr/local/lib/python3.10/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
  File "/usr/local/lib/python3.10/json/decoder.py", line 337, in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
  File "/usr/local/lib/python3.10/json/decoder.py", line 353, in raw_decode
    obj, end = self.scan_once(s, idx)
json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 7349 (char 7348)
================================================================================
📌 非原子池周报任务完成：success=0, failed=1
### Action
- 修改batch number为5
