{
  "weekly_trend_prompt_file_guid": "趋势分析prompt文件guid",
  "weekly_final_prompt_file_guid": "最终整理prompt文件guid",
  "weekly_validation_prompt_file_guid": "覆盖校验prompt文件guid",
  "weekly_repair_prompt_file_guid": "修复prompt文件guid",
  "weekly_card_prompt_guid": "卡片摘要prompt文件guid"
}

{
  "ak": "",
  "sk": "",
  "org_guid": "",
  "user_guid": "",

  "llm_global": {
    "base_url": "",
    "api_key": "",
    "model": "doubao-seed-2.0-pro",
    "temperature": 0.3,
    "max_tokens": 4096,
    "print_stream": false,
    "max_retries": 5
  },

  "weekly_global": {
    "weekly_projects_per_batch": 5,

    "weekly_trend_temperature": 0.1,
    "weekly_trend_max_tokens": 4096,

    "weekly_final_temperature": 0.2,
    "weekly_final_max_tokens": 4096,

    "weekly_validation_temperature": 0.0,
    "weekly_validation_max_tokens": 2048,

    "weekly_repair_temperature": 0.1,
    "weekly_repair_max_tokens": 4096,

    "weekly_card_temperature": 0.3,
    "weekly_card_max_tokens": 1024,

    "enable_weekly_coverage_check": true,
    "enable_weekly_second_validation": false,

    "llm_print_stream": false,
    "batch_number": 10
  },

  "projects": [
    {
      "project_name": "CET周报",

      "state_project_guid": "",
      "state_parent_guid": "",

      "weekly_target_project_guid": "",
      "weekly_target_parent_guid": "",
      "weekly_target_user_guid": "",

      "weekly_trend_prompt_file_guid": "",
      "weekly_final_prompt_file_guid": "",
      "weekly_validation_prompt_file_guid": "",
      "weekly_repair_prompt_file_guid": "",
      "weekly_card_prompt_guid": "",

      "weekly_webhook_url": "",
      "weekly_sender_guid": []
    }
  ]
}
