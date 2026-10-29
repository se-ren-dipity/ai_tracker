# Ubuntu 服务器部署与维护

适用范围：一台 Linux 服务器、Docker Engine 与 Compose v2、一个管理员。前端和 API 使用同一个域名，SQLite 放在服务器本地磁盘。现在使用仓库内的 `docker-compose.yml`，不要继续使用旧版只启动 backend 的 `compose.server.yml`。

## 运行结构

| 服务 | 职责 | 状态保存 |
|---|---|---|
| backend | 网页、管理员登录、查询、任务入队 | 管理会话、来源开关、操作记录 |
| collector | 每天北京时间 10 点采集，随后分析、编排、发现模型 | collection_runs 及阶段检查点 |
| model-worker | 管理员手动联网补查与资料更正；默认关闭旧模型周期研究 | model_tasks、研究检查点和租约 |

三个服务共享 `./data:/app/data`，各运行一个进程。网页重启不会启动采集；采集任务每天只有一个唯一标识。每天 10 点后启动时补建当天漏掉的任务，10 点前启动则等待或手动触发。历史停机天数不会产生虚构日报，只能补收源站当前 RSS 窗口内仍可获取的资料。

各来源独立处理，成功来源在同一任务重试时跳过。连接失败最多请求 3 次；来源或整理步骤仍失败，则任务间隔 30 分钟再尝试，最多 3 轮，随后显示失败，等待次日新任务或管理员重试。缓存可保留资料，但不算本次同步成功。分析、编排、模型发现按数据状态继续，已完成内容不重复支付分析费用；请求已返回但尚未提交数据库时断电，重试可能再次请求，不能保证外部 API 只调用一次。

模型任务保留独立的 90 秒租约和研究检查点，最多自动尝试 3 次，单项最长 15 分钟。进程文件锁阻止同机启动第二个相同 worker，进程退出后锁由操作系统释放。数据库写事务中不执行网络请求；写锁等待上限 30 秒。不要使用网络文件系统、多机共享 SQLite 或并行运行离线写库脚本。

## 1. 准备代码与 Docker

