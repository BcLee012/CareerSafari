# CareerSafari · 职为你而来

> 求职信息**导航 + 结构化岗位知识库**——不做投递、不做撮合，做"求职前的信息决策层"。

把散落在企业招聘官网、公众号、招聘平台上的岗位信息，收敛成**按「行业 / 企业 / 岗位」三维组织、带来源可信度标注、可对比**的一站式信息入口。

## 当前阶段

**定位：技术演示作品**（不对外提供招聘服务）。

原计划阶段一是「个人自用工具」，阶段二备案公开发布。但在推进「做到可发布水平」时查证法规发现：
**以个人身份在中国大陆无法合规公开运营招聘信息站**——个人 ICP 备案明确禁止涉及招聘类内容；
而经营性网络招聘服务需取得《人力资源服务许可证》（条件含 3 名以上专职人员、固定场所、开办资金），
属创业级投入。

因此当前路径调整为：**把工程与数据管线打磨到生产级，作为技术作品展示**，
不对外提供求职/招聘服务。若未来要真正发布，需走企业主体 + 单位备案 + 许可证路线。

详见 [`prototype/static/disclaimer.html`](prototype/static/disclaimer.html)（站内 `/disclaimer`）。

## 首发行业

1. **互联网 AI 岗**（LLM / Agent 开发方向）
2. **供应链**
3. **运筹优化算法岗**

## 仓库结构

```
CareerSafari/
├── PRD-CareerSafari-求职信息导航网站.md    # 产品需求文档 v0.3（15 项关键决策）
├── 清单-代表企业-v0.1.md                   # 代表企业清单（69 家，按三条行业线组织）
└── prototype/                              # 可运行原型
    ├── app/
    │   ├── main.py         # FastAPI 接口层
    │   ├── security.py     # 鉴权 / 限流 / SSRF 防护
    │   ├── extractor.py    # 可插拔 LLM 抽取（OpenAI / Anthropic / 通义千问 / mock）
    │   ├── fetcher.py      # 抓取模块（协议校验 + 重定向逐跳校验 + 体积上限）
    │   └── storage.py      # SQLite 存储（WAL / 分页 / 唯一索引 / 东八区日期判定）
    ├── static/
    │   ├── index.html      # 单页前端：列表 + 详情 + 筛选 + 校准（响应式，零依赖）
    │   └── disclaimer.html # 数据来源与免责声明
    ├── collects/           # 采集产物（每个 JSON = 一次真实抓取，唯一数据源头）
    ├── tests/              # test_pipeline.py 端到端 + test_security.py 安全回归
    ├── load_collects.py    # JSON → 数据库（幂等，重复导入自动跳过）
    ├── backfill_deadlines.py # 按三级优先级回填招聘截止日期
    ├── Dockerfile / docker-compose.yml
    └── data/careersafari.db  # SQLite（构建产物，不入库，可由 collects 重建）
```

## 快速开始

```bash
cd prototype

# 1. 装依赖（建议先建虚拟环境）
pip install -r requirements.txt

# 2. 配置管理令牌 —— 写操作（解析/校准/删除）必需
cp .env.example .env
openssl rand -hex 24        # 填入 .env 的 ADMIN_TOKEN=

# 3. 由采集产物重建数据库
python load_collects.py      # 幂等加载 collects/*.json
python backfill_deadlines.py # 回填截止日期 → 派生在招状态

# 4. 启动
uvicorn app.main:app --reload --port 8765
#   网页：http://127.0.0.1:8765/
#   API 文档：http://127.0.0.1:8765/docs
```

或容器部署：`ADMIN_TOKEN=$(openssl rand -hex 24) docker compose up -d --build`

## 数据管线（核心认知）

主流招聘官网（BOSS / 字节 / 滴滴 / 美团 / 京东等）都是 **JS 渲染的 SPA**，
本地 Python `requests` 抓不到正文。当前实际链路是：

```
智能体 WebFetch 抓页面  →  人工/LLM 结构化为 JSON  →  collects/*.json 落盘
                                                          ↓
                                              load_collects.py 载入 → SQLite
```

`collects/*.json` 是**唯一数据源头**，数据库是可重建的构建产物。

## 来源可信度分级

| 等级 | 含义 |
|---|---|
| **P1** | 企业官方（官网 / 官方公众号）—— 最可信，但普遍不披露薪资 |
| **P2** | 招聘平台（BOSS / 猎聘 / 智联等） |
| **P3** | 公众号转发 / 聚合站转载 —— 信息最全（含日薪、时长），但是二手，需交叉核对 |
| **P4** | UGC（小红书 / 知乎 / 面经）—— 参考性最强、可信度最低 |

每条记录都带：来源等级 + 页面类型（岗位详情页 / 列表页 / 公告页）+ 可信度说明 + 采集日期。

## 功能一览

- **岗位列表**：多维筛选（状态 / 行业 / 类型 / 公司 / 岗位名）
- **技术栈 / 专业 chip 多选**：技术栈 AND 逻辑、专业 OR 逻辑，按命中数排序
- **在招状态自动判定**：在招 / 近期截止（≤14 天）/ 已截止 / 未知，支持"仅在招"过滤
- **薪资交叉补充**：官方不披露时，用 P3 参考区间补充（`collects/salary_refs.json`），不覆盖原文
- **字段完整度**：每条记录显示核心字段填充百分比
- **校准工作流**：`pending → calibrated → archived`

## 工程与安全

- **写接口鉴权**：`X-Admin-Token` 请求头；`ADMIN_TOKEN` 未配置时写接口返回 503（fail closed，而非放行）
- **限流**：`/parse` 10 次/分钟，读接口 120 次/分钟
- **SSRF 防护**：协议白名单 + 公网地址校验 + **重定向逐跳校验** + 响应体 3MB 上限
- **XSS 防护**：`source_url` 服务端与前端双重协议校验
- **存储**：SQLite WAL + `(company,title,location)` 唯一索引（重复导入幂等）+ 分页
- **时区**：截止日期按东八区判定，避免「已截止」差一天
- **SEO**：meta / OG 标签、`robots.txt`、`sitemap.xml`
- **测试**：`tests/test_security.py` 41 项断言覆盖上述机制

## 已知短板

- **薪资数据**是最大缺口——官方页普遍不披露，P3 转载需逐条核实
- **截止日期可能滞后**——所以列表同时显示采集日期，供判断信息新鲜度
- **采集未自动化**——依赖智能体抓取；若要脱离智能体，需 Playwright + LLM key
- **SEO 受限**——单页应用，岗位详情无独立 URL，`sitemap.xml` 目前只能列静态页
- **架构上限**——SQLite 单文件 + 进程内限流，适合中小规模；再往上需换 PostgreSQL 与 Redis

## 路线图

- [x] 工程加固：鉴权 / 限流 / SSRF / XSS / 分页 / 时区 / 移动端 / SEO / 容器化
- [ ] 扩量：字节 / 阿里通义 / 菜鸟 / SHEIN 等更多企业岗位（当前仅 19 条 / 4 家公司）
- [ ] 多岗位并排对比（PRD 核心价值，尚未实现）
- [ ] P3 薪资数据库按主流岗位逐条补齐
- [ ] 技能匹配度：填自己的技能栈 → 岗位匹配打分排序
- [ ] 接真实 LLM，替换 mock 抽取，对比准确率
- [ ] 采集半自动化：沉淀可复用的采集流程，降低手搓 JSON 的出错面
- [ ] 阶段二（需企业主体）：备案公开发布 + 爆料贡献体系 + AI 求职建议
