# 电商模拟数据库

数据库默认位置：`data/commerce.db`，也可由 `COMMERCE_DATABASE_URL` 配置。所有新增记录均为虚构演示数据。金额统一使用整数分，时间为 Unix 秒。

## 初始化与造数

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_commerce_data
```

默认创建 10 个独立账号，每个账号 12 笔订单。随机密码写入被 Git 忽略的 `data/demo_accounts.json`，不会输出到终端。通过网页登录其中一个账号即可查询对应订单。原有账号不会获得其他测试账号的订单。

脚本先使用 SQLite 在线备份 API 将现有数据库（包含 WAL 已提交内容）备份到 `data/backups/`，然后创建缺失表并写入数据。不会删除或重置旧账号、订单，也不会修改已有订单状态。新数据作为一个事务写入，账号重名时拒绝并回滚。MySQL 不执行 SQLite 备份。

重复运行不会重复创建已完成的场景批次，也不会重置库存、价格、密码、售后状态或模拟时间。`--users 1..100` 仅在首次生成该批次时有效；可用 `--credentials` 指定凭据文件。演示时间以首次造数时间为基准，之后会自然老化。

## 数据表

| 表名（均有 commerce_ 前缀） | 作用 | 关键字段 |
|---|---|---|
| users | 用户，原有 | id、salt、password（哈希）、preferences |
| sessions | 登录会话，原有 | token_hash、user_id、expires |
| products | 商品资料，新增 | id、name、category、description、attributes、customized |
| skus | 规格价格库存，新增 | id、product_id、specifications、price_cents、stock |
| policy_versions | 政策版本快照，新增 | version、topic、merchant_id、channel、content |
| orders | 订单主表，原有 | id、user_id、product_id、status、signed_at、policy_version、version、details |
| order_items | 订单明细，新增 | order_id、sku_id、quantity、unit_price_cents、paid_cents、product_snapshot、policy_version |
| shipments | 包裹，新增 | order_id、carrier、tracking_number、status、shipped_at、signed_at |
| tracking_events | 包裹轨迹，新增 | shipment_id、occurred_at、location、status、description |
| after_sales | 售后主表，原有 | id、order_id、user_id、status、policy_version |
| after_sale_details | 售后申请明细，新增 | after_sale_id、order_item_id、quantity、reason、requested_cents、created_at、updated_at |
| after_sale_events | 售后状态历史，新增 | after_sale_id、status、occurred_at、note |
| confirmations | 待确认操作与幂等结果，原有 | id、session_hash、order_id、order_version、action、expires、consumed、result |
| order_events | 订单状态记录，新增 | order_id、status、occurred_at、note |
| handoffs | 人工队列，原有 | id、user_id、reason、created |
| seed_batches | 造数批次，新增 | id、created_at、summary |

新增子表包含外键；SKU 库存/金额、明细数量和金额包含检查约束，包裹承运方与运单号组合唯一。SQLite 连接启用外键校验。旧表采用非破坏式兼容，未重建旧表或重写历史字段。

## 样例数据范围

- 18 个商品、54 个 SKU，覆盖电子产品、服装、定制商品；包含零库存商品 P104。
- 10 个测试账号、120 笔新增订单；每笔有订单明细和状态快照。
- 待付款、正常待发货、发货超时、定制已制作、运输中、物流停滞、签收可退、签收超期、已取消。
- 售后状态包含 submitted、approved、returned、closed、rejected；有对应的申请明细和按时间排序的事件。
- 新发货订单各有一个包裹、三条物流轨迹；所有运单号以 DEMO 开头。
- 10 条政策版本快照、10 条人工队列记录。
- 登录会话和操作确认是运行时数据，不预造可用令牌或伪造用户确认。

订单编号 `ODEMO001001` 表示第 1 个测试账号的第 1 笔订单。推荐登录 `demo_buyer_01`（密码见本地凭据文件）体验：

| 订单 | 场景 |
|---|---|
| ODEMO001002 | 已付款，尚在发货时限内，可测试取消 |
| ODEMO001003 | 发货超时 |
| ODEMO001004 | 定制商品已开始制作 |
| ODEMO001005 | 运输中，三条物流轨迹 |
| ODEMO001006 | 超过 48 小时未更新物流 |
| ODEMO001007 | 签收两天，可提交退货申请 |
| ODEMO001008 | 签收十天，超过演示无理由退货期限 |
| ODEMO001009 | 已有待处理售后申请 |

## 与客服业务的连接及边界

商品资料检索、规则使用的商品信息/价格/总库存从新表读取；物流查询优先读取包裹及轨迹表。新提交的售后会同步写入申请明细、初始事件；确认操作会写订单事件。旧订单缺少准确发货时间时保留原 JSON 物流查询路径，不猜测补造历史轨迹。

政策 JSON 仍是规则运行配置，policy_versions 是首次初始化时的数据库快照；修改已有 JSON 不会自动修改该快照，政策升级应新增版本。意图关键词仍来自内置商品目录，数据库自行新增商品后的任意别名识别尚未实现。

为兼容现有客服工作流，造数保持一单一个 SKU；order_items 表可以容纳多行，但当前取消/售后逻辑仍按整单处理，after_sales 仍限制一单一申请。售后进度已支持聊天、订单卡片按钮以及只读 API 查询，见 [十类意图](commerce_intents.md)。未实现多商品部分退货、SKU 指定库存查询或审核后台。库存查询为商品下各 SKU 可售库存之和，当前示例同商品 SKU 同价，不做跨 SKU 价保比较。

售后 closed 只是演示流程结束，不代表真实退款。当前数据与测试不涉及真实支付、库存预占、承运商或商家平台。
