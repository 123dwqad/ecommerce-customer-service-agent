# 电商智能客服 Agent

个人电商客服演示项目。支持商品资料查询、本人订单与物流查询、版本化售后政策校验、用户确认后的取消订单与售后申请，以及本地人工队列记录。

**不需要搭建 RAG 服务、向量数据库或 Embedding。** 商品资料用本地关键词检索；售后政策通过订单绑定的版本精确读取，Python 规则服务决定申请资格。大模型仅在可选模式下解释商品资料，不能改变规则判定或执行确认提交。

## 本地启动

从项目根目录执行，Python 3.12：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-commerce.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

打开 <http://127.0.0.1:8000>，注册测试账号。默认自动为新账号创建三笔独立的演示订单：待发货、运输中、已签收。无固定公共账号或密码。

本地默认使用 `data/commerce.db` 和进程内短期记忆，不依赖 Redis/MySQL/MCP 服务，不调用收费模型接口。本地模式仅运行一个 API 进程；多进程部署必须配置 Redis。

建议体验：

1. 询问“P100 耳机支持降噪吗”。
2. 点击某笔已签收订单，追问“这笔订单还能退吗”。
3. 输入“我要退货”，核对确认卡片后提交，结果是待审核申请，不是退款。
4. 选择待发货订单，输入“取消订单”，确认后观察状态变化。
5. 用另一账号查询第一账号的订单号，验证归属校验。

## 规则如何模拟真实项目

客服已支持商品咨询、规则咨询、订单查询、物流查询、售后资格咨询、取消订单、售后申请、售后进度查询、转人工、其他与澄清共十类意图。示例、工具和售后查询 API 见 [十类意图说明](docs/commerce_intents.md)。订单卡片上可直接点击“查询售后进度”。

已扩展为 **10 类规则**：库存限购、优惠叠加、运费与配送范围、发货时效、收货信息修改、取消订单、发票、保价、物流停滞和无理由退货。示例与配置方式见 [规则清单](docs/commerce_rules.md)。新增规则保存在 `app/commerce/business_rules.json`，通过同一个只读工具 `check_business_rule` 调用，不依赖 RAG。

政策配置：[`app/commerce/policies.json`](app/commerce/policies.json)。每条政策包含版本、商家、渠道、生效时间、适用品类、申请期限、可引用条款。

订单创建时绑定 `policy_version`。查询时，服务端读取订单归属、状态、签收时间和商品属性，再匹配政策版本及适用范围，返回 `eligible`、`reason` 和政策依据。缺失或不匹配的政策不会由模型补造；重复版本在加载时拒绝。

修改演示政策后重启 API 和 MCP。模拟政策升级时新增版本并为新订单绑定新版本，保留旧版本供历史订单查询。不要就地修改已有版本；当前是文件管理流程，尚无政策审批或不可变存储机制。

政策是**演示店铺配置**，不是所有商品的通用法律规则。当前自动处理范围仅包含无理由退货申请；质量争议、赔偿、定制和超期诉求需要人工。人工工具写入本地队列表，未对接真实客服。

## 架构与实现边界

新增语音输入与图片识别：前端支持录音、音频上传和图片预览，后端调用独立配置的外部 API。填写根目录 `.env.multimodal`，具体协议和操作见 [多模态接入说明](docs/commerce_multimodal.md)。默认关闭，未配置不会影响普通客服功能。

```text
Web → FastAPI 鉴权 → LangGraph
                    ├─ 意图识别（精确规则 / 可选 Few-shot 语义模型）
                    ├─ 会话上下文解析（商品 / 单笔订单）
                    ├─ 受控工具调用（local 或 MCP/SSE）
                    ├─ 确定性业务结果 / 可选 ReAct 商品问答
                    └─ 结构化回复 + 会话保存

工具 → 账号会话复核 → 订单归属校验 → 数据 / 政策规则
写入 → 确认卡片 → 独立确认 API → 版本检查 + 事务 + 幂等记录
```

