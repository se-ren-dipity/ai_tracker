# Ubuntu 服务器部署与维护

适用范围：一台 Linux 服务器、一个管理员。前端和 API 使用同一个域名，SQLite 放在服务器本地磁盘。Ubuntu 常规部署使用仓库内的 `docker-compose.yml` 与 Compose v2；当前 BCLinux 服务器按用户要求改用下方直接 Docker 命令。不要继续使用旧版只启动 backend 的 `compose.server.yml`。

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

### 当前 BCLinux 服务器的兼容安装（2026-10-29）

已确认当前服务器为 BCLinux For Euler 22.10、ARM64（aarch64），项目目录为 `/iflytek/xdqian4/ai_tracker`，Docker CLI 和 daemon 均为 18.09.0；应用启动仍待服务器验收。该环境不要照搬 Ubuntu 安装命令。服务器直连 GitHub Release 下载出现 `curl: (52) Empty reply from server`。

当前采用 Compose v2.24.7 ARM64 独立程序，使用 `docker-compose` 命令。它内置的 Docker 客户端支持 API 版本协商；对这台服务器的实际兼容性仍以配置检查、连接检查和应用启动结果为准。独立安装方式见 [Docker 官方说明](https://docs.docker.com/compose/install/standalone/)。

本机安装包已通过发布方提供的 SHA256 校验，位于忽略目录 `data/tools/docker-compose-v2.24.7/`；同目录 `install-on-server.ps1` 先校验本地文件，再经 SCP 上传、SSH 校验并安装至 `/usr/local/bin/docker-compose`。已有同名程序会先备份。脚本最后执行版本查询、项目 `config --quiet` 和 `ps`，不启动应用；上传和安装分别在终端提示输入服务器密码。

验证状态：本机和服务器安装包校验通过；服务器已安装 v2.24.7，版本查询、项目 `config --quiet` 和连接 Docker 的 `ps` 检查通过。该环境使用带连字符的 `docker-compose`；旧 Docker CLI 执行 `docker compose up -d --build` 会报 `unknown shorthand flag: 'd' in -d`。应用构建随后遇到下方 ARM64 镜像问题，尚未完成启动。本次兼容安装未升级或重启 Docker daemon。

### ARM64 基础镜像修复（2026-10-29）

服务器构建在首个 `RUN pip install` 报 `exec format error`，`docker-compose ps` 为空。已查询原华为镜像源的 manifest 和 config：`swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.11-slim` 实际为 `linux/amd64`，config ID `10f461201cdb...` 与服务器构建日志一致，无法在该 ARM64 主机直接执行。

仓库 Dockerfile 改为默认 `python:3.11-slim`，并通过 `PYTHON_IMAGE` 构建参数支持已校验的本地镜像。该服务器的旧 Docker 对新 OCI manifest 存在兼容限制，因此准备 Docker load 格式的 ARM64 离线包。来源为 DaoCloud 镜像代理的 `library/python`，ARM64 manifest 固定为 `sha256:8f867a1f16776618234a2d0ef41fbb991d0bd0b142c8a95bc37456d8e47104cf`；制作时验证 config 架构、压缩层 SHA256 和解压后的 rootfs diff ID。包和脚本放在忽略目录 `data/tools/python-arm64-20261029/`。

本机 `repair-server.ps1` 经 SCP 上传镜像与修复脚本，服务器脚本校验并导入为 `ai-tracker-python:3.11-arm64`，先运行 Python 验证架构，再备份并仅替换原 Dockerfile 的基础镜像声明，构建启动项目并等待三个服务健康。本机已完成四层镜像校验，确认 shell 与 Python 的 ELF 架构均为 AArch64；PowerShell/Bash 语法和 Dockerfile 替换结果检查通过。服务器已完成镜像导入并运行 Python 3.11.18，原架构错误已解决；后续应用构建与启动结果见下方直接 Docker 部署章节。

### pip 创建线程失败（2026-10-29）

ARM64 修复后的服务器构建进入依赖下载，在 pip 24.0 的 rich 进度条启动线程时报 `RuntimeError: can't start new thread`。仓库 Dockerfile 已加 `--progress-bar off`，关闭构建进度条的后台线程；这只能避开该进度条路径，不能证明应用线程可用。服务器诊断已确认 Docker client/server 均为 18.09.0、镜像 glibc 2.41、进程限额 1048576、容器 pids.current=1 且 pids.max=max；无效参数的 clone3 返回 EPERM，实际线程创建失败。结果指向旧 Docker seccomp 与新 glibc 的兼容问题，参考 [Docker 官方镜像维护者说明](https://github.com/docker-library/official-images/issues/16829)。

本机 `data/tools/python-arm64-20261029/resume-server.ps1` 只建立一次 SSH，复用服务器已有镜像，无须再次上传镜像。脚本先在默认容器限制下检查 glibc、进程限额、无效参数的 clone3 返回值和实际线程创建；服务器执行时线程检查失败，已按设计停止，未再次构建或启动应用。它未更改 Docker daemon 或容器安全配置。

成功导入、修复 Dockerfile 且线程检查通过后，该服务器后续更新使用：

```bash
DOCKER_BUILDKIT=0 docker-compose build --build-arg PYTHON_IMAGE=ai-tracker-python:3.11-arm64
docker-compose up -d --no-build --wait --wait-timeout 180
docker-compose ps
```

服务器 Dockerfile 的改动有时间戳备份；后续拉取代码前先检查此处本地修改，避免覆盖服务器其他改动。

### 当前服务器直接使用 Docker（用户选定，2026-10-30 更新）

以下临时启动方式不调用 Compose。假设项目 `.env` 和迁移后的 `data/tracker.db` 已就位，并复用已导入的 ARM64 基础镜像。先验证线程，再关闭构建时的 pip 进度条；网页健康后启动两个 worker。`.env` 以只读文件挂载，由应用的 python-dotenv 解析，保留带引号的管理员密码哈希等值；数据库仍挂载原 data 目录。用户于 2026-10-30 明确要求从自己电脑通过服务器 IP 直接访问，因此当前服务器发布地址改为 `0.0.0.0:29011`，浏览器访问 `http://120.222.7.246:29011`；容器内部仍使用 8000，不占用宿主的 8000 端口。

当前 IP 直连使用 HTTP，网页容器通过 `-e ADMIN_COOKIE_SECURE=false` 覆盖 `.env` 的默认 true，确保浏览器能在 HTTP 下使用管理员及助手 Cookie；该覆盖只作用于网页容器。HTTP 不加密登录凭据及会话，云安全组应将 TCP 29011 的来源限制为用户电脑出口 IP；后续使用 HTTPS 时恢复 true。发布端口会开放宿主网络入口，见 [Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)。命令不关闭防火墙、不修改安全组，也不更改 Docker daemon；其他环境的 Compose 仍默认回环绑定。

用户要求避免干扰同机其他任务。命令先检查三个容器名和 29011 监听端口，冲突时退出，不删除或停止已有服务，不修改或重启 Docker daemon。建议初始资源上限：每个业务容器 0.5 CPU、768 MiB 内存、128 个进程/线程，内存与 swap 总额度同为 768 MiB；构建 RUN 步骤限 1 CPU、1 GiB 内存。三个业务容器内存上限合计 2.25 GiB，不含 Docker daemon 等宿主开销；日志使用 json-file，每容器最多 3 个 10 MB 文件。限制依赖宿主内核支持；磁盘、网络和宿主资源仍共享，不能保证对其他任务零影响。参数依据 [Docker 资源限制说明](https://docs.docker.com/engine/containers/resource_constraints/) 和 [Docker 18.09 构建参数](https://github.com/docker/cli/blob/v18.09.0/cli/command/image/build.go)。

临时参数 `--security-opt seccomp=unconfined` 会关闭这些容器的 seccomp 系统调用过滤，降低隔离，仅作为旧 Docker 的兼容措施；长期运行应升级 Docker/runc/libseccomp，验证默认配置下可创建线程后移除此参数。本次没有修改 daemon 配置，也没有升级或重启服务器 Docker。Docker 18.09 的 Linux 构建接口不支持 build security options，不能把该参数直接加到 `docker build`。

2026-10-30 服务器反馈已成功构建 `ai-tracker:current`（镜像 ID `ad5644425a39`），但首个网页容器因 `exec: "docker-init": executable file not found in $PATH` 启动失败。`--init` 依赖 Docker daemon 路径内的 docker-init，见 [Docker 官方说明](https://docs.docker.com/reference/cli/docker/container/run/#specify-an-init-process)。当前服务器命令改为显式 `--init=false`；三个服务直接运行 Python/Uvicorn，不需要额外子进程管理器。设置 `--stop-signal SIGINT`，让使用 `asyncio.run` 的 worker 在作为 PID 1 时也通过 Python 已注册的中断处理执行取消和清理，保留 30 秒停止超时。其他机器的 Compose 配置保持原样。

在已登录服务器的 Bash 中执行；任一步失败即停止，不继续启动旧镜像，也不删除任何已有容器：

```bash
(
set -e
cd /iflytek/xdqian4/ai_tracker
test -f .env && test -f data/tracker.db || { echo 'Missing .env or data/tracker.db'; exit 1; }

existing=$(docker ps -a --format '{{.Names}}')
for name in ai-tracker-web ai-tracker-collector ai-tracker-models; do
  if printf '%s\n' "$existing" | grep -Fxq "$name"; then
    echo "Container already exists; stopped without changing it: $name"; exit 1
  fi
done
listeners=$(ss -H -ltn 'sport = :29011')
[ -z "$listeners" ] || { echo 'Port 29011 is in use; no process was stopped.'; exit 1; }

docker run --rm --network none --security-opt seccomp=unconfined \
  --cpus 0.5 --memory 128m --memory-swap 128m --pids-limit 32 \
  ai-tracker-python:3.11-arm64 python -c \
  'import threading; t=threading.Thread(target=lambda: None); t.start(); t.join(); print("Thread OK")'

cp -p Dockerfile "Dockerfile.before-direct-$(date +%Y%m%d-%H%M%S)"
sed -i 's|^RUN pip install --no-cache-dir -r requirements\.lock$|RUN pip install --no-cache-dir --progress-bar off -r requirements.lock|' Dockerfile
DOCKER_BUILDKIT=0 docker build \
  --cpu-period 100000 --cpu-quota 100000 --memory 1g --memory-swap 1g \
  --build-arg PYTHON_IMAGE=ai-tracker-python:3.11-arm64 -t ai-tracker:current .

common=(--init=false --restart unless-stopped --stop-signal SIGINT --stop-timeout 30
  --cpus 0.5 --memory 768m --memory-swap 768m --pids-limit 128
  --security-opt seccomp=unconfined
  --mount "type=bind,src=$PWD/data,dst=/app/data"
  --mount "type=bind,src=$PWD/.env,dst=/app/.env,readonly"
  -e DB_PATH=/app/data/tracker.db
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3
  --health-interval 30s --health-timeout 10s --health-retries 3)

docker run -d --name ai-tracker-web "${common[@]}" \
  -p 0.0.0.0:29011:8000 -e ADMIN_COOKIE_SECURE=false --health-start-period 60s \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)\"" \
  ai-tracker:current

for attempt in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' ai-tracker-web)" = healthy ] && break
  sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' ai-tracker-web)" = healthy ] || { docker logs --tail 80 ai-tracker-web; exit 1; }

docker run -d --name ai-tracker-collector "${common[@]}" \
  --health-start-period 30s --health-cmd 'python -m backend.runtime collector' \
  ai-tracker:current python -m backend.collector.collection_worker
docker run -d --name ai-tracker-models "${common[@]}" \
  --health-start-period 30s --health-cmd 'python -m backend.runtime models' \
  ai-tracker:current python -m backend.collector.model_worker

docker ps --filter name=ai-tracker --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
)
```

两个 worker 初始可能显示 `health: starting`，需后续确认三个容器均为 healthy。停止本项目的直接 Docker 容器使用 `docker stop -t 30 ai-tracker-models ai-tracker-collector ai-tracker-web`；再次启动已有容器使用 `docker start ai-tracker-web ai-tracker-collector ai-tracker-models`，不要重复 `docker run` 创建同名容器。当前命令方案待服务器实际执行与健康验收。

针对这次 docker-init 失败，镜像无需重建。先核对并删除日志中创建失败的容器，再执行上面从 `common=(--init=false ...` 开始的启动与健康检查部分。删除不使用 `-f` 或 `-v`，不删除宿主挂载的 `.env` 或数据库：

```bash
(
set -e
failed=c2e26d6e0c2237c3f4fccece0b9f8a918502467962b1408197c99d0abe33acad
[ "$(docker inspect -f '{{.Name}} {{.State.Running}}' "$failed")" = '/ai-tracker-web false' ] || {
  echo 'Container identity/state changed; no container was removed.'; exit 1;
}
docker rm "$failed"
)
```

### 已有网页容器改为服务器 IP 直连

在服务器项目目录执行下方命令，复用现有网页容器的镜像，只停止并重建 `ai-tracker-web`。两个后台 worker 和其他服务不操作，原 `.env` 与数据库继续使用宿主挂载。修改端口映射需要重建容器，`docker restart` 不会更新映射。

```bash
(
set -e
cd /iflytek/xdqian4/ai_tracker
test -f .env && test -f data/tracker.db
web_image=$(docker inspect -f '{{.Image}}' ai-tracker-web)

docker stop -t 30 ai-tracker-web
docker rm ai-tracker-web

docker run -d --name ai-tracker-web \
  --init=false --restart unless-stopped --stop-signal SIGINT --stop-timeout 30 \
  --cpus 0.5 --memory 768m --memory-swap 768m --pids-limit 128 \
  --security-opt seccomp=unconfined \
  --mount "type=bind,src=$PWD/data,dst=/app/data" \
  --mount "type=bind,src=$PWD/.env,dst=/app/.env,readonly" \
  -e DB_PATH=/app/data/tracker.db -e ADMIN_COOKIE_SECURE=false \
  -p 0.0.0.0:29011:8000 \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  --health-interval 30s --health-timeout 10s --health-retries 3 \
  --health-start-period 60s \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)\"" \
  "$web_image"

for attempt in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' ai-tracker-web)" = healthy ] && break
  sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' ai-tracker-web)" = healthy ] || {
  docker logs --tail 80 ai-tracker-web; exit 1;
}
docker port ai-tracker-web
)
```

端口输出应为 `8000/tcp -> 0.0.0.0:29011`。若网页容器健康但电脑仍不能访问，检查云安全组、主机防火墙及公网路由是否允许来自该电脑的 TCP 29011；不要停用整机防火墙。健康检查 URL 的 127.0.0.1 是容器自检地址，与宿主对外监听地址不同。

2026-10-30 迭代：增加同名容器、端口冲突检查及构建/运行资源限制；根据 docker-init 报错关闭项目容器 init 并指定 SIGINT 停止信号；根据用户明确要求将当前服务器网页改为 IP 直连，HTTP Cookie 设置仅覆盖网页容器。本机完成 Bash 语法、命令范围及 Python 入口检查；服务器镜像构建已成功，三容器启动、健康、停止和电脑访问仍待验证。

### 获取代码

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

默认 Secure Cookie 用于 HTTPS。本机 HTTP、下方 SSH 隧道或用户选择的 HTTP IP 直连需要 `ADMIN_COOKIE_SECURE=false`；当前服务器直连命令仅在网页容器环境中覆盖该值。HTTP 不加密登录凭据和会话，直连时限制安全组来源，后续配置 HTTPS 并恢复 true。阅读资讯和模型无需登录；模型审核、来源切换、采集及重试接口都验证会话和 CSRF。

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

快照包含原文、模型、任务、来源设置和管理记录；原始图片地址保留在库中，网页通过[服务器图片缓存](news-image-cache.md)加载。`data/image-cache/` 与来源缓存可以随 data 迁移，也可在服务器首次查看时重新下载；已有图片缓存可在源站暂时不可达时继续提供图片。服务器停止所有服务后、仅在目标库不存在时安装初始快照：

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

### 当前 ARM64 服务器：新闻图片缓存更新（2026-10-30）

本次不改数据库结构，也不重采历史文章。先保存服务器此前手工修改的 Dockerfile，再拉取 main；仓库 Dockerfile 已包含 ARM64 构建参数和 pip 进度条修复，不要把旧 Dockerfile stash 再覆盖回来。`.env`、数据库和图片缓存均不在 Git 中。

```bash
(
set -e
cd /iflytek/xdqian4/ai_tracker
git stash push -m 'server-dockerfile-before-image-cache' -- Dockerfile
git -c http.version=HTTP/1.1 pull --ff-only https://github.com/se-ren-dipity/ai_tracker.git main
bash scripts/update-server-web.sh
)
```

`scripts/update-server-web.sh` 是此服务器更新步骤的唯一执行脚本：核对原网页容器的数据挂载，复用已导入 ARM64 基础镜像，限资源构建成功后执行 SQLite 在线备份，只停止并重建网页容器；旧网页容器改名保留，启动或健康检查失败时自动恢复旧网页容器。两个后台 worker 不重启，不清理 Docker 全局资源，不修改防火墙、daemon 或其他任务。旧网页容器保留为停止状态；未自动删除备份或旧容器。

网页保持 `0.0.0.0:29011`、HTTP Cookie 覆盖、`--init=false` 及原有资源限制/线程兼容参数。更新后用电脑刷新 `http://120.222.7.246:29011`，首次查看的图片可能稍慢，成功文件写入 `data/image-cache/`；之后源站不可用也能读缓存。浏览器图片地址应变为 `/api/articles/.../images/...`。功能与边界见[图片缓存说明](news-image-cache.md)。2026-10-30 已完成服务器健康检查、3 张本站图片响应抽查和 Edge 首页显示验收；新镜像为 `ee302d7af369`。

### GitHub 拉取失败时：离线增量更新（2026-10-30）

本次更新前服务器停在 `58b6b39`，网页容器内 `assets.juya.uk` 解析为公网地址；`git pull` 返回 `Empty reply from server`，尚未执行网页更新脚本。此时使用 Git bundle 传输已提交的代码增量，保留 Git 历史；不打包工作区中的 `.env`、数据库或图片缓存。Git bundle 的使用方式见 [Git 官方文档](https://git-scm.com/docs/git-bundle)。

本机从已确认的服务器版本生成 `ai-tracker-image-cache.bundle`，传到服务器已有项目的 `data/` 目录后执行：

```bash
(
set -e
cd /iflytek/xdqian4/ai_tracker
git bundle verify data/ai-tracker-image-cache.bundle
git fetch data/ai-tracker-image-cache.bundle refs/heads/main
git merge --ff-only FETCH_HEAD
bash scripts/update-server-web.sh
)
```

服务器此前的 Dockerfile 修改已经 stash，无需再执行 stash，也不要覆盖恢复。bundle 的前置提交校验或快进合并失败即停止，不强制覆盖本地改动。代码传输不依赖 GitHub；Docker 构建如需安装依赖仍需要可用的 Python 包源。本机已验证 26828 字节的离线包可从 `58b6b39` 快进至 `883270e`；用户随后完成服务器构建与健康检查，线上真实图片响应也已抽查通过。此包是本次发布的固定增量，未来更新应重新生成对应版本的包。

### 常规 Compose 部署

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