已安装可用的 Docker 和 Compose 时跳过安装；否则按 [Docker Ubuntu 官方步骤](https://docs.docker.com/engine/install/ubuntu/) 安装，并启用开机启动：

```bash
sudo systemctl enable --now docker
sudo docker compose version
```

首次获取代码：

```bash
git clone --branch main https://github.com/se-ren-dipity/ai_tracker.git ~/ai-tracker
cd ~/ai-tracker
mkdir -p data
umask 077
cp -n .env.example .env
chmod 600 .env
```

已有仓库时使用原目录，检查本地修改后再 `git pull --ff-only origin main`。代码变更需要先提交并推送才会出现在服务器；`.env` 和 `data/` 不在 Git 中。

服务器若此前从讯飞代码仓库克隆，先在项目目录运行 `git remote set-url origin https://github.com/se-ren-dipity/ai_tracker.git`，再拉取 `main`。本机保留原 `origin`，另设 `github` 远端用于发布；新服务器从 GitHub 克隆后，`origin` 即为 GitHub。

## 2. 配置与管理员密码

编辑 `.env`。默认宿主端口为 29011；想保留旧 Nginx 的 8000 转发，可设置 `WEB_PORT=8000`。

```dotenv
DEEPSEEK_API_KEY=你的Key
COLLECTOR_PROXY=
BIND_ADDRESS=127.0.0.1
WEB_PORT=29011
ADMIN_PASSWORD_HASH=
ADMIN_COOKIE_SECURE=true
MODEL_AUTO_REVIEW_ENABLED=false
```

没有 DeepSeek Key 仍会采集 RSS 原文与 Models Table，分析显示等待配置，模型进程保持等待。来源开关在管理员页面修改，保存在数据库；新增 RSS 地址仍编辑 `backend/collector/sources.yaml` 后构建部署。API Key 与代理地址不通过网页编辑或返回。

构建并设置密码（密码至少 12 个字符，终端不回显）：

```bash
sudo docker compose build
sudo docker compose run --rm --no-deps backend python -m backend.admin_auth --print-hash
```

将最后输出的 `ADMIN_PASSWORD_HASH=...` 整行填入 `.env`。没有配置密码时管理接口默认拒绝访问，不提供默认账号密码。只有一位管理员，不需要用户名。登录会话有效期 12 小时，HttpOnly、SameSite=Strict，修改密码并重建网页容器后旧会话失效；连续 5 次密码错误会对该客户端地址限流 15 分钟。

默认 Secure Cookie 用于 HTTPS。仅在本机 HTTP 或下方 SSH 隧道方式下将 `ADMIN_COOKIE_SECURE=false`。不应在公共 HTTP 链路输入管理员密码。阅读资讯和模型无需登录；模型审核、来源切换、采集及重试接口都验证会话和 CSRF。

## 3. 可选：迁移本机数据

最终切换前停止本机的网页、采集 worker 和模型 worker，使用当前版本生成新的快照，不使用之前的过期快照：

```powershell
Set-Location 'D:\模型收集'
.\stop-local.ps1
.\.venv\Scripts\python.exe -m backend.backup data/tracker.migrated.db
Get-FileHash data/tracker.migrated.db -Algorithm SHA256
scp data/tracker.migrated.db ubuntu@SERVER_PUBLIC_IP:~/ai-tracker/data/
scp -r data/source-cache ubuntu@SERVER_PUBLIC_IP:~/ai-tracker/data/
```

快照包含原文、模型、任务、来源设置和管理记录；图片仍引用外部地址。缓存可选，但迁移缓存能让上游暂时不可达时继续提供最近成功的资料。服务器停止所有服务后、仅在目标库不存在时安装初始快照：

```bash
cd ~/ai-tracker
sha256sum data/tracker.migrated.db
test ! -e data/tracker.db && mv data/tracker.migrated.db data/tracker.db
```

已有服务器数据时先备份，不能直接覆盖。两台机器之间没有数据库自动合并机制，切换后以服务器为准，不要让本地副本持续采集。

2026-10-29 已整理的本机初始化快照位于 `data/releases/20261029/tracker.migrated.db`，校验和同目录保存。它保留新闻、模型、证据、任务历史和管理配置，已移除本地登录会话、访客问答/计量和 worker 心跳；完整回滚备份仍在 `data/backups/`。此快照用于新库初始化，不能直接覆盖有独有内容的服务器数据库。完整核对结果见[发布前整理](release-readiness-2026-10-29.md)。

初始化或更新后可运行只读检查：`sudo docker compose exec -T backend python -m backend.maintenance`。检查结构、声明及软关联、原文拆条和每条新闻事实的原文引用；不会联网或修改数据，不代表上游互联网已被穷尽采集。

## 4. 启动与访问

```bash
sudo docker compose config --quiet
sudo docker compose up -d
sudo docker compose ps
curl --fail http://127.0.0.1:29011/health/ready
sudo docker compose logs --tail=100 backend collector model-worker
```

Compose 等待网页健康检查通过后启动两个 worker。三个服务有独立的健康检查和 `restart: unless-stopped`。日志各保留 3 个 10 MB 文件；不再通过容器内 `model-worker.log` 判断运行状态，改用 `docker compose logs model-worker`。

**没有域名时推荐 SSH 隧道**：安全组只需开放你的出口 IP 到 SSH 端口，不开放 29011。设置 `ADMIN_COOKIE_SECURE=false` 并重建网页容器，然后在本机运行：

```powershell
ssh -N -L 29011:127.0.0.1:29011 ubuntu@SERVER_PUBLIC_IP
```

用 Chrome 打开 `http://localhost:29011/#manage`。保持 SSH 窗口运行即可。通过 SSH 加密传输，服务器端口仍只绑定回环地址。

**已有域名与 HTTPS**：保留 `ADMIN_COOKIE_SECURE=true`，让现有 Nginx HTTPS 站点转发至回环端口。以下是站点内的 location 配置，TLS 证书、域名和可选 IP 白名单沿用服务器配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:29011;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
}
```

不要为方便配置将容器端口绑定公网。若前面有代理，登录限流可能按代理地址合并；只在确认代理 IP 后配置 Uvicorn 的可信 forwarded IP，不信任任意客户端传入的地址。

## 5. 验收与故障处理

登录页脚“管理员入口”后确认：

- 两个后台进程在线，最近心跳更新时间正常；模型进程无 Key 时显示等待 API 配置。
- 来源清单包含 Models Table、橘鸦、两路 AIHOT、机器之心和新智元。
- 每个来源显示最近成功时间，使用缓存和最新成功不会混淆。
- 点击一次采集后出现任务记录和阶段状态；重复点击复用尚未结束的任务。
- 模型审核保留身份依据、字段更正建议、人工确认/丢弃与逐项重新补查。
- 容器重启后任务 ID、完成步骤和来源开关仍在，网页服务重启不产生新的每日任务。

```bash
sudo docker compose logs --tail=200 collector
sudo docker compose logs --tail=200 model-worker
sudo docker compose restart collector
sudo docker compose ps
```

Docker 的健康检查只标记 unhealthy，**不会单凭 unhealthy 自动重启**；进程退出才按重启策略恢复。心跳长期不更新或磁盘满时需要查看日志并处理。当前没有外部告警系统；无人值守期间的异常会保留供后台查看，如需主动通知可后续接服务器监控。

| 现象 | 检查 |
|---|---|
| Models Table / RSS 使用缓存 | 服务器到该域名的 DNS、TLS、网络或代理；不要把缓存时间当本次更新时间 |
| DeepSeek 401 / 403 | Key、权限和实际接口配置；更新环境后重建服务，再从后台重试 |
| 登录后仍跳回登录 | HTTP 下误用了 Secure Cookie，或代理 Host 不一致 |
| worker 离线 | 对应容器退出/健康状态、磁盘、数据库权限、重复 worker 文件锁 |
| 任务显示失败 | 展开任务阶段，修复后点击重试；不必清空数据库 |
| SQLite 锁冲突 | 是否运行了额外离线写库脚本、第二套项目或使用网络磁盘 |

## 6. 备份与更新

在线一致备份（输出实际文件名并执行完整性检查）：

```bash
sudo docker compose exec -T backend python -m backend.backup
```

备份在 `data/backups/`，定期复制到另一台机器或备份存储。数据库包含管理会话，备份应保持私有。`.env` 单独备份并保护权限，缓存可另行归档。此命令没有自动删除策略；检查磁盘容量。按自己的服务器运维方式安排每日备份，例如 cron 每天 03:30 调用上述命令并检查退出码。

更新前保存当前代码版本与镜像，并备份数据库：

```bash
sudo docker compose exec -T backend python -m backend.backup
git rev-parse HEAD > data/previous-code-version.txt
sudo docker image tag ai-tracker:current ai-tracker:previous
git pull --ff-only origin main
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
```

修改 `.env` 使用 `up -d` 重建环境变化的容器；`restart` 不会加载新的环境变量。修改代码后重新 build。直接依赖固定为本机测试通过的版本；Docker 使用 `backend/requirements.lock` 固定 Linux/Python 3.11 的传递依赖。锁文件已完成解析，实际 Linux 镜像构建仍需在目标服务器验证。需要升级依赖时先修改 requirements.txt，再运行 `uv pip compile backend/requirements.txt --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --output-file backend/requirements.lock`，运行测试并在服务器验证新镜像。

2026-10-29 的后台思考更新在代码中统一启用 `thinking=enabled`、`reasoning_effort=high`，无需增加 `.env` 配置。按上述步骤拉取后执行 `sudo docker compose up -d --build`，让三个服务载入更新；无需数据库迁移，不自动重跑已完成新闻或全库补查。处理时间与输出 token 可能增加，范围、上限及本机验证见[后台思考模式](model-agent-v1.md#后台思考模式)。服务器是否生效仍需在部署后单独验收。

回退前停止全部服务并保存故障现场。恢复与旧代码版本匹配的数据库备份，保留备份之后新增的数据副本；恢复会丢失备份后的变化。为三个服务都覆盖镜像：

```yaml
# compose.rollback.yml，放在仓库外或加入本地 Git exclude
services:
  backend:
    image: ai-tracker:previous
  collector:
    image: ai-tracker:previous
  model-worker:
    image: ai-tracker:previous
