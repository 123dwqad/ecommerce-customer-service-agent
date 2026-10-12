# 模型优先意图评测集

`intent_model_focused.json` 包含 150 条表达用例，覆盖十类意图，每类 15 条。新增表达以改述、上下文省略和间接诉求为主，标记 `rule_hard` / `model_required`，用于模型优先意图识别评估。该标签表示设计目标，并不保证每条在当前规则实现中都不会命中；评测结果中的 `actual_source` 才是实际路径。

该集合只测意图分类、标注槽位提取和识别延迟，不执行订单工具，因此不评测任务完成率、订单工具参数准确率或越权拦截率。模型 API 返回失败时按回退策略统计。

运行前需配置 `COMMERCE_INTENT_API_KEY` 或 `DASHSCOPE_API_KEY`：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_intent_model_eval
```

也可使用 `--dataset` 指定数据集、`--output` 指定报告。当前仅扩充并静态核对数据，新增 50 条尚未跑模型评分。
