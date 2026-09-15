# 公众号招聘推文 → 岗位档案 解析管线（W1 原型）

**目标**：手动贴入公众号招聘推文 URL 或正文 → 抓取 → 去噪 → LLM 抽取 → 落库为"待校准"岗位档案 → 你快速校准 → 入库正式。

## 当前进度

- ✅ 抓取模块（fetcher）：支持 URL / raw text 双入口
- ✅ 抽取模块（extractor）：可插拔 LLM 后端，**无 API key 时用 mock 返回示例**，方便先走通链路
- ✅ 存储模块（storage）：SQLite，岗位档案 schema 对应 PRD §F1
- ✅ FastAPI 接口：`POST /parse`、`GET /jobs`、`GET /jobs/{id}`、`POST /jobs/{id}/calibrate`
- ✅ 端到端测试通过（tests/test_pipeline.py）
- ✅ **前端浏览页面**（static/index.html）：左右双栏，列表+详情+校准，浏览器直接用

## 快速开始

```bash
# 1. 激活虚拟环境
source /Users/brucelee/.workbuddy/binaries/python/envs/default/bin/activate

# 2. 启动服务
cd /Users/brucelee/Documents/CodeProjects/CareerSafari/prototype
uvicorn app.main:app --reload --port 8765

# 3. 浏览器打开
#   网页：http://127.0.0.1:8765/
#   API 文档：http://127.0.0.1:8765/docs
```

## 接 LLM（可选）

把 `.env.example` 复制为 `.env`，填入你的 API key（OpenAI / Anthropic / 通义千问任一）。未填则默认走 mock。

```bash
cp .env.example .env
# 编辑 .env，选择 LLM_PROVIDER 与对应 key
```

## 用 raw text 直接试（推荐 W1 起步）

公众号 URL 抓取经常被反爬拦截，最稳的方式是直接粘贴正文：

```bash
curl -X POST http://127.0.0.1:8765/parse \
  -H 'Content-Type: application/json' \
  -d '{"text": "字节跳动 AI 团队招聘 LLM 应用工程师（北京/上海）..."}'
```

## 数据存储

SQLite 文件在 `data/careersafari.db`。表 `jobs` 字段：

| 字段 | 来源 |
|---|---|
| company / title / location / category | 抽取 |
| requirements / skills / responsibilities | 抽取（JSON） |
| salary_text / experience_text / education_text | 抽取 |
| source_url / source_tier / raw_text | 输入 |
| status (`pending` / `calibrated` / `archived`) | 校准动作 |
| created_at / calibrated_at | 时间 |

## 下一步（W1 后）

1. 接入真实 LLM，对比 mock 抽取准确率；
2. 公众号 URL 抓取加反爬策略（带 referer/UA、必要时上 Playwright）；
3. 校准页面（最简 HTML 表单）替代 curl。