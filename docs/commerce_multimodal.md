# 语音输入与图片识别

两个功能都调用外部模型 API，没有下载本地模型，不要求使用豆包。语音服务与视觉服务可以来自不同厂商，也不必与文本聊天模型使用同一 Key。

## 填写配置

### 千问语音（北京地域）

`qwen-audio-3.0-asr-flash` 使用百炼原生 JSON 协议，需要以下配置；图片配置独立保留。Key 必须属于对应地域的百炼账户。

```dotenv
COMMERCE_SPEECH_ENABLED=true
COMMERCE_SPEECH_PROTOCOL=dashscope_asr
COMMERCE_SPEECH_URL=https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
COMMERCE_SPEECH_MODEL=qwen-audio-3.0-asr-flash
COMMERCE_SPEECH_API_KEY=填写百炼Key
```

录音以 WAV Base64 上传，解析 `output.text`；识别后仍需用户确认发送，不会自动执行业务操作。其他兼容 multipart 的语音接口使用 `COMMERCE_SPEECH_PROTOCOL=multipart`（默认）。参见[百炼 HTTP API 文档](https://help.aliyun.com/zh/model-studio/fun-asr-flash-recorded-speech-recognition-http-api)。

项目根目录已有私有配置 `.env.multimodal`，模板是 `.env.multimodal.example`。私有文件已加入 `.gitignore`，Docker 镜像构建也不会包含它。

```dotenv
COMMERCE_SPEECH_ENABLED=true
COMMERCE_SPEECH_URL=https://你的服务商/完整语音转写接口路径
COMMERCE_SPEECH_MODEL=你的语音模型名称
COMMERCE_SPEECH_API_KEY=你的语音API密钥

COMMERCE_VISION_ENABLED=true
COMMERCE_VISION_URL=https://你的服务商/完整图片对话接口路径
COMMERCE_VISION_MODEL=你的视觉模型名称
COMMERCE_VISION_API_KEY=你的视觉API密钥

COMMERCE_MEDIA_TIMEOUT_SECONDS=45
```

只启用其中一项也可以。填写的是**完整端点地址**，不只是 Base URL；外部接口使用 HTTPS，本地模拟接口允许 localhost HTTP。配置文件路径按项目根目录定位，系统环境变量优先；修改后重启后端。

不要把真实 Key 写进前端文件或模板。未配置时，页面会显示“语音未配置 / 图片未配置”，普通聊天继续可用。主文本模型的 `COMMERCE_LLM_ENABLED` 与这两个开关互相独立。

Docker Compose 的 API 服务通过 `env_file` 加载 `.env.multimodal`，MCP 服务不接收这些 Key。此配置文件可不存在（Compose 需支持 `env_file.required`）；修改后重新创建 API 容器。Nginx 已单独为 `/media/` 放宽到 8 MB 请求体和 100 秒响应等待，其余接口仍保持原来的限制。

## 支持的接口协议

| 能力 | 外部请求 | 期望响应 |
| --- | --- | --- |
| 语音转写 | `POST multipart/form-data`，字段 `model` 和二进制 `file`；Bearer 鉴权 | `{"text":"识别出的文字"}` |
| 图片识别 | `POST application/json`，字段 `model`、`messages`；消息包含 `text` 和 `image_url`，图片以 JPEG Base64 Data URL 传入；Bearer 鉴权 | `choices[0].message.content` 为文本 |

这是两个明确的兼容协议，**不是任何厂商的任意 API 都可以只改 URL 使用**。WebSocket 实时语音、异步任务、签名鉴权、自定义 JSON 字段或原生豆包接口，需要根据该服务商文档新增适配器。目前没有自动调用备用厂商，也不会自动重试收费请求。

协议参考：[语音转写接口](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post)、[图片输入接口](https://docs.siliconflow.cn/docs/userguide/capabilities/vision)。可使用这些接口形状兼容的服务，不绑定该厂商；模型名称以你实际账号可用列表为准。

接入时需要的信息：服务商、完整 URL、模型名称、API Key，以及接口文档或请求示例。可把 Key 直接填入本地配置，无须放进聊天记录。

## 页面使用方式

**语音输入**：登录后点击“语音输入”，允许浏览器使用麦克风，讲话后点击“停止并转写”。转写结果进入文本输入框，检查后点击“发送”，才进入现有客服业务流程。也可以取消录音，不上传。

**上传音频**：选择浏览器可解码的音频（例如常见 WAV、MP3、WebM；具体支持取决于浏览器）。浏览器统一转换为 16 kHz、单声道、16 位 PCM WAV 后上传。最长 60 秒，源音频文件最多 12 MB，转换后 API 接收最多 4 MB。

**图片识别**：选择 JPEG、PNG、WebP 静态图片（不超过 5 MB、2000 万像素），可填写问题，例如“包装有哪些可见破损”或“商品标签写了什么”。本地预览后点击“识别图片”才上传，结果显示在对话区。后端会旋转校正、缩放至最长边 1600 像素并重新编码，去除文件元数据。

浏览器录音需要 HTTPS 或 localhost 安全上下文；普通 HTTP 远程部署通常不能获取麦克风权限，仍可尝试上传音频。权限被拒绝时页面会提示，不会反复弹出权限申请。

## 数据与业务边界

- 上传与识别接口需要登录。浏览器只与本项目后端通信，Key 不传给浏览器。
- 音频和图片只在处理期间保留在内存，没有保存到业务数据库、长期记忆或文件目录。外部模型服务的数据保留政策由服务商决定。
- 图片重新编码能移除 EXIF 等元数据，**不会自动遮盖图中的姓名、地址等可见内容**；这些像素会随图片发送给配置的服务。因此页面明确提醒不要上传隐私内容。
- 模型输出中的常见手机号、证件号等走现有文本脱敏。模型仍可能误读、幻觉或漏遮字段，不能把图片结果当作订单事实或售后证据认定。
- 图片分析没有业务工具权限，不自动传入 Agent，不自动关联订单；查询订单或申请售后需用户另外输入本人订单信息。图片结果暂不支持跨轮视觉上下文。
- 语音识别文本不会自动发送，即使内容是“取消订单”，也先进入草稿。进入原有聊天流程后，写操作仍需要确认卡片。
- 每个 API 进程最多同时向外部模型发送 4 个识别请求，超限返回忙碌提示。调用日志只记录请求标识、能力类型、状态和耗时，不记录 Key、图片、音频或转写正文。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-commerce.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_multimodal.py -v
```

测试通过 HTTP MockTransport 验证鉴权、外部请求协议、WAV 校验、图片规范化、文件限制、超时和错误处理，不调用真实模型。真实识别效果需填入你的服务配置后再联调。

当前支持**语音输入（ASR）与电商图片理解**，不包含语音播报（TTS）。
