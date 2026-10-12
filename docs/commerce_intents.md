# 十类客服意图

识别逻辑位于独立模块 `app/commerce/intent.py`，LangGraph 的 classify 节点调用 IntentRecognizer。启用语义识别后，少量精确、明确的请求直接走规则，其他表达进入独立模型，模型结合 Few-shot 与受控上下文返回结构化结果。未启用时保留原有本地规则模式。

## 两层识别与四项设计

```text
当前消息 + 当前会话摘要
  → 精确规则命中 → IntentResult
  → 未命中 → Few-shot + 独立模型 → Pydantic 结构校验、十类白名单、编号来源校验
  → LangGraph 上下文解析 → 参数/归属校验 → 业务工具
```

1. Few-shot：`app/commerce/intent_few_shot.json` 保存十类示例及资格/申请/进度、否定、改口、上下文、多诉求对比；修改后重启服务。
2. 结构化输出：`IntentResult` 包含 `intent`、`slots` 和 `needs_clarification`；slots 仅允许订单号、售后单号、商品号、规则主题、数量和配送区域。使用 JSON Schema 提示并在本地严格校验，不依赖模型自报置信度。
3. 独立模块：模型支持异步调用、独立模型名称/地址/密钥、超时；原 detect_intent 兼容函数仍可被旧测试和调用方导入。
4. 白名单：只允许本文十种意图，拒绝非法字段、格式错误和无输入/上下文依据的编号。原始消息中的明确编号由上下文节点重新提取，业务层继续校验归属。

仅传当前脱敏消息和上一轮意图、订单/售后/商品编号、规则主题；不传完整聊天历史、令牌或身份。模型超时、网络异常、格式不合法时返回澄清，不退化成猜测写入。多意图先澄清，不并行执行多项操作。模型输出仅为分类和参数，不授予业务执行权限。

## 启用配置

在本地 `.env` 或部署环境配置：

```dotenv
COMMERCE_INTENT_ENABLED=true
COMMERCE_INTENT_MODEL=qwen-plus
COMMERCE_INTENT_TIMEOUT_SECONDS=8
COMMERCE_INTENT_MAX_RETRIES=1
COMMERCE_INTENT_TRUST_ENV=false
# 已有 DASHSCOPE_API_KEY 时可以复用；也可单独设置 COMMERCE_INTENT_API_KEY。
```

默认关闭外部模型调用。该开关与商品回答开关 `COMMERCE_LLM_ENABLED` 独立。自定义兼容服务可设置 `COMMERCE_INTENT_BASE_URL`。意图客户端默认不读取系统代理变量，连接失败最多重试一次；若当前网络必须通过代理访问模型服务，设置 `COMMERCE_INTENT_TRUST_ENV=true`。模型仍不可用时使用本地规则分类，未知表达和模糊写操作继续澄清；订单归属校验及取消/售后确认不由模型替代。启用前需要有效密钥，启用后重启 API。Compose 已传递这些配置。

响应新增 `intent_source`（rule/model/rule_fallback/model_fallback）和 `intent_slots` 便于核对识别路径。规则与 Mock 模型测试用于验证编排、校验和降级，不能作为真实模型准确率。真实语义效果需使用已标注的独立问题集评测。

| 意图 | 示例 | 主要工具 |
|---|---|---|
| product_consult 商品咨询 | P100 耳机续航多久 | search_knowledge |
| rule_query 业务规则咨询 | 包邮规则是什么 | check_business_rule |
| order_query 订单查询 | 我的订单 | list_orders / query_order |
| logistics_query 物流查询 | 这单快递到哪了 | query_logistics |
| after_sales 售后资格咨询 | 这单还能退吗 | check_after_sales |
| cancel_order 取消订单 | 帮我取消订单 | prepare_operation |
| create_after_sale 售后申请 | 我要退货 | check_after_sales / prepare_operation |
| after_sale_query 售后进度查询 | 这单售后审核通过了吗 | query_after_sale |
| handoff 转人工 | 转人工 | human_handoff |
| other 其他与澄清 | 你好 / 无法识别的问题 | 问候或资料检索兜底 |

取消和申请仅生成确认卡片，仍需点击按钮调用独立确认 API。不会通过文字“确认”提交，模糊的“继续”不继承写入意图。否定表达先移除否定的操作片段再识别剩余请求，例如“我不想取消订单，只想看看物流”走物流查询。撤销售后申请当前返回不支持提示，不会当成取消订单。

## 售后进度

- 按订单查询：“查询 ODEMO001009 的售后进度”。
- 按售后单查询：“ASDEMO001008 审核通过了吗”。单号大小写均可识别。
- 已选订单后追问：“这单售后处理到哪了”。已查售后后追问：“审核通过了吗”。
- 未指定订单或售后单且没有上下文时，返回本人订单供选择；不会任意选择一笔。
- 没有售后申请时返回 no_after_sale，不会创建申请。
- 同时指定售后单和订单时，必须属于当前用户且两者匹配。切换订单会清除旧售后单上下文，查询失败后也不会沿用旧申请。

新增只读工具 `query_after_sale`，参数为可选的 `order_id`、`after_sale_id`，至少提供一个。该工具在 local 与 MCP 模式均可用，归入可重试的只读工具。

HTTP 接口（均需 Bearer 登录令牌）：

```text
GET /orders/{order_id}/after-sales
GET /after-sales/{after_sale_id}
```

返回订单号、售后号、状态和中文解释、申请/最近记录时间、申请金额与数量（有明细时）及按时间排序的状态历史。不会返回申请人身份、地址、申请原因原文或内部审核备注。旧售后记录没有明细和事件时，仍可查主表状态，不编造时间。

状态说明：submitted 已提交待审核；approved 审核通过待退回；returned 已记录退回待处理；closed 流程已关闭；rejected 申请未通过。关闭和审核通过均不代表真实退款到账。当前不提供退款账务查询、审核动作或撤销售后功能。

网页每张订单卡片都有“查询售后进度”按钮。修改代码后重启 API；MCP 模式还需重启 MCP 服务。
