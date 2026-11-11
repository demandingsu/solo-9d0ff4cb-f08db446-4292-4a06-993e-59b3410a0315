# 市政工程投标截止时间采集与保证金核验服务

面向招投标专员的两个 API：

1. **公告采集**：提交市政工程公告站的**列表页地址**，服务自动遍历所有分页与
   详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布时间**
   （而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。
2. **保证金核验**：输入**已采集的原公告编号、标段号、企业编号与保证金平台明细
   目录 URL**，从原公告详情读取应缴金额与收款账户，沿同站分页读取该企业的到账、
   冲正与标段分配明细，以更正后的截止时间为界还原资金，返回目标标段的有效金额、
   缺口与逐笔来源。账目残缺或冲突时**标为无法判定，绝不把残缺账目判成不足**。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/bond/verify、/api/projects ...
│   ├── service.py          # 采集编排 + 事务边界 + 过期判定
│   ├── bond_service.py     # 保证金核验编排（DB 定位 + 页面读取 + 纯逻辑核验）
│   ├── bond.py             # 保证金资金还原与结构校验（纯函数，不依赖网络/DB）
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析（公告 + 保证金明细）
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── database.py         # SQLite 连接、建表、UPSERT
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 本地分页 HTML 示例站（公告页 + 保证金明细分页）
├── scripts/generate_demo_site.py  # 示例站生成器（数据可复现）
├── tests/test_collection.py       # 采集端到端 + 归并单元测试
├── tests/test_bond_verification.py# 保证金核验端到端测试（含故障注入）
├── tests/test_bond_logic.py       # 资金还原规则单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 核验 E1001 企业在 SZ-2026-001/G1 标段的保证金是否足额
curl -X POST http://localhost:8000/api/bond/verify \
  -H 'Content-Type: application/json' \
  -d '{"announcement_code": "SZ-2026-001", "lot": "G1",
       "enterprise_id": "E1001",
       "catalog_url": "http://demo-site/bond_1.html"}'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'

