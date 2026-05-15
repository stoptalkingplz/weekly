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

[Step 2][V1项目日报] 📦 person_blocks 已生成: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_person_blocks_d423c81a.json

    [Rule Convert] person_blocks=23, weekly_items=117

[Step 2][V1项目日报] ✅ 临时 weekly_items 已生成: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_weekly_items_6dccb57b.json, total_items=117

[Step 3][V1项目日报] 构建 timeline_state

[Step 3][V1项目日报] 📦 timeline_state 已生成: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_timeline_state_b1edd34d.json

[Step 4][V1项目日报] 开始 platform/project 分块趋势分析，共 1 批，并行数: 1

    [Trend Batch 1/1][V1项目日报][V1] 开始趋势分析，项目数: 1

    🔄 [LLM 尝试 1/5] 调用 doubao-seed-2.0-pro ...

    ✅ LLM 流式调用完成

    [Trend Batch 1/1][V1] ⚠️ 趋势分析失败，使用保底逻辑: Expecting ',' delimiter: line 135 column 10 (char 7722)

[Step 5][V1项目日报] 📦 trend_results 已生成: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_trend_results_93c85173.json

    🔄 [LLM 尝试 1/5] 调用 doubao-seed-2.0-pro ...

    ✅ LLM 流式调用完成

    🔄 [LLM 尝试 1/5] 调用 doubao-seed-2.0-pro ...

    ✅ LLM 流式调用完成

    ✅ 覆盖性校验通过

[Step 6][V1项目日报] 📄 结构化草稿: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_structured_body_0b273127.md

[Step 6][V1项目日报] 📄 最终周报: /tmp/weekly_raw_456098699202928712_20260504_to_20260510_final_weekly_7b20f096.md

[Step 6][V1项目日报] coverage_result={"pass": true, "missing_items": [], "wrong_or_suspicious_items": []}

    ⚠️ 未配置 weekly_target_parent_guid，将默认创建到根目录 parent_guid=0

[Step 7][V1项目日报] ✅ 周报写入完成: https://workspace.cxmt.com/workspace/492322091782135822, result={'code': 10000, 'msg': '插入成功', 'data': {}}

    🔄 [LLM 尝试 1/5] 调用 doubao-seed-2.0-pro ...

    ✅ LLM 流式调用完成

    ✅ Webhook 推送完成: {'StatusCode': 0, 'StatusMessage': 'success', 'code': 0, 'data': {}, 'msg': 'success'}

    ✅ 消息推送完成: {'code': 10000, 'msg': '服务调用成功', 'data': {'guid': ['492322175879110726']}}

✅ 项目处理完成: V1项目日报

================================================================================

📌 非原子池周报任务完成：success=1, failed=0
