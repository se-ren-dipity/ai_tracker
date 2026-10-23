# Ubuntu 24.04 服务器部署流程（直接使用 IP）

适用于 Ubuntu 24.04 LTS、可 SSH 登录且有 sudo 权限的服务器。以下以专门部署此项目的服务器为例；若已经有 Docker、Nginx 或其他网站，保留原有配置并检查端口占用。

部署结构：浏览器 → 服务器 IP 的 80 端口 → Nginx → 127.0.0.1:8000 → Docker 内的 FastAPI。前端由同一应用提供，不需要安装 Node.js、MySQL 或 GPU。

项目没有内置登录鉴权，内容管理 API 可以执行更新和审核。因此本流程用 Nginx 限制访问来源 IP，8000 端口只绑定服务器回环地址。IP 白名单不是加密；本方案使用 HTTP。如需通过公共网络保护传输内容，应另行配置 HTTPS 或使用 SSH 隧道。

## 1. 准备服务器信息与访问规则

准备服务器公网 IPv4、SSH 用户名、你的电脑当前公网出口 IPv4。文中的 `SERVER_PUBLIC_IP` 和 `YOUR_PUBLIC_IP` 必须分别替换；后者不是电脑的 192.168.x.x 内网地址。

在本机 PowerShell 查看当前公网 IPv4：

```powershell
curl.exe -4 https://api.ipify.org
```

云服务器安全组放行：

| 端口 | 来源 | 用途 |
|---|---|---|
| TCP 22（或实际 SSH 端口） | 你的公网 IP/32 | SSH、上传文件 |
| TCP 80 | 你的公网 IP/32 | 浏览网页 |
| TCP 8000 | 不放行 | 仅供本机 Nginx 访问 |

