# 我的 AI 情报

面向个人的模型、工程与行业信息收集项目。收录橘鸦早报与 AIHOT 的资料，由项目内的 DeepSeek Agent 拆条整理、提取事实、分析影响、合并同一事件，再在本站展示。

## 现在的使用方式

- **资讯首页**：焦点文章与图文列表，按模型 / 工程 / 行业分类，支持日期、搜索与分页。默认展示最新日期，列表摘要优先使用正文导语，配图来自对应新闻章节。
- **情报正文**：Agent 将事件的事实和原文摘录编排为连贯短文，将背景、具体进展与意义融入段落。相关配图穿插正文，支持点击放大、原尺寸查看和 Esc 关闭；文末提供延伸阅读和收录原文入口。
- **模型库**：按类型、机构、参数量、上下文、语言与许可查找模型，点击名称进入独立档案。档案汇集简介、能力与规格、相关报道、使用入口及资料更新；文章可关联多个模型。
- **内容管理**：从页脚进入，包含文章更新状态、模型审核和原文档案。首页不显示采集统计、整理进度或审核数量，也不请求后台状态和审核队列。
- **原文与审核**：原文章节、段落、引用、图片链接和全文文本继续保存；模型资料 Agent 联网确认身份、补充缺失字段并判断入库。闭源模型只要有可信存在依据也会收录，未公开的字段留空。

关注范围写在 `backend/collector/interest_profile.yaml`：**模型、工程和行业动态都关注，不限制技术方向、使用场景或硬件条件**。修改该文件影响后续分析；已经完成的情报不会自动重写。

## 启动

在项目根目录运行：

仓库包含程序、测试和配置示例；`.env`、本地工具设置及 `data/` 下的采集数据、数据库、备份和日志不上传。新环境启动时会初始化数据库；已有内容需另行迁移。

```bash
pip install -r backend/requirements.txt
# 复制 .env.example 为 .env，填写 DEEPSEEK_API_KEY
python -m uvicorn backend.main:app --port 8000
```

打开 http://localhost:8000 ，API 文档位于 `/docs`。使用单个 Web 服务进程；配置 Key 后会自动启动一个独立的模型资料执行进程，Windows 下不弹出终端窗口。执行日志位于 `data/model-worker.log`。

配置 Key 后，服务启动时采集一次，随后每天北京时间 09:00 采集。每轮最多收录 30 份新原文，按 URL 去重；一份日报可能拆出多条信息。已收录但待处理或失败的条目在下一次整理时继续处理。没有 Key 时仍可浏览已有资料和模型库，自动采集与分析不启动。

“内容管理 → 内容更新”中的“更新文章”处理待整理或失败的条目，并补齐尚未编排的正文。已完成的分析与正文不重复请求；同一事件合并新报道后，正文会重新编排。原文采集由定时流程负责。内容管理是独立浏览入口，不是权限隔离。

## 处理链路

```text
RSS / 详情页 → articles 原文档案
  → source_items 独立新闻章节
  → DeepSeek Agent 分类、事实提取、分析、同事件合并
  → insights 情报 + 来源引用 → 编辑 Agent 编排短文 → 本站情报流
  → 新模型名称 / Models Table → pending_models
  → model_tasks 持久任务 → 独立执行进程搜索、保存研究结果、判断入库
  → models 当前资料 + model_observations 字段依据及更正 + model_insights 关联报道
```

橘鸦日报按新闻章节拆分后，完整条目交给 Agent；不再以整份日报为一条情报，也不截取全文前 1500 字做分类。AIHOT 单篇报道作为一条来源信息。

结构化结果经过 JSON、条目对应关系、合并目标及引用校验；事实抽取保留能在输入正文中定位的摘录。编辑 Agent 只接收事件标题、保留的事实、摘录和来源日期，避免旧摘要或旧限制把已更正的说法带回正文。正文与底层事实分别保存，陈述与推断通过行文区分，不在阅读页堆叠审核栏目。引用匹配不能证明信源说法真实或模型概括完全准确；同事件合并也仍可能出现误判。

