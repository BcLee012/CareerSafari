# CareerSafari · 职为你而来

> 求职信息**导航 + 结构化岗位知识库**——不做投递、不做撮合，做"求职前的信息决策层"。

把散落在企业招聘官网、公众号、招聘平台上的岗位信息，收敛成**按「行业 / 企业 / 岗位」三维组织、带来源可信度标注、可对比**的一站式信息入口。

## 当前阶段

**阶段一：个人自用工具**（不备案、不公开推广、无 UGC）。
先用自己的求职场景把数据管线与岗位档案质量打磨扎实，作为阶段二（备案公开发布 + 爆料贡献 + AI 求职建议）的启动资产。

## 首发行业

1. **互联网 AI 岗**（LLM / Agent 开发方向）
2. **供应链**
3. **运筹优化算法岗**

## 仓库结构

```
CareerSafari/
├── PRD-CareerSafari-求职信息导航网站.md    # 产品需求文档 v0.3（15 项关键决策）
├── 清单-代表企业-v0.1.md                   # 代表企业清单（69 家，按三条行业线组织）
└── prototype/                              # 可运行的 W1 原型
    ├── app/
    │   ├── main.py         # FastAPI 服务 + 接口
    │   ├── extractor.py    # 可插拔 LLM 抽取（OpenAI / Anthropic / 通义千问 / mock）
    │   ├── fetcher.py      # 抓取模块（URL / 原文双入口）
    │   └── storage.py      # SQLite 存储（jobs 表 schema 对应 PRD §F1）
    ├── static/index.html   # 单文件前端：列表 + 详情 + 筛选 + 校准
    ├── collects/           # 采集产物（每个 JSON = 一次真实抓取，数据源头）
    ├── load_collects.py    # 把 collects/*.json 载入数据库（含去重）
    ├── backfill_deadlines.py # 按三级优先级回填招聘截止日期
    └── data/careersafari.db  # SQLite（构建产物，不入库，可由 collects 重建）
```

## 快速开始

```bash
cd prototype

# 1. 装依赖（建议先建虚拟环境）
pip install -r requirements.txt

# 2. 由采集产物重建数据库
python load_collects.py --replace     # 载入 collects/*.json 并去重
python backfill_deadlines.py          # 回填截止日期 → 派生在招状态

# 3. 启动
uvicorn app.main:app --reload --port 8765
#   网页：http://127.0.0.1:8765/
#   API 文档：http://127.0.0.1:8765/docs
```

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

## 已知短板

- **薪资数据**是最大缺口——官方页普遍不披露，P3 转载需逐条核实
- **截止日期可能滞后**——所以列表同时显示采集日期，供判断信息新鲜度
- **采集未自动化**——依赖智能体抓取；若要脱离智能体，需 Playwright + LLM key

## 路线图

- [ ] 扩量：字节 / 阿里通义 / 菜鸟 / SHEIN 等更多企业岗位
- [ ] P3 薪资数据库按主流岗位逐条补齐
- [ ] 接真实 LLM，替换 mock 抽取，对比准确率
- [ ] 采集自动化（Playwright + 反爬策略）
- [ ] 阶段二：备案公开发布 + 爆料贡献体系 + AI 求职建议
