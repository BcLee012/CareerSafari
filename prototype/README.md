# CareerSafari 原型（W2）

**定位**：求职信息导航 + 结构化岗位知识库。把散落在企业招聘官网、公众号、招聘平台上的
AI / 供应链 / 运筹优化岗位信息，整理成带来源可信度标注、可对比的结构化档案。

> ⚠️ **本站不提供招聘服务**——不发布岗位、不接收简历、不做投递撮合、不收费。
> 仅对已公开的招聘信息做整理与索引。详见 `/disclaimer`。

---

## 快速开始

```bash
cd prototype

# 1. 依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置管理令牌（写操作必需）
cp .env.example .env
openssl rand -hex 24        # 生成一个随机令牌，填到 .env 的 ADMIN_TOKEN=

# 3. 建库（数据源头是 collects/*.json，SQLite 是可重建的产物）
python load_collects.py      # 幂等：已存在的记录会自动跳过
python backfill_deadlines.py

# 4. 启动
uvicorn app.main:app --reload --port 8765
#   网页：http://127.0.0.1:8765/
#   API 文档：http://127.0.0.1:8765/docs
```

首次打开网页时，点右上角「令牌未设置」，填入 `.env` 里的 `ADMIN_TOKEN`。
令牌只存在浏览器 localStorage，不上传服务器。

---

## 安全模型

| 机制 | 说明 |
|---|---|
| **写接口鉴权** | `/parse`、`/jobs/{id}/calibrate`、`DELETE /jobs/{id}` 需请求头 `X-Admin-Token`。**`ADMIN_TOKEN` 未配置时这些接口直接返回 503（fail closed）**，而不是放行。 |
| **限流** | 进程内滑动窗口：`/parse` 每分钟 10 次，读接口每分钟 120 次，超限 429。多 worker 下为「每 worker 一份配额」。 |
| **SSRF 防护** | `/parse` 的 url 参数过协议白名单 + 公网地址校验，**重定向逐跳校验**（否则 302 到 `169.254.169.254` 就能拿到云元数据）。响应体上限 3MB。 |
| **XSS 防护** | `source_url` 出站时由服务端收敛协议，前端渲染进 `href` 前再校验一次（阻断 `javascript:` 等伪协议）。 |
| **开发逃生开关** | `FETCH_ALLOW_NON_PUBLIC=1` 跳过域名的公网校验（供透明代理环境本地调试）。**字面量内网 IP 仍会被拦截。公网部署务必删除。** |

---

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/` | — | 前端页面 |
| GET | `/disclaimer` | — | 数据来源与免责声明 |
| GET | `/healthz` | — | 健康检查（容器编排用） |
| GET | `/jobs` | — | 列表。`status` `company` `category` `job_type` `title` + `limit` `offset` 分页 |
| GET | `/jobs/{id}` | — | 详情 |
| GET | `/stats` | — | 统计（状态 / 行业 / 类型 / 在招分布 / 公司数） |
| POST | `/parse` | ✅ 令牌 | 接 URL 或正文 → 抽取 → 落库 |
| POST | `/jobs/{id}/calibrate` | ✅ 令牌 | 校准状态 |
| DELETE | `/jobs/{id}` | ✅ 令牌 | 删除单条 |

---

## 数据管线

主流招聘官网（BOSS / 字节 / 滴滴 / 美团 / 京东）都是 **JS 渲染的 SPA**，
本地 `requests` 抓不到正文。当前实际链路：

```
WebFetch 抓页面 → 结构化为 JSON → collects/*.json 落盘
                                       ↓
                          load_collects.py 载入 → SQLite
```

**`collects/*.json` 是唯一数据源头**，数据库是可重建的构建产物。
（历史教训：曾有一批数据抓完直接插库、没落盘，被后续重建清掉且无法恢复。
**抓完必须先写 JSON 再 load**——库里存的不是源头。）

---

## 来源可信度分级

| 等级 | 含义 | 说明 |
|---|---|---|
| **P1** | 企业官方 | 可信度最高，但普遍不披露薪资 |
| **P2** | 招聘平台 | BOSS / 猎聘 / 智联等 |
| **P3** | 转发 / 聚合 | 信息最全（含薪资区间），二手来源，需交叉核对 |
| **P4** | UGC | 小红书 / 知乎 / 面经，参考性最强、可信度最低 |

每条记录带：来源等级 + 页面类型 + 可信度说明 + 采集日期 + 原始链接。

---

## 容器部署

```bash
export ADMIN_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
# 访问 http://localhost:8000/
```

SQLite 数据存在 `careersafari-data` 卷。默认单 worker——**要提升并发请先把存储换成
PostgreSQL**，多 worker 只会让 SQLite 写操作互相等锁，且限流会变成每 worker 各一份配额。

---

## 测试

```bash
python tests/test_pipeline.py    # 端到端链路
python tests/test_security.py    # 鉴权 / SSRF / 限流 / 分页 / 时区（41 项断言）
```

两个脚本都不依赖 pytest，也可被 pytest 直接收集。

---

## 已知限制

- **尚未发布**：以个人身份在中国大陆无法合规公开运营招聘信息站（个人 ICP 备案禁止招聘类内容；
  经营性网络招聘服务需《人力资源服务许可证》，条件含 3 名专职人员）。当前定位为技术演示作品。
- **SEO 受限**：前端是单页应用，岗位详情没有独立 URL，`sitemap.xml` 目前只能列出静态页。
  要做岗位页 SEO 需先支持服务端渲染。
- **采集未自动化**：依赖智能体抓取 + JSON 落盘，尚未脱离人工。
- **薪资数据薄弱**：官方普遍不披露，`collects/salary_refs.json` 目前证据很少。
- **Dockerfile 未在本机构建验证**（本机无 Docker 环境），首次部署请留意构建日志。