配图从数据库保存的 HTML 中按新闻章节提取，过滤日报封面、二维码与头像，跨报道去重后每篇最多展示 3 张，并保留来源。不会把整份日报的图片混进单个事件。图片仍从原地址加载，失效时隐藏；无配图的文章正常显示文字。当前按原文顺序选图，尚不做图像内容识别。正文编排失败时保留原有摘要与分析供阅读，下一轮可以继续编排。

状态 `pending / analyzed / filtered / failed` 及原因保存在数据库。校验失败不会发布为情报；可以重试。过滤只用于无实质信息的宣传或无关内容，重复报道保留为来源。新闻分析目前依赖已收录原文；模型资料 Agent 会进一步搜索和读取官网、模型卡、技术报告与提供商文档。

模型审核在“模型库 → 模型审核”，也可从页脚内容管理进入。每日整理产生的新候选、旧草稿、已有模型的新报道及到期复查都会进入持久任务队列；“运行资料 Agent”和“补查已入库模型”负责排队，每条记录也支持单独补查。执行进程逐项处理，页面显示队列、当前模型、处理阶段和失败重试；关闭浏览器不影响运行。

Agent 使用现有 `DEEPSEEK_API_KEY` 调用 DeepSeek Search / Fetch 与工具调用。每个模型最多搜索 3 次、读取 4 个页面，优先官方与提供商资料；保存查询、正文、身份摘录和字段引用。补充字段校验原文引用、类型及数字口径，空字段自动补齐，月份日期可细化到同月某一天。与已有非空值不同的资料形成更正建议，在管理页查看原文依据并采用或保留现值，保存旧值与处理记录。

模型存在状态、权重开放情况、API 可用性、公布日期和实际开放日期分别保存，不从旧发布日期推断开放日期。字段区分已知、尚未查到、明确未公开、不适用和资料冲突；“明确未公开”等判断需要正文依据。一般模型每 7 天复查，闭源或已有明确未公开字段的模型每 30 天复查；新报道和手动请求可以提前触发。身份待确认的草稿每 7 天复查。

任务用数据库租约防止同一任务被多个进程同时处理，90 秒过期、每 20 秒续期。研究结果保存后再进入入库判断；判断失败或进程重启可复用研究结果。搜索阶段尚未保存结果时中断会重新搜索。入库与任务完成在同一事务提交，失败最多尝试 3 次，间隔递增，之后在管理页重试；已有模型不会因复查失败被删除。服务停止期间不执行，重启后继续积压任务和到期任务。

入库标准是具体模型是否确实存在。闭源、参数未公开、没有 HF 仓库或暂未开放 API 都不妨碍收录，官方公布的研究模型也可入库；搜索后仍无法确认身份才保留草稿，明确不是模型的标为未收录。已有模型按名称和完整名称去重，补查在原记录上填空；联网失败保留已有数据，审核失败不会默认通过。网页和 LLM 判断仍可能出错，管理页保留人工确认和丢弃入口。

LifeArchitect 的 Models Table 使用公开 CSV 快照导入，保存来源和原始行作为审核依据。选取公布日期不在未来的模型，不因未开放使用排除候选，再交给资料 Agent 判断。月份精度的日期按月份展示，标为估算的参数量不写入确定参数栏；初始许可证只补充匹配到的 HF 元数据，其他缺失字段再联网查找。付费字段不采集。

## 存量处理

```bash
# 旧摘要补采正文，不调用 DeepSeek
python -m backend.collector.backfill_content
# 拆分并分析待处理/失败的条目，会调用 DeepSeek
python -m backend.collector.process_insights
# 仅补齐文章正文，不重新采集或分析，会调用 DeepSeek
python -m backend.collector.insight_editor
# 将 LifeArchitect 公开 CSV 中最近的 20 个候选加入持久任务队列
python -m backend.collector.import_models_table data/imports/lifearchitect-public-2026-10-22.csv --limit 20
```