```

```bash
sudo docker compose stop
# 在此保存当前 data 并恢复已确认的数据库备份；不要边运行边覆盖数据库。
sudo docker compose -f docker-compose.yml -f compose.rollback.yml up -d --no-build
```

首次从旧单容器版本升级时，旧镜像仍会自己启动 scheduler 和 worker，不能配合新三服务配置运行。回退到该版本必须使用保存的旧 Compose 配置，只启动旧 backend。新版本之后的回退使用上述三服务方案。

## 维护边界

网站助手“囤囤”已增加访客会话、使用额度和管理开关。升级时自动创建四张助手表，先备份再 build/up；同一数据库只运行一个 Web 进程，进程锁会拒绝多 worker。代理为 `/api/assistant/` 关闭 SSE 缓冲并设置 120 秒读超时，HTTPS 继续启用 Secure Cookie。具体默认值、数据保留和尚未完成的服务器验证见 [囤囤第一版](pet-agent-v1.md)。

模型维护 Agent 保留现有三个服务，MCP 作为可选只读服务运行。升级、令牌配置及接入方式见 [第一版使用说明](model-agent-v1.md)。

调度、状态转换、去重和重试由确定性代码负责，LLM 负责有证据的抽取、编排和判断。先用现有结构保障可观察、可恢复和可审查，不引入 Pi/Redis/Celery 等额外依赖。将来有多机吞吐或复杂审批需求，再迁移任务存储与执行框架。自动判断仍可能出错，管理员审核是校正入口，不能以框架替代数据校验。