- **LangGraph**：控制上下文解析、工具执行和输出校验。没有多智能体或 BERT 模型。
- **RAG 商品问答**：`COMMERCE_LLM_ENABLED=true` 且配置 `DASHSCOPE_API_KEY` 后，先检索商品知识，再由 Qwen 基于命中资料组织答案；资料未命中或不足时可补充通用知识，并明确标注未经商品资料验证。商品规格、价格、库存、商家政策等资料未覆盖时，模型会说明无法确认，不猜测；引用仅显示模型实际采用的资料。模型不可用时回退为最相关资料片段，未命中则提示暂时无法回答。业务写操作不暴露给模型。
- **短期记忆**：Redis，或单进程演示下的内存实现；按登录会话和 conversation_id 隔离、30 分钟过期、保留最近 20 条脱敏消息。模型不直接读取完整聊天历史。
- **用户偏好**：独立账户下的品类及回复风格白名单，可手工保存；品类用于“推荐商品”，回复风格用于可选模型回答。不自动提取个人敏感资料。
- **数据**：SQLAlchemy，SQLite 默认，MySQL 用于 Compose；业务表使用 `commerce_` 前缀。演示订单会持久化，确认通过数据库条件更新与事务防止重复写入。
- **MCP**：API 服务包装了具名工具；远端统一使用 `commerce_dispatch`。API 签名绑定会话、工具名称、参数和有效期，MCP 校验签名后重新验证数据库会话。模型看不到凭据。
- **隐私**：订单输出采用字段白名单，当前演示版不向 Agent 返回地址、电话和收件人；用户输入常见手机号、证件号、显式地址字段会脱敏。这不是通用 DLP，不承诺识别所有自由文本敏感信息。真实业务接入时，仅在任务确有需要且用户授权后，通过鉴权接口读取必要地址或联系方式，并限制传给模型的字段与展示范围。
- **受控操作**：确认绑定登录会话、订单 ID、订单版本和操作类型，5 分钟过期。确认 ID 同时作为幂等键，状态变化后必须重新确认。文字“确认”不会提交。
- **异常处理**：只读工具最多重试一次；写准备不自动重试；模型超时或格式异常回退。HTTP 请求标识串联 API 和工具日志；不记录令牌、消息正文或地址。

## MCP 本地联调

两个终端使用相同 `COMMERCE_DATABASE_URL`（默认值也可）和随机 `COMMERCE_SIGNING_KEY`（至少 32 个字符）。

```powershell
# 两个终端都设置同一个随机密钥，勿提交到 Git。
$env:COMMERCE_SIGNING_KEY = "<同一个随机密钥，至少32字符>"
```

终端一：

```powershell
.\.venv\Scripts\python.exe -m mcp_server.server
```

终端二：

```powershell
$env:COMMERCE_TOOL_TRANSPORT = "mcp"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

MCP 默认在 8004；使用 `COMMERCE_MCP_PORT` / `COMMERCE_MCP_URL` 调整服务端和客户端。

## Docker 演示部署

```powershell
.\.venv\Scripts\python.exe -m scripts.init_commerce_env
docker compose --env-file .env.prod up -d --build
```

密钥生成器不会覆盖已有 `.env.prod`。私密配置不要提交 Git。

Compose 启动 Redis、MySQL、MCP、API、Nginx，浏览器访问 <http://localhost:8080>。默认仍是演示商家数据；`COMMERCE_DEMO_ENABLED=false` 只停止为新用户生成订单，**不会变成真实商家接入模式**。

本版本没有实现真实商家的 OMS/WMS/售后平台客户端、真实退款、支付对账、短信登录、工单派发或生产级限流。适合本地演示和开发验证。

## 模拟数据库

运行 `.\.venv\Scripts\python.exe -m scripts.seed_commerce_data` 可补建商品、SKU、订单明细、包裹轨迹和售后历史等表，并生成 10 个测试账号、120 笔场景订单。原有数据保留，重复执行不会重复造数，随机账号密码保存在 `data/demo_accounts.json`。表结构、数据范围与兼容边界见 [数据库说明](docs/commerce_database.md)。

## 测试与评测

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m scripts.run_commerce_eval
```

测试使用临时数据库，包含独立进程的 MCP/SSE 联调，不需要 API Key。

电商测试集与报告在 `eval/commerce/`，覆盖商品咨询、订单物流、业务规则、售后、多轮上下文、越权和异常输入。评测检查意图、工具及订单参数、任务结果、越权拦截和耗时。运行时使用临时数据库，并关闭外部模型、Redis 和 RAG，属于**本地确定性回归**；报告指标不能当成模型或 RAG 准确率、真实接入成功率或线上延迟。

项目依赖见 `requirements-commerce.txt`；旧入口、评测和资料已清理，当前以 `app.main:app`、`mcp_server/server.py` 和电商前端为准。