新闻的离线整理命令与服务内整理不要同时运行。公开表导入只排队，可由执行进程接续处理。需要单独运行执行进程时使用 `python -m backend.collector.model_worker`；通常由 Web 服务自动启动即可。数据库结构自动增量迁移，原文与已有模型保留。本轮改造前备份位于 `data/tracker.before-model-profiles.db`。

## 主要文件

| 文件 | 作用 |
|---|---|
| `design.html` | 项目工程设计 |
| `backend/collector/sources.yaml` | 信源配置 |
| `backend/collector/interest_profile.yaml` | 个人关注范围 |
| `backend/collector/source_items.py` | 日报拆条与可重复入库 |
| `backend/collector/insight_agent.py` | 分析、校验、合并与重试 |
| `backend/collector/insight_editor.py` | 基于保留事实编排文章正文 |
| `backend/collector/insight_reader.py` | 按事件章节提取配图与延伸链接 |
| `backend/collector/insight_models.py` | 情报到模型审核队列 |
| `backend/collector/model_research.py` | DeepSeek 联网搜索、正文读取、字段引用校验 |
| `backend/collector/model_review.py` | DeepSeek 入库判断、理由保存与去重入库 |
| `backend/collector/model_jobs.py`、`model_worker.py` | 持久队列、租约、恢复、自动复查与独立执行 |
| `backend/collector/model_profiles.py` | 字段依据、更正、历史资料及文章关联迁移 |
| `backend/models/model_task.py` | 任务、字段观察记录与模型文章关联 |
| `backend/collector/import_models_table.py` | LifeArchitect 公开表快照导入 |
| `backend/collector/scheduler.py` | 原文采集与每日整理 |
| `backend/models/insight.py` | 来源条目与情报数据模型 |
| `frontend/index.html`、`frontend/insights.js` | 无构建步骤的前端 |
| `frontend/models.js`、`frontend/models.css` | 模型档案阅读及管理页的资料更正 |

## API

| 端点 | 说明 |
|---|---|
| `GET /api/insights` | 情报列表及章节配图：day/category/q/page/page_size |
| `GET /api/insights/dates` | 已发布文章的可浏览日期，不含未处理资料日 |
| `GET /api/insights/{id}` | 文章段落、配图、延伸链接及底层事实与来源 |
| `GET /api/insights/status` | 整理数量、状态、可用日期与关注范围 |
| `POST /api/insights/refresh` | 整理待处理/失败条目 |
| `GET /api/articles`、`GET /api/articles/{id}` | 来源列表、全文搜索与正文 |
| `GET /api/models`、`GET /api/models/filters` | 模型库与筛选值 |
| `GET /api/models/{id}` | 模型档案、相关报道、已采用的资料更新及资源入口 |
| `GET /api/pending` | 模型审核记录，status=draft/approved/rejected/all |
| `POST /api/pending/review` | 持久排队；pending_id 可指定单条补查复核 |
| `POST /api/pending/enrich` | 已有模型复查排队；model_id 可指定单个模型 |
| `GET /api/pending/changes` | 查看有资料冲突的更正建议 |
| `POST /api/pending/changes/{id}/accept`、`dismiss` | 采用更正或保留现值 |
| `POST /api/pending/{id}/approve`、`reject` | 确认入库或丢弃 |

## 验证

```bash
python -m unittest discover -s backend/tests -v
```

覆盖正文安全清理、拆条幂等、引用校验、失败重试、事件合并、模型审核，以及任务租约与中断恢复、研究结果复用、自动复查、资料更正、闭源字段状态和多模型关联。浏览器验证默认使用 Chrome，Chrome 不可用时使用 Edge；只有明确要求才使用 Tabbit。
