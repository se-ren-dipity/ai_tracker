# 我的 AI 情报

面向个人的模型、工程与行业信息收集项目。收录橘鸦早报与 AIHOT 的资料，由项目内的 DeepSeek Agent 拆条整理、提取事实、分析影响、合并同一事件，再在本站展示。

代码仓库：[se-ren-dipity/ai_tracker](https://github.com/se-ren-dipity/ai_tracker)。服务器部署使用 `main` 分支，步骤见[Ubuntu 部署手册](docs/deployment-ubuntu.md)。

## 现在的使用方式

- **资讯首页**：焦点文章与图文列表，按模型 / 工程 / 行业分类，支持日期、搜索与分页。默认展示最新日期，列表摘要优先使用正文导语，配图来自对应新闻章节。
- **情报正文**：Agent 将事件的事实和原文摘录编排为连贯短文，将背景、具体进展与意义融入段落。相关配图穿插正文，支持点击放大、原尺寸查看和 Esc 关闭；文末提供延伸阅读和收录原文入口。
- **模型库**：按类型、机构、参数量、上下文、语言与许可查找模型，每页 20 条，列表描述最多两行并省略，点击名称进入独立档案查看全文。档案汇集简介、能力与规格、相关报道、使用入口及资料更新；文章可关联多个模型。分页与展示规则见[模型库列表说明](docs/model-library-ui.md)。
- **管理员模式**：从页脚登录，管理采集源开关、任务状态与失败重试、模型审核，并查看后台进程心跳和操作记录。首页不显示采集统计、整理进度或审核数量，也不请求后台状态和审核队列。
- **模型来源与审核**：LifeArchitect Models Table 的公开数据直接增量同步；新闻分析同时提取具体模型和证据，本地查库后直接建档或融合。默认不自动联网复查旧模型；管理员可手动补查。原文章节、引用和全文保留，未知字段留空，后台逐条显示收录结果。

关注范围写在 `backend/collector/interest_profile.yaml`：**模型、工程和行业动态都关注，不限制技术方向、使用场景或硬件条件**。修改该文件影响后续分析；已经完成的情报不会自动重写。

## 启动

在项目根目录运行：

仓库包含程序、测试和配置示例；`.env`、本地工具设置及 `data/` 下的采集数据、数据库、备份和日志不上传。新环境启动时会初始化数据库；已有内容需另行迁移。

```bash
pip install -r backend/requirements.txt
# 复制 .env.example 为 .env，填写 DEEPSEEK_API_KEY
# 本地 HTTP 设置 ADMIN_COOKIE_SECURE=false；服务器 HTTPS 保持 true
python -m backend.admin_auth
# 分别在三个终端运行（生产使用下方 Docker Compose）：
python -m uvicorn backend.main:app --port 8000
python -m backend.collector.collection_worker
python -m backend.collector.model_worker
```

打开 http://localhost:8000 ，API 文档位于 `/docs`。网页不再创建后台进程或定时任务，三个进程独立运行；服务器由 Docker Compose 管理。

Windows 本地已建立 `.venv` 后，推荐在项目根目录运行 `./start-local.ps1`，一次启动并检查网页、采集、模型维护三个进程；重复运行复用已有进程。`./start-local.ps1 -StatusOnly` 只检查状态。停止时双击 `stop-local.cmd`，或运行 `./stop-local.ps1`，会结束本项目这三个服务及其子进程；可重复执行，数据和日志保留。仅运行 uvicorn 不会执行 10 点采集。此脚本不注册开机自启，电脑重启后需重新运行，详见[本地运行说明](docs/local-runtime.md)。

每天北京时间 10:00 生成唯一的每日任务，先同步 Models Table，再采集新闻、联合分析新闻与模型、直接收录并对账，最后编排正文。重启后恢复中断任务；若当天 10 点已过但任务尚未创建，补跑当天一次，不按停机天数补造历史日报。10 点前首次启动需等待定时执行或从后台手动采集。表格和 RSS 原文采集不依赖 DeepSeek Key；没有 Key 时分析阶段显示等待配置，模型补查进程保持在线等待。每轮遍历全部订阅源并保存其返回的新原文，不再因累计 30 篇而跳过后面的来源；首次接入会补录当前 RSS 窗口内的历史文章，随后按每批 6 条分析。普通文章按 URL 去重，微信长链接按公众号、消息及子文章标识（`__biz/mid/idx`）去重，忽略跟踪参数变化，同时保留原始阅读链接。已收录但待处理或失败的条目在下一次整理时继续处理。表格同步失败会记录日志并保留已有数据，不阻止新闻流程；新闻失败不回滚已经同步的模型。

普通页面已隐藏管理员入口；现有受保护的 `/#manage` 直达页暂保留。“采集并更新”重新采集各来源、处理待整理或失败的条目，并补齐尚未编排的正文。已完成的分析与正文不重复请求；同一事件合并新报道后，标题、摘要和正文会一起重新编排。所有管理写接口及审核数据需要管理员会话，写操作校验 CSRF；阅读资讯和模型库不要求登录。

后台新闻分析、正文编排、模型补提取、资料补查与审核、HF 评测抽取均显式开启 DeepSeek 思考模式，程度为 `high`。统一参数、输出上限与验证结果见[后台思考模式](docs/model-agent-v1.md#后台思考模式)。更新代码并重启后对新任务生效，已完成结果不会自动重跑；RSS 与 Models Table 抓取不调用大模型。

来源请求遇到连接错误、429 或 5xx 时最多尝试 3 次；定时采集仍失败时，间隔 30 分钟最多再补跑 2 次。经过解析校验的数据保存在数据库同目录的 `source-cache/`，断网时可补录缓存中尚未入库的内容，缓存时间与刷新错误在内容管理中明确展示，不能视为最新同步。源站无法直连时可在 `.env` 设置 `COLLECTOR_PROXY` 为可用 HTTP 代理；程序不自动修改系统代理。

事件整理使用独立的事件类型与原文链接：发布、评测、预告、正式提交等不同动作不合并；具体公告链接用于召回同事件报道，通用首页或模型卡不会强制合并。逐条分析结果保存在来源条目中。同一次分析提取本次发布或新增事实的主体模型，不为比较基线逐个补查；所有类别的具体模型都可提名。收录阶段复用 MCP 的本地检索函数，身份原文通过校验后直接建档或融合，不额外请求研究及审核模型。综合语音对话和同声传译使用 `Speech`，与 `TTS`、`ASR` 区分。

## 服务器部署

本轮数据核对、清理结果和同步文件见[2026-10-29 发布前整理](docs/release-readiness-2026-10-29.md)。只读复核：`python -m backend.maintenance`。代码库不包含 `.env`、`data/`、`output/`；数据库通过私有快照单独迁移，已有服务器库不能直接覆盖。

Ubuntu 24.04 使用服务器 IP 访问的完整步骤见 [部署手册](docs/deployment-ubuntu.md)，包含 Git 拉取、数据迁移、Nginx 访问限制、备份及回退。

服务器使用仓库中的 `docker-compose.yml`，统一启动 `backend`、`collector`、`model-worker`，共享本机磁盘上的 `data/`。默认发布到 `127.0.0.1:29011`，通过 SSH 隧道或 HTTPS 反向代理访问。设置密码、迁移和运行命令见部署手册。不要继续使用旧版仅有一个服务的 `compose.server.yml`。

任务步骤、来源开关、登录会话及管理记录存入 SQLite，网络请求不占用数据库事务。一个采集进程和一个模型进程分别持有操作系统文件锁，防止同机重复启动。SQLite 等待写锁最多 30 秒，适用于当前单机规模；不要将数据目录放到 NFS 或让多台服务器共享该文件。模型补查单项上限 15 分钟，已有搜索检查点可在重试时复用。

容器退出自动重启，日志每个服务最多 3 × 10 MB。健康检查用于发现进程/数据库异常；Docker 不会仅因为 unhealthy 自动重启容器。管理员页面区分进程在线、任务成功、来源使用缓存三种情况。备份使用 `python -m backend.backup` 的 SQLite backup API，保留数据和任务状态。本机未安装 Docker，镜像构建仍需在目标服务器验证。

文档采用[按需周整理](docs/document-agent-design.md)：由用户发起整合任务，按模态组织正文和图片；当前不部署自动文档同步服务。

网站宠物助手“囤囤”已接入：使用用户选定的冷色仓鼠 SVG，支持整体拖动与位置记忆、双击摸头和动作菜单；基础待机约 10 秒后插入六段随机表演之一，每轮不重复，提问立即播放检索动画。点击“聊一聊”查询新闻、筛选模型、简短闲聊，回答附证据链接。访客会话隔离，默认每天 20 次，管理员可调配额与开关。复用现有 DeepSeek 密钥，无新增依赖，本地动作不消耗问答额度。见[使用与验证](docs/pet-agent-v1.md)、[助手设计](docs/pet-agent-design.md)和[系统提示词](docs/pet-agent-system-prompt.md)。本机已验证，服务器仍需按部署说明更新。

## 处理链路

```text
RSS / 详情页 → articles 原文档案
  → source_items 独立新闻章节
  → DeepSeek Agent 分类、事实提取、分析、同事件合并
  → insights 情报 + 来源引用 → 编辑 Agent 编排短文 → 本站情报流
  → 同一次新闻分析提取 model_candidates → 本地查库 → pending_models 保存处置
  → 直接新增基础档案 / 已有字段融合 / 仅关联 / 身份待确认
  → models 当前资料 + model_observations 字段依据及更正 + model_insights 关联报道

LifeArchitect 公开 CSV → 结构与日期校验 → 按模型身份比较来源行
  → models 新增 / 补空 / 更新本来源维护的字段（不调用 DeepSeek）
  → model_table_entries 来源快照与已写字段 + model_observations 更新历史
```

橘鸦日报按新闻章节拆分后，完整条目交给 Agent；不再以整份日报为一条情报，也不截取全文前 1500 字做分类。AIHOT 单篇报道作为一条来源信息。

新智元、机器之心接入用户指定的 BestBlogs 微信 RSS，优先直接保存 RSS 正文、图片链接和发布时间，不重新访问微信文章页。2026-10-26 测试时两源均返回 10 篇全文，最新条目为 10-25，源端存在延迟；当前接口未提供 GUID，使用微信文章链接中的稳定标识去重。RSS 窗口之外的历史文章及尚未由源端更新的内容不能凭本地采集恢复。量子位及之前的其他 RSS 地址未加入配置。

跨来源的同事件报道合并为一条情报并保留各自原文入口；同批重复事件要求统一事件标识。事实只在中文表述和原文引用均相同时去重，避免因表述相同而丢弃不同的数字、限制或更正。同一模型的不同版本、发布与评测等不同事件仍分别记录；事件识别依赖 Agent，不能保证语义去重绝无误判。

结构化结果经过 JSON、条目对应关系、合并目标及引用校验；事实抽取保留能在输入正文中定位的摘录。编辑 Agent 只接收事件标题、保留的事实、摘录和来源日期，避免旧摘要或旧限制把已更正的说法带回正文。正文与底层事实分别保存，陈述与推断通过行文区分，不在阅读页堆叠审核栏目。引用匹配不能证明信源说法真实或模型概括完全准确；同事件合并也仍可能出现误判。

配图从数据库保存的 HTML 中按新闻章节提取，过滤日报封面、二维码与头像，跨报道去重后每篇最多展示 3 张，并保留来源。不会把整份日报的图片混进单个事件。图片仍从原地址加载；首页首图加载失败时依次尝试同一事件的其他配图，全部失败才隐藏，正文中的失效图片隐藏；无配图的文章正常显示文字。当前按原文顺序选图，尚不做图像内容识别。正文编排失败时保留原有摘要与分析供阅读，下一轮可以继续编排。

状态 `pending / analyzed / filtered / failed` 及原因保存在数据库。校验失败不会发布为情报；可以重试。过滤只用于无实质信息的宣传或无关内容，重复报道保留为来源。新闻分析及普通收录依赖已收录原文；管理员手动补查时，资料 Agent 才会进一步搜索和读取官网、模型卡、技术报告与提供商文档。

模型审核从管理员入口进入。橘鸦“模型发布”、AIHOT 模型栏目和其他来源的发布/更新线索逐条对账；多个尺寸、Base/SFT 等分别处理，系列总称不重复建档。新模型先建基础档案，已有模型只融合有证据的新增字段，身份不清时保留候选。旧结果缺少提取信息时至多每批 6 条补提取一次（校验失败再修复一次），不联网搜索；内容指纹相同的已完成条目不重复调用。后台区分对账完成、已入库和仍待核对。“运行资料 Agent”和“补查已入库模型”仍可手动排队。

手动补查 Agent 先检索模型库，再使用现有 `DEEPSEEK_API_KEY` 按需调用 DeepSeek Search / Fetch；每个模型最多搜索 3 次、读取 4 个页面。普通新闻收录不走该流程。空字段可自动补齐，月份日期可细化到同月某一天；后续权重/API 状态变化需有明确日期和既有资料入口的证据，其余冲突进入更正建议。人工确认字段会锁定。只读 MCP 与收录程序复用同一套档案检索逻辑。

模型存在状态、权重开放情况、API 可用性、公布日期和实际开放日期分别保存。默认 `MODEL_AUTO_REVIEW_ENABLED=false`，关闭表外模型 7/30 天和草稿 7 天的周期补查；新新闻直接提供增量证据，资料不全不自动发起全量研究。升级后旧自动待执行任务暂停，保留历史和手动重试入口；只有显式启用该变量才恢复旧周期策略。

手动补查任务使用 90 秒租约、每 20 秒续期，研究结果保存后可在重试时复用；失败最多尝试 3 次。普通新闻收录通过内容指纹和事务保存档案、来源关联及对账结果，中断后重放不重复新增。新流程未完成结果和失败来源跨日继续处理；升级前的旧来源只自动核对当天发布或当天抓取部分，不扫描全历史发起付费研究。

入库标准是具体模型是否确实存在。闭源、参数未公开、没有 HF 仓库或暂未开放 API 都不妨碍收录，官方公布的研究模型也可入库；搜索后仍无法确认身份才保留草稿，明确不是模型的标为未收录。检索结合名称、机构和已确认别名，同名歧义与版本差异不能自动合并；同一版本的开放权重、上线 API 等变化记录在原档案及事件时间线中。联网失败保留已有数据，审核失败不会默认通过。网页和 LLM 判断仍可能出错，管理页保留人工确认和丢弃入口。

LifeArchitect Models Table 是模型库的主来源，以 LLM 为主并包含部分多模态模型，不能作为全模态模型库的唯一来源。同步读取其网页使用的公开 CSV 接口，首次补齐全部已公布的记录，以后比较完整公开行，仅处理新增和变化的内容；旧模型的资料修订也能同步，不限于最新 20 行。按机构与模型名识别，表内同名版本再用仓库或资料链接区分，不使用会随排序变化的 Count 编号。

公开表直接提供名称、机构、非估算参数、评测、资料链接、月份精度的日期和说明，保存来源快照。新模型直接入库，已有模型补齐空字段；后续只自动覆盖本来源写入且未被人工修改的值。人工修订、已有非空资料和更精确的发布日期保留，来源删除整行不会删除本地模型。参数后来被标为估算时，撤回仍由本来源维护的确定参数值。月份日期仍按月份显示，Public 标记不直接推断开放权重或 API 状态。公开表没有可靠的模态分类列，新模型类型暂为 Unknown，不统一误标为文本模型；既有分类保留。许可证、上下文等付费字段不采集、不猜测，按需手动补查。

## 存量处理

```bash
# 旧摘要补采正文，不调用 DeepSeek
python -m backend.collector.backfill_content
# 拆分并分析待处理/失败的条目，会调用 DeepSeek
python -m backend.collector.process_insights
# 仅补齐文章正文，不重新采集或分析，会调用 DeepSeek
python -m backend.collector.insight_editor
# 立即增量同步 LifeArchitect 最新公开表，不调用 DeepSeek
python -m backend.collector.import_models_table
# 也可从本地公开快照同步；--limit 20 可用于手动小批量验证
python -m backend.collector.import_models_table data/imports/lifearchitect-public-2026-10-26.csv
```

离线整理或同步命令不要与服务内同类流程同时运行。公开表同步和新闻增量收录在采集进程完成；独立模型进程默认处理手动补查。使用 `python -m backend.collector.model_worker` 启动，Docker Compose 会单独管理此进程，Web 服务不会代为启动。数据库结构自动增量迁移，原文与已有模型保留。

## 主要文件

| 文件 | 作用 |
|---|---|
| `docs/model-registry-agent-design.md` | 当前模型维护设计；早期 HTML 原型已归档 |
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
| `backend/collector/import_models_table.py` | LifeArchitect 公开表直接增量同步与快照导入 |
| `backend/models/model_table.py` | 表格来源行、模型映射和字段维护记录 |
| `backend/collector/scheduler.py` | 原文采集与每日整理 |
| `backend/models/insight.py` | 来源条目与情报数据模型 |
| `frontend/index.html`、`frontend/insights.js` | 无构建步骤的前端 |
| `frontend/models.js`、`frontend/models.css` | 模型档案阅读及管理页的资料更正 |

## API

模型维护 Agent、MCP 查询工具和故障恢复的接入说明见 [模型维护 Agent 第一版](docs/model-agent-v1.md)。

后续所有模型审核改由智能体完成，不设人工复核终点；来源优先级、自动补证据、预算与验收见 [自动审核 v3 方案](docs/autonomous-model-review.md)。当前仅完成方案及公开管理入口隐藏，完整自动审核闭环尚未接通，部署状态以功能文档为准。

| 端点 | 说明 |
|---|---|
| `GET /api/insights` | 情报列表及章节配图：day/category/q/page/page_size |
| `GET /api/insights/dates` | 已发布文章的可浏览日期，不含未处理资料日 |
| `GET /api/insights/{id}` | 文章段落、配图、延伸链接及底层事实与来源 |
| `GET /api/insights/status` | 整理数量、状态、可用日期与关注范围 |
| `POST /api/insights/refresh` | 整理待处理/失败条目 |
| `GET /api/articles`、`GET /api/articles/{id}` | 来源列表、全文搜索与正文 |
| `GET /api/models`、`GET /api/models/filters` | 模型库与筛选值 |
| `GET /api/models/search` | 按名称、机构和已确认别名检索候选：q/organization/limit/offset |
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
node --test frontend/tests/insights.test.cjs
```

覆盖正文安全清理、拆条幂等、引用校验、失败重试、事件合并、模型审核，以及任务租约与中断恢复、研究结果复用、自动复查、资料更正、闭源字段状态和多模型关联。公开表同步测试覆盖直接入库、重复同步、旧记录修订、同名版本、人工值保护、估算和付费字段、异常不污染数据、无 Key 同步，以及表内外模型的新报道维护。Agent 测试另覆盖事件融合、字段锁定、MCP 协议和服务恢复。浏览器验证默认使用 Chrome，Chrome 不可用时使用 Edge；只有明确要求才使用 Tabbit。
