# 电商智能客服 Agent

一个基于 Python、FastAPI 和 LangGraph 构建的电商客服演示项目，支持商品咨询、订单与物流查询、售后规则校验及用户确认后的业务操作。

## 功能

- 商品知识检索与 RAG 问答
- 订单、物流及售后进度查询
- Redis 短期会话记忆
- 规则校验、用户确认和越权防护
- Web 对话前端与 MCP 工具接入

## 本地运行

需要 Python 3.12。安装依赖并启动 API：

```powershell
python -m pip install -r requirements-commerce.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。配置 `COMMERCE_REDIS_URL` 后，短期会话记忆使用 Redis；未配置时使用单进程内存演示模式。

## 模型 API 配置

API 配置放在项目根目录的本地 `.env` 文件中；图片识别配置放在 `.env.multimodal`。这两个文件已加入 `.gitignore`，请勿把真实 API Key 提交到 GitHub。

- **主对话模型**：在 `.env` 中设置 `COMMERCE_LLM_ENABLED=true`、`DASHSCOPE_API_KEY=你的Key`，可用 `COMMERCE_MODEL` 指定模型。
- **图片识别**：在 `.env.multimodal` 中设置 `COMMERCE_VISION_ENABLED=true`、`COMMERCE_VISION_URL`、`COMMERCE_VISION_MODEL` 和 `COMMERCE_VISION_API_KEY`。

两项功能的 API 地址、启用开关和 Key 配置相互独立；可以使用不同服务，也可以在接口兼容时使用同一服务或 Key。修改配置后重启后端。详细说明见 [`docs/commerce_multimodal.md`](docs/commerce_multimodal.md)。

## 项目说明

本项目使用模拟商品、订单和店铺数据，仅用于学习与演示，不连接真实商家业务系统。