如果已启用 UFW，也添加对应的 22、80 规则；不要在未确认 SSH 端口规则前直接启用防火墙。Docker 发布的端口可能绕过 UFW，所以仍要保留后面的 `127.0.0.1:8000:8000` 绑定。[Docker 端口说明](https://docs.docker.com/engine/network/port-publishing/)

## 2. 在服务器安装 Docker Compose 和 Nginx

本机 PowerShell 登录（用户名按实际修改）：

```powershell
$ServerHost = "SERVER_PUBLIC_IP"
$SshUser = "ubuntu"
ssh "${SshUser}@${ServerHost}"
```

下面在服务器 Bash 中执行。已安装正常工作的 Docker Engine 和 Compose 插件时，跳过 Docker 安装部分。已有旧版 Docker 或 Podman 时，先按官方文档处理冲突包，不要直接删除正在被其他服务使用的容器运行时。

```bash
sudo apt update
sudo apt install -y ca-certificates curl git nginx sqlite3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker nginx
sudo docker version
sudo docker compose version
sudo docker run --rm hello-world
```

安装命令依据 [Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)。`hello-world` 成功后继续。拉取镜像失败时先检查服务器到 Docker 仓库的网络连接。

## 3. 从仓库获取代码，单独迁移数据库

代码仓库为 `https://code.iflytek.com/xdqian4/Information_gathering.git`，使用 `main` 分支。首次部署时在服务器执行：

```bash
git clone --branch main https://code.iflytek.com/xdqian4/Information_gathering.git ~/ai-tracker
cd ~/ai-tracker
git log -1 --oneline
mkdir -p data
printf '%s\n' '/compose.server.yml' '/compose.rollback.yml' '/backups/' '/manifest.json' >> .git/info/exclude
```

如果已经克隆，进入已有仓库执行 `git pull --ff-only origin main` 获取最新代码，后续统一使用该目录；不要再次克隆到非空目录。需要认证时使用代码平台支持的账号凭据或访问令牌，不要将令牌直接拼进仓库 URL。如果服务器无法访问公司代码平台，需要先具备相应网络访问条件。

代码通过 Git 获取，不必再上传代码压缩包。`.env` 和 `data/` 已被 Git 忽略，不会随仓库下载，仍要单独配置和迁移。

2026-10-23 准备的数据库迁移文件位于本机 `D:\模型收集\data\deploy-20261023`：

- `tracker.db`：使用 SQLite backup 接口生成的一致快照。
- `manifest.json`：生成时间、文件校验值和数据数量。

数据库保存了情报、原文、模型和任务状态。图片仍使用外部链接，没有单独的图片目录要迁移。

重新打开一个本机 PowerShell，执行：

```powershell
$ServerHost = "SERVER_PUBLIC_IP"
$SshUser = "ubuntu"
Set-Location 'D:\模型收集'
scp 'data/deploy-20261023/tracker.db' "${SshUser}@${ServerHost}:~/ai-tracker/data/tracker.migrated.db"
scp 'data/deploy-20261023/manifest.json' "${SshUser}@${ServerHost}:~/ai-tracker/"
```

若 SSH 使用其他端口，`ssh` 使用小写 `-p`，`scp` 使用大写 `-P`。

服务器上执行：

```bash
cd ~/ai-tracker
sha256sum data/tracker.migrated.db
cat manifest.json
test ! -e data/tracker.db && mv data/tracker.migrated.db data/tracker.db
sqlite3 -readonly data/tracker.db 'PRAGMA integrity_check;'
sqlite3 -readonly data/tracker.db 'SELECT COUNT(*) FROM insights; SELECT COUNT(*) FROM models;'
```

数据库校验值应与 manifest 的 `tracker.db` 对应，完整性检查应输出 `ok`；manifest 中另外记录的代码包不用于本次 Git 部署。如果 `data/tracker.db` 已存在，迁移命令不会覆盖它：先确认这是首次部署还是已有站点，再决定迁移，不能覆盖服务器上已经新增的数据。

不迁移旧数据时可以跳过数据库上传、迁移和检查；应用首次启动会创建新数据库。快照生成之后本机新增的数据不会自动同步到服务器。

## 4. 配置 API Key 与服务器 Compose 文件

服务器执行：

```bash
cd ~/ai-tracker
umask 077
cp -n .env.example .env
nano .env
chmod 600 .env
```

编辑为：

```dotenv
DEEPSEEK_API_KEY=填入你的真实Key
DB_PATH=data/tracker.db
```

不要把 Key 发到聊天、提交到 Git 或写进前端。没有 Key 也能浏览已经迁移的数据，但自动采集、分析和模型资料任务不会启动。

新建专用的 `compose.server.yml`，避免后续更新仓库里的开发配置时改变服务器端口：

```bash
cat > compose.server.yml <<'EOF'
services:
  backend:
    image: ai-tracker:current
    build: .
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:-}
      DB_PATH: data/tracker.db
    restart: unless-stopped
    logging:
      driver: local
EOF

sudo docker compose -f compose.server.yml config --quiet
sudo docker compose -f compose.server.yml up -d --build
sudo docker compose -f compose.server.yml ps
sudo docker compose -f compose.server.yml logs --tail=100 backend
```

后续命令都使用 `-f compose.server.yml`。不要直接运行仓库原来的 Compose 配置，否则其 `8000:8000` 会公开发布应用端口。

配置 Key 后启动会立即采集一次，并在每天北京时间 10:00 再采集；不依赖服务器本地时区。模型资料执行进程自动启动，不需要额外添加定时任务或另开 worker。只运行一个后端容器，保持一个 Uvicorn Web 进程。

## 5. 验证应用，然后配置 IP 访问

服务器执行：

```bash
curl -f http://127.0.0.1:8000/api/insights/dates
curl -f 'http://127.0.0.1:8000/api/insights?page_size=1'
curl -f http://127.0.0.1:8000/api/insights/status
```

应返回 JSON；迁移了数据库时应有原来的情报日期和内容。采集正在进行时状态中的 `running` 可以为 `true`。

编辑 Nginx 配置：

```bash
sudo nano /etc/nginx/sites-available/ai-tracker
```

填入以下内容并替换两个 IP 占位符。每个希望允许访问的公网 IP 增加一行 `allow`，最后保留 `deny all`。

```nginx
server {
    listen 80;
    server_name SERVER_PUBLIC_IP;

    allow YOUR_PUBLIC_IP;
    allow 127.0.0.1;
    deny all;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用并检查：

```bash
sudo ln -s /etc/nginx/sites-available/ai-tracker /etc/nginx/sites-enabled/ai-tracker
sudo nginx -t
sudo systemctl reload nginx
curl -f -H 'Host: SERVER_PUBLIC_IP' http://127.0.0.1/api/insights/dates
```

已有同名软链接时不要重复创建，编辑后直接检查并 reload。不要删除服务器上的其他站点配置。Nginx 的 IP 规则按顺序匹配，`allow` 放在 `deny all` 之前。[访问控制说明](https://nginx.org/en/docs/http/ngx_http_access_module.html)、[代理说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)

现在用本机 Chrome 打开 `http://SERVER_PUBLIC_IP`。这里不加 `:8000`，也不是 HTTPS。

## 6. 完成迁移验收

确认首页、历史日期、文章详情、模型库和内容管理都能打开，原来的记录还在；确认有配图的文章能显示预览。第三方图片仍由访问者的浏览器请求原地址，搬到服务器不会改变这部分网络路径。

服务器上检查执行进程日志：

```bash
cd ~/ai-tracker
sudo docker compose -f compose.server.yml logs --tail=100 backend
sudo tail -n 100 data/model-worker.log
```

填写了 Key 时应能看到调度器启动和模型资料任务相关日志；没填 Key 时不要求存在 worker 日志。

服务器确认正常后，停止本机项目的 Uvicorn 服务及它启动的 `backend.collector.model_worker` 进程，后续以服务器数据库为准。这可以避免两台机器分别采集、消耗 API 额度并形成两套不同的数据。不要停止其他项目的 Python 进程。

如果需要迁移本机快照之后新增的数据，必须先确定最终切换时间：停止本机该项目的写入，重新生成快照；服务器停止后再导入。不要用旧快照覆盖已经投入使用的服务器数据库。

## 7. 日常查看与重启

在服务器项目目录中执行：

```bash
cd ~/ai-tracker
sudo docker compose -f compose.server.yml ps
sudo docker compose -f compose.server.yml logs --tail=100 -f backend
```

`Ctrl+C` 只退出日志查看，不会停止容器。

```bash
# 临时停止
sudo docker compose -f compose.server.yml stop

# 重新启动
sudo docker compose -f compose.server.yml up -d

# 只重启当前容器
sudo docker compose -f compose.server.yml restart backend
```

修改 `.env` 后用 `up -d` 重建配置发生变化的容器；只执行 `restart` 不会刷新容器环境变量。修改代码后用 `up -d --build`。重启会再次触发启动采集，已收录 URL 会去重。

Docker 已启用开机启动，容器配置了 `unless-stopped`。曾经手动 `stop` 的容器需要用 `up -d` 恢复。

## 8. 数据备份

不要直接复制正在写入的数据库文件，用 SQLite 的一致备份接口。服务器上的命令：

```bash
cd ~/ai-tracker
umask 077
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
sqlite3 -readonly data/tracker.db ".backup 'backups/tracker-$stamp.db'"
sqlite3 -readonly "backups/tracker-$stamp.db" 'PRAGMA integrity_check;'
cp .env "backups/env-$stamp"
```

备份目录包含 Key，保持私有。定期把备份另外保存到自己的电脑或备份存储，避免服务器磁盘损坏时连同备份一起丢失。日志不是恢复内容的必要数据。

本机需要重新生成数据库快照时，用 Python 的 `sqlite3.Connection.backup()`；它支持源数据库同时有其他连接访问。[Python SQLite 备份说明](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)

## 9. 后续更新与回退

后续在本机完成代码修改、验证、提交和推送，再到服务器拉取。不再上传最初的数据库。

服务器更新前确认工作区干净，再保存原镜像、提交号与数据库。每一步成功后再继续；如果拉取或构建失败，先按下方方法恢复服务，不要重置或丢弃未确认的服务器改动：

```bash
cd ~/ai-tracker
git status --short
git fetch origin main
sudo docker image tag ai-tracker:current ai-tracker:rollback
sudo docker compose -f compose.server.yml stop
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
git rev-parse HEAD > "backups/revision-before-update-$stamp.txt"
sqlite3 -readonly data/tracker.db ".backup 'backups/tracker-before-update-$stamp.db'"
git pull --ff-only origin main
sudo docker compose -f compose.server.yml up -d --build
curl -f http://127.0.0.1:8000/api/insights/dates
```

保留 `data/`、`.env`、`compose.server.yml` 和 Nginx 配置，不要用本机旧库覆盖它们。

构建或启动失败时可先恢复原镜像服务：

```bash
cat > compose.rollback.yml <<'EOF'
services:
  backend:
    image: ai-tracker:rollback
EOF
sudo docker compose -f compose.server.yml -f compose.rollback.yml up -d --no-build
```

如新版本已改变数据库且不兼容旧程序，先停止容器、备份当前 `data/`，再恢复与旧镜像对应的更新前数据库；恢复会丢失备份之后的变更。以下备份文件名必须替换成刚才实际生成的文件名：

```bash
sudo docker compose -f compose.server.yml stop
sudo cp -a data "backups/data-before-restore-$(date +%Y%m%d-%H%M%S)"
sudo sqlite3 data/tracker.db ".restore 'backups/tracker-before-update-实际时间.db'"
sudo docker compose -f compose.server.yml -f compose.rollback.yml up -d --no-build
```

## 10. 常见问题

| 现象 | 检查 |
|---|---|
| 403 Forbidden | Nginx 中 `allow` 是否是当前电脑公网出口 IP；更换网络/VPN 后可能变化，需要同步修改安全组与 Nginx |
| 请求超时 | 云安全组、UFW、80 端口和访问 IP；确认 Nginx 正在运行 |
| 显示 Nginx 欢迎页 | `server_name` 是否填写实际服务器 IP，站点是否启用且已 reload |
| 502 Bad Gateway | 容器是否启动；先在服务器访问 `127.0.0.1:8000` |
| 页面有模型但没有旧情报 | 首次启动前是否把快照放入 `~/ai-tracker/data/tracker.db`；是否在错误目录创建了新库 |
| 不采集或分析失败 | Key 是否填入并重新 `up -d`；检查额度和服务器到 DeepSeek、橘鸦、AIHOT 的网络连接 |
| 个别图片仍没有 | 原文无配图，或访问者网络无法加载外链；首页会尝试同一情报最多 3 张已有图片 |
| Docker 构建失败 | 查看拉镜像和 pip 安装的具体错误；当前依赖未锁定版本，新依赖兼容性也可能影响构建 |
| 数据库锁冲突 | 是否额外启动了第二个 Web 服务、手工 worker 或并行离线整理命令 |

本流程已依据项目代码与官方文档核对。本机未安装 Docker，未执行服务器镜像构建或远程部署；服务器上完成构建、接口访问和浏览器验收后才算部署成功。