# 保证金核验（需先完成采集）
curl -X POST http://localhost:8000/api/bond/verify \
  -H 'Content-Type: application/json' \
  -d '{"announcement_code": "SZ-2026-001", "lot": "G1",
       "enterprise_id": "E1001",
       "catalog_url": "http://127.0.0.1:8001/bond_1.html"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/bond/verify` — 核验单个标段的保证金是否足额

核验为**只读**操作，不写入数据库。请求参数：

| 字段 | 含义 |
| --- | --- |
| `announcement_code` | 已采集的**原公告**编号（如 `SZ-2026-001`） |
| `lot` | 标段号（如 `G1`） |
| `enterprise_id` | 投标企业编号 |
| `catalog_url` | 保证金平台明细目录 URL，服务沿同站 `nav.pagination` 分页读全量明细 |

服务先用原公告编号从本地数据还原**更正后的生效截止时间**，再实时读取原公告
详情页的 `section.bond-section`（各标段应缴金额、收款账号）与保证金平台分页明细。

资金还原规则：

1. **分配必须引用到账**：每条 `allocation` 带 `arrival_id`，每条 `reversal` 带
   `reverses_id`；引用不存在或类型不符即“引用悬空”。
2. **分配总额受实收约束**：同一笔到账分给各标段的金额合计不得超过到账实收金额，
   否则“分配超额”。跨企业引用到账按归属冲突处理。
3. **截止时间为界**（受理时间 `<=` 生效截止时间才计入，边界时刻计入）：
   - 到账或分配受理时间晚于截止时间的不计入；
   - **截止前冲正**的到账及其全部分配整体不计入；**截止后冲正**不影响截止前
     已经形成的资金（冲正记录仍在 `excluded` 中说明）。
4. **只认公告指定账户**：到账收款账号与目标标段公告账号不一致的款项不计入；
   同一项目不同标段可指定不同账户，按标段各自比对。
5. 目标标段有效金额 = 通过上述全部校验且 `lot` 指向本标段的分配金额之和；
   `缺口 = max(应缴 - 有效, 0)`。

成功响应（200，账目自洽，`status=verified`）：

```json
{
  "status": "verified",
  "reasons": [],
  "announcement_code": "SZ-2026-001",
  "lot": "G1",
  "enterprise_id": "E1001",
  "target_lot": "SZ-2026-001/G1",
  "deadline": "2026-11-25 14:30",
  "deadline_source_code": "SZ-2026-001-G1",
  "deadline_source_url": "http://demo-site/SZ-2026-001-G1.html",
  "required_amount": "80000.00",
  "account_no": "8000100020260001",
  "valid_amount": "80000.00",
  "gap": "0.00",
  "is_sufficient": true,
  "sources": [
    {
      "allocation_id": "ALC-2026-0001",
      "arrival_id": "ARR-2026-0001",
      "allocation_accepted_at": "2026-11-12 15:00",
      "amount": "50000.00",
      "arrival_accepted_at": "2026-11-10 09:15",
      "arrival_amount": "50000.00",
      "account_no": "8000100020260001",
      "allocation_detail_url": "http://demo-site/bond_2.html#bond-ALC-2026-0001",
      "arrival_detail_url": "http://demo-site/bond_3.html#bond-ARR-2026-0001"
    }
  ],
  "excluded": [
    {
      "record_id": "ALC-2026-0003",
      "kind": "allocation",
      "amount": "10000.00",
      "reason": "分配至其他标段 SZ-2026-002/G1，不计入目标标段 SZ-2026-001/G1",
      "detail_url": "http://demo-site/bond_4.html#bond-ALC-2026-0003"
    }
  ],
  "arrival_count": 2,
  "reversal_count": 0,
  "allocation_count": 3,
  "catalog_url": "http://demo-site/bond_1.html",
  "announcement_detail_url": "http://demo-site/SZ-2026-001.html"
}
```

无法判定响应（**仍为 HTTP 200**，`status=indeterminate`，金额字段全部为 `null`，
不会输出 `is_sufficient=false` 这种“不足”结论）。触发情形：原公告详情页或任一
保证金分页读取失败、引用悬空、分配超额、同编号明细冲突、受理时间无法解析：

```json
{
  "status": "indeterminate",
  "reasons": ["分配超额: 到账 ARR-9101 实收 100.00，各标段分配合计 150.00"],
  "announcement_code": "SZ-2026-001",
  "lot": "G1",
  "enterprise_id": "E1001",
  "valid_amount": null,
  "gap": null,
  "is_sufficient": null,
  "sources": [],
  "excluded": []
}
```

只有请求引用的原公告未采集（`404 尚未采集`）或标段在原公告中不存在
（`404 不存在标段`）才返回 404；企业在平台上没有任何明细属于业务事实，
按有效金额 `0.00`、缺口等于应缴金额处理，**不是**无法判定。

## 示例站数据设计

### 公告站

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

原公告详情页内嵌 `section.bond-section`，逐标段给出应缴金额与收款账户；
其中 SZ-2026-005 的 G1/G2 两个标段**指定不同收款账号**。

### 保证金平台明细（`bond_1.html` ~ `bond_5.html`）

28 条到账/冲正/分配明细，固定随机种子打乱后分到 5 个分页，判定不依赖页面顺序。
主目录账目自洽，覆盖以下核验场景：

| 企业 | 目标标段 | 场景 | 预期结论 |
| --- | --- | --- | --- |
| E1001 | 001/G1 | 足额；一笔到账跨标段分配（合计不超实收） | 有效 80000，缺口 0 |
| E1002 | 001/G1 | 截止前冲正，到账与分配整体剔除 | 有效 30000，缺口 50000，不足 |
| E1003 | 004/G1 | 截止后冲正，不影响截止前资金 | 足额 |
| E1004 | 004/G1 | 汇入非公告指定账户 | 有效 0，缺口 30000，不足 |
| E1005 | 002/G1、G2 | 错账户/截止后款项剔除，G1 足额 G2 不足 | G2 有效 50000、缺口 70000 |
| E1006 | 003/G1 | 已过期项目的历史账目 | 足额 |
| E1007 | 005/G1、G2 | 同项目两标段不同收款账户 | G1 缺口 20000；G2 足额 |

另有三个**单页残缺目录**用于无法判定的端到端验证：

| 文件 | 构造的残缺类型 |
| --- | --- |
| `bond_dangling.html` | 分配引用了不存在的到账（引用悬空） |
| `bond_overallocation.html` | 同一笔到账分配合计超过实收（分配超额） |
| `bond_conflict.html` | 两条明细共用同一 record_id 但内容不同（同编号冲突） |

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 公告列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 公告详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）；
- 保证金信息：原公告详情页内 `section.bond-section table.bond-lots tr.lot-row`，
  行属性 `data-lot` / `data-amount` / `data-account-no`，单元格支持
  `td[data-field="标段号|标段名称|应缴金额|收款账户名称|收款账号|开户银行"]`；
- 保证金明细页：`ul.bond-record-list li.bond-record`，每条明细以
  `data-*` 属性给出字段：
  `data-record-id`、`data-kind`（`arrival`/`reversal`/`allocation`）、
  `data-enterprise-id`、`data-accepted-at`、`data-amount`，
  到账带 `data-account-no`，冲正带 `data-reverses-id`，
  分配带 `data-arrival-id` 与 `data-lot`（取值 `原公告编号/标段号`）；
  分页同样使用 `nav.pagination a`，与公告列表页一致。
