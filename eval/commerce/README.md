# 电商综合评测

默认综合数据集为 `cases.json`。它已合并端到端业务样例、30 条参数误提取风险用例，以及 50 条规则难命中的意图表达。每条用例可带多个 `eval_tags`，报告按这些标签对应的有效分母统计指标。

在项目根目录运行**一条命令**：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_commerce_eval --dataset eval/commerce/cases.json --output eval/commerce/report_comprehensive.json
```

runner 默认读取项目 `.env` 的意图模型开关和密钥；`--with-model` 可强制启用意图模型。问答大模型仍按 `COMMERCE_LLM_ENABLED` 配置运行。评测使用本地模拟订单及本地工具；RAG需显式加 `--with-rag` 才会纳入本轮评测。

综合数据集当前 217 个场景（222 个意图步骤），越权样例 30 条、工具参数检查点与歧义护栏按报告 `metric_denominators` 计数。`task_completion_rate` 只统计带 `task_completion` 标签且有预期结果的场景；意图准确率统计所有步骤；越权率只统计风险样例；延迟覆盖所有步骤。

评测结果是本地模拟回归，不代表生产流量效果。单条 `model_required` 用例表示设计上期待模型处理；报告中的 `model_required_source_rate` 会依据真实 `intent_source` 显示实际走模型的比例。
