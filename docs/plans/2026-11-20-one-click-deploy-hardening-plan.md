# scCloud 一键部署脚本加固与测试计划

- **日期**：2026-11-20
- **状态**：已实施，待 CI 验收（Implemented / Pending CI）
- **基线提交**：`main` @ `b154e67`（已合并 PR #99 `fix/ghcr-cangiolo-and-org-urls`）
- **涉及范围**：`start.sh`、`scripts/setup-wizard.sh`、`scripts/sccloud-ops.sh`、`docker-compose.yml`、`docker-compose.rootless.yml`、`.env.example`、`README.md`、`docs/MAINTAINERS.md`、`tests/test_start_sh.sh`（新增）、`.github/workflows/deploy-scripts-test.yml`（新增）

---

## 1. 背景与故障复盘

### 1.1 真实用户踩坑时间线

共享 Linux 服务器上的普通用户按 `README.md` 执行 `sh ./start.sh` 时，连续踩中以下问题：

1. **Docker 权限报错误导**：脚本在无法连接 Docker daemon 时直接输出 `sudo usermod -aG docker $USER`，未区分共享机与个人机，也未探测或引导 rootless Docker。
2. **Rootless 安装门槛高与 `docker info` 告警焦虑**：需管理员手工指导 4 步配置；安装完成后 `docker info` 打印大量 rootless 下正常的 cgroup/iptables `WARNING`（`No cpu cfs quota support`、`No io.weight`、`bridge-nf-call-iptables is disabled`），文档未说明其无害。
3. **拉取镜像失败后无脑回退全量 `docker compose build`（核心放大器）**：
   - `main` 分支默认镜像仍指向旧命名空间 `ghcr.io/zyzhou-saffron/sccloud-*`（账号更名后匿名访问返回 `DENIED`；当前可用公开包为 `ghcr.io/cangiolo/sccloud-{frontend,backend,r-engine}:latest`）。
   - `start.sh` 仅用当前 Shell 的 `curl` 检测 `https://ghcr.io/v2/`，忽略了 **Shell 环境变量代理 ≠ `dockerd`（尤其是 `systemd --user` rootless daemon）代理**，也未检测 Docker Hub 连通性。
   - `docker compose pull` 失败后，脚本吞掉真实拉取原因并直接回退到全量 `docker compose build`（含 `r-engine` 与 `r-engine-worker`）。
   - `r-engine/Dockerfile` 写死 `COPY r-library/`（572 个服务器预编译 R 包，`.gitignore` 不入库）。导致：
     - 未给 `dockerd` 配代理时：报错 `rocker/r-ver:4.3.2 ... i/o timeout`；
     - 配好代理重跑时：因旧 `.env` 仍指向失效镜像 + 盲目 `build`，报错 `"/r-library": not found`，文案 `镜像构建失败（R 需要 r-engine/r-library 或已有镜像）` 彻底误导用户。

### 1.2 与 PR #99 的边界

| 事项 | PR #99（已开待合） | 本计划 |
| --- | --- | --- |
| 默认镜像切到 `ghcr.io/cangiolo/sccloud-*` | 已覆盖 | 基于 PR #99 叠加 |
| 克隆地址 / OCI label 切到 `WeiLabCPU/scCloud` | 已覆盖 | 基于 PR #99 叠加 |
| 已有 `.env` 中旧 `zyzhou-saffron` 自动迁移 | 未覆盖 | **本计划实现** |
| 禁止 `pull` 失败自动回退 `build` + 失败分类诊断 | 未覆盖 | **本计划实现** |
| `r-engine` 无 `r-library` 时前置硬拦截 | 未覆盖 | **本计划实现** |
| Shell 代理 vs `dockerd` 代理错位检测 | 未覆盖 | **本计划实现** |
| Rootless 自动探测、禁用交互式 `sudo`、共享机引导 | 未覆盖 | **本计划实现** |
| 二次运行 `start.sh` 导致 `WEB_PORT` 自增漂移 | 未覆盖 | **本计划实现** |
| `docker-compose.rootless.yml` `ports:` 合并冲突 | 未覆盖 | **本计划实现** |
| `.env` 重写后权限从 `600` 降级为 `644` 泄漏 Redis 密码 | 未覆盖 | **本计划实现** |
| 架构检查 / `DockerRootDir` 磁盘检查 / bind-mount 权限检查 / `--doctor` | 未覆盖 | **本计划实现** |
| 自动化 Mock 单测 + CI + 沙盒冒烟 | 未覆盖 | **本计划实现** |

---

## 2. 改造目标与设计原则

1. **Fail Fast & Explain Clearly**：拉镜像失败就明确报告为什么拉失败（网络/代理、鉴权/命名空间、磁盘），绝不靠盲目 `docker compose build` 掩盖根因。
2. **Rootless First（共享服务器友好）**：不默认推荐危险的 `usermod -aG docker`，不触发阻塞式 `sudo` 密码输入，自动识别 rootless socket 与 daemon 属性。
3. **幂等与安全**：重复运行 `sh ./start.sh` 不漂移端口、不降级 `.env` 文件权限、不破坏已有运行中的业务数据。
4. **POSIX `/bin/sh` 兼容**：所有启动与运维脚本继续保持 POSIX `sh`（兼容 Ubuntu/Debian `dash` 与 RHEL/CentOS `bash`），不引入 bash-only 语法。

---

## 3. 详细实施任务拆解

### Phase 0：分支准备

- 方案 A（推荐）：先合并 PR #99 到 `main`，再从最新 `main` 创建分支 `feat/harden-one-click-deploy`。
- 方案 B：若 PR #99 暂缓合并，直接基于 `origin/fix/ghcr-cangiolo-and-org-urls` 创建堆叠分支，待 PR #99 合并后再 rebase 到 `main`。

---

### Phase 1：核心脚本与 Compose 改造

#### T1. Docker 与 Rootless 环境自适应探测（`start.sh`、`scripts/sccloud-ops.sh`）

- **补充 `PATH`**：若 `$HOME/bin` 存在且不在 `PATH` 中，自动加入（兼容 `get.docker.com/rootless` 默认安装路径）。
- **无阻塞探测顺序**：
  1. 检查 `docker` 与 `docker compose version`；
  2. 先执行 `docker info >/dev/null 2>&1`；
  3. 若失败且非 root：
     - 探测 rootless socket：`${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock`；
     - 若 socket 存在：设置并 `export DOCKER_HOST="unix://${sock}"` 重试 `docker info`，成功则打印 info 提示（建议运行 `docker context use rootless` 或将 `DOCKER_HOST` 写入 `~/.bashrc`）；
     - 若 socket 不存在但检测到 `~/.config/systemd/user/docker.service`：明确提示执行 `systemctl --user start docker`；
  4. 仅当前述失败时，尝试 **非交互** `sudo -n docker info >/dev/null 2>&1`（严禁裸 `sudo docker info` 卡住等密码或触发告警）；
  5. 若仍不可用，退出并输出**分层引导**：
     - 共享服务器（无 root 权限）：优先给出 rootless Docker 安装引导与 `README.md` 章节引用；
     - 个人独享机器：补充说明可用 `sudo usermod -aG docker $USER` 后重登。
- **自动识别 Rootless 模式与变量修正**：
  - 通过 `$SUDO docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -qi rootless` 设置 `IS_ROOTLESS=1`；
  - 统一环境变量名为 `SCCLOUD_ROOTLESS`（兼容旧名 `SCLOUD_ROOTLESS`），并**移至加载 `.env` 之后**求值，使写入 `.env` 也能生效；
  - 若用户未显式设置 `COMPOSE_FILE`，且满足 `SCCLOUD_ROOTLESS=1`（或 `SCLOUD_ROOTLESS=1` 或 `IS_ROOTLESS=1` 且未设 `SCCLOUD_ROOTLESS=0`）时，叠加 `-f docker-compose.yml -f docker-compose.rootless.yml`。

#### T2. `.env` 安全写入与旧镜像命名空间自动迁移（`start.sh`、`scripts/setup-wizard.sh`）

- **封装原子安全写工具函数**（`safe_set_env_key` / `safe_rewrite_env`）：
  - 在子 Shell 内 `umask 077` 写入 `.env.tmp` → `chmod 600 .env.tmp` → `mv .env.tmp .env`，彻底修复 `start.sh` 更新 `WEB_PORT`、`R_REDIS_URL` 时把 `.env` 权限重置为 `644` 导致 Redis 密码被同机其他用户可读的漏洞。
- **自动迁移废弃 GHCR 地址**：
  - 在 `setup-wizard.sh`（处理已有 `.env` 时）与 `start.sh`（加载 `.env` 前/后）检测 `FRONTEND_IMAGE`、`BACKEND_IMAGE`、`R_ENGINE_IMAGE` 是否仍包含 `ghcr.io/zyzhou-saffron/sccloud-`；
  - 若命中，自动替换为 `ghcr.io/cangiolo/sccloud-`（通过 `safe_rewrite_env` 写回 `.env` 保持 `600` 权限），并打印醒目迁移日志，确保之前已执行过 `start.sh` 的同学 `git pull && sh ./start.sh` 即可恢复。

#### T3. 代理错位诊断、全量拉取与禁止盲目回退 `build`（`start.sh`）

- **Shell 代理 vs `dockerd` 代理错位检查（`check_docker_proxy`）**：
  - 读取 Shell 代理：`${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}`；
  - 读取 daemon 代理与镜像源：`$SUDO docker info --format '{{.HTTPProxy}}|{{.HTTPSProxy}}'`、`Mirrors`；
  - 若 Shell 配了代理而 `dockerd` 未配置代理（且无 mirror），在 `pull` 前给出明确警告，并按 `IS_ROOTLESS` 打印对应配置片段：
    - Rootless：`~/.config/systemd/user/docker.service.d/http-proxy.conf` + `systemctl --user daemon-reload && systemctl --user restart docker`；
    - Rootful：`/etc/systemd/system/docker.service.d/http-proxy.conf` + `sudo systemctl daemon-reload && sudo systemctl restart docker`。
- **全量镜像完整性校验（`images_ready`）**：
  - 校验清单从 3 个业务镜像扩充为**全部 6 个服务镜像**：`FRONTEND_IMAGE`、`BACKEND_IMAGE`、`R_ENGINE_IMAGE`、`NGINX_IMAGE`、`MARIADB_IMAGE`、`REDIS_IMAGE`（与 `docker-compose.yml` 默认值保持一致）。
- **重构拉取与错误处理流程（默认模式，非 `--build`）**：
  1. 若 `DO_PULL=1`：不再因为 Shell `curl` 不通就直接跳过 `docker compose pull`（因为 `dockerd` 本身可能配置了代理或 mirror）；直接执行 `$DOCKER_COMPOSE pull` 并将输出同时记录到临时文件（`mktemp` + `trap` 清理），以便失败后做分类诊断。
  2. 执行 `ensure_r_engine_image`（依次检查本地镜像 / 常见本地 tag / `./data/sccloud-r-engine-image.tar.gz` 离线加载）。
  3. 检查 `images_ready`：
     - 全部 6 个镜像本地已就绪：若此前 `pull` 失败，打印提示“远端拉取失败，但本地已有全部镜像，将继续使用本地缓存启动”，然后继续 `up -d`；
     - **仍有镜像缺失：严禁回退到 `docker compose build`！** 根据 `pull` 日志内容做分类诊断后直接 `die`：
       - `i/o timeout` / `deadline exceeded` / `connection refused` / `TLS handshake` / `EOF` / `no such host` → 判定为**网络/代理问题**，按 rootless/rootful 输出 `dockerd` 代理配置命令，以及通过 `.env` 替换国内镜像源的方式；
       - `denied` / `unauthorized` / `manifest unknown` / `not found` → 判定为**镜像名或权限问题**，提示检查 `.env` 中 `*_IMAGE`（是否误指向旧仓库或拼错）或 `docker login ghcr.io`；
       - `no space left on device` → 指出 `DockerRootDir` 路径并建议清理或修改 `data-root`；
       - 若缺失 `R_ENGINE_IMAGE` → 额外提示普通用户**无需 `r-engine/r-library/`**，并给出离线加载 `data/sccloud-r-engine-image.tar.gz`（GitHub Release）的兜底方式。
- **`--build` 模式前置硬拦截**：
  - 仅在用户显式传 `--build` 时才执行构建；
  - 若 `./r-engine/r-library` 不存在或为空：
    - 若本地已有 `R_ENGINE_IMAGE`（含 `ensure_r_engine_image` 补齐后）：打印说明“缺少 `r-engine/r-library`，跳过 R 镜像构建，仅构建 `frontend` 与 `backend`”，执行 `$DOCKER_COMPOSE build frontend backend`；
    - 若本地也无 `R_ENGINE_IMAGE`：**在调用 BuildKit 之前立即退出**，明确告知“外部用户无需 `r-library/`，请直接运行 `sh ./start.sh` 拉取预构建 R 镜像，或先加载离线 tar 包”。

#### T4. 端口占用检测与幂等重跑修复（`start.sh`）

- **精确端口解析（`check_port_available`）**：
  - Linux `ss -tln` 改为 `awk` 提取第 4 列本地地址并精确匹配末尾 `:${port}`（如 `0.0.0.0:8080`、`127.0.0.1:8080`、`[::]:8080`、`*:8080`），避免宽泛正则误判。
- **识别自家已运行容器（`is_own_web_port`）**：
  - 当 `check_port_available "$WEB_PORT"` 判定被占用时，先执行 `$DOCKER_COMPOSE port web 8080 2>/dev/null`（匹配末尾 `:${WEB_PORT}`）；
  - 若确认占用者就是当前 compose 项目的 `web` 容器，打印 `✓ 端口 $WEB_PORT 正由当前 scCloud 服务使用，保持不变`，**不再自动递增 `WEB_PORT`**；
  - 只有被外部进程占用时才顺延查找可用端口并安全写回 `.env`。
- **修复 `print_access_info` 提示**：
  - 仅当 `WEB_BIND = "0.0.0.0"` 时打印公网暴露警告；`127.0.0.1` 时提示可通过 SSH 端口转发访问（例如 `ssh -L ${WEB_PORT}:127.0.0.1:${WEB_PORT} <user>@<host>`）。

#### T5. 前置体检与 `--doctor` 诊断模式（`start.sh`）

- **CPU 架构检查**：检测 `uname -m`，非 `x86_64`/`amd64` 时提前输出清晰告警（frontend 与 r-engine 仅提供 `linux/amd64` 镜像）。
- **双目录磁盘检查**：
  - 检查项目目录 `.`；
  - 同时查询 `docker info --format '{{.DockerRootDir}}'`，若所在挂载点与 `.` 不同则分别检查；
  - 告警阈值提升至 **20 GB**（6 个镜像解压后已约 8 GB，加上下载解压缓冲与项目数据）。
- **Rootless bind-mount 权限检查**：
  - 当 `IS_ROOTLESS=1` 时，检查 `./nginx/nginx.bridge.conf` 及 `./r-engine/plumber.R`、`./r-engine/worker.R`、`./r-engine/run_job.R`、`./r-engine/R`、`./r-engine/data` 是否对 other 可读（目录可读可执行）；若用户 `umask 077` 克隆导致无读权限，提前告警并给出 `chmod -R a+rX nginx r-engine` 修复命令。
- **新增 `sh ./start.sh --doctor`**：
  - 只读体检模式（不改 `.env`、不拉镜像、不启容器），逐项输出：架构、Docker 版本/模式（rootless/rootful）、`DockerRootDir` 与磁盘余量、Shell vs `dockerd` 代理状态与 mirrors、GHCR（`ghcr.io/cangiolo/sccloud-*` 匿名 token 探测）与 Docker Hub 连通性、`.env` 旧地址与权限检查、端口占用归属、本地镜像就绪情况；
  - 方便用户排障时一键执行并把文本发给维护者。

#### T6. Compose 文件与 `.env.example` 加固

- **`docker-compose.rootless.yml`**：
  - **移除重复的 `ports:` 块**，消除与 `docker-compose.yml` 合并时在未设 `WEB_BIND_ADDRESS` 场景下产生 `0.0.0.0:8080` + `127.0.0.1:8080` 双重绑定冲突，以及 `.env` 默认 `WEB_BIND_ADDRESS=0.0.0.0` 覆盖 `127.0.0.1` 导致注释与实际行为不符的问题；
  - 保留各服务 `restart: always`，在文件头注释清楚说明：监听地址统一在 `.env` 通过 `WEB_BIND_ADDRESS=127.0.0.1`（仅本机/SSH 转发）或 `0.0.0.0`（局域网）控制。
- **`docker-compose.yml` 与 `.env.example`**：
  - 将 3 个基础镜像参数化，保留现有 sha256 digest 钉死作为默认值：
    - `NGINX_IMAGE`（默认 `nginx:alpine@sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913`）
    - `MARIADB_IMAGE`（默认 `mariadb:11@sha256:2439dcd7d14010ecd1ff7a4e1c5abe8e208c34fe35290744deeeaac3569043c3`）
    - `REDIS_IMAGE`（默认 `redis:7-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf`）
  - 清理 `start.sh`、`scripts/setup-wizard.sh`、`docker-compose.yml` 中残留的外部产品名称（`闲鱼助手`）。

#### T7. `scripts/sccloud-ops.sh` 同步加固

- 同步 `$HOME/bin` `PATH` 补齐、rootless socket 自动探测、非交互 `sudo -n docker info`；
- 兼容 `SCCLOUD_ROOTLESS` 与 `SCLOUD_ROOTLESS`，且在加载 `.env` 之后计算 `COMPOSE_FILES`；
- `promote-admin` 增加 `<username>` 字符白名单校验（仅允许 `[A-Za-z0-9_.-@]`），防止特殊字符破坏 SQL。

---

### Phase 2：文档补齐

#### T8. 更新 `README.md` 与 `docs/MAINTAINERS.md`

- **`README.md`**：
  - 在“部署条件 / 常见问题”增加 `<details>` 折叠指引：**“共享 Linux 服务器（无 root / Rootless Docker）与代理配置”**：
    1. 前置检查：`command -v newuidmap newgidmap` 与 `grep "^$(whoami):" /etc/subuid /etc/subgid`（至少 65536）；
    2. 安装与启用：`dockerd-rootless-setuptool.sh install`（若系统 Docker 已运行则加 `--force`，不停止现有服务）、`systemctl --user enable --now docker`、`docker context use rootless`、管理员可选执行 `sudo loginctl enable-linger <user>`；
    3. 预期告警说明：明确注明 `docker info` 中的 `No cpu cfs quota / No io.weight / bridge-nf-call-iptables` 警告在 rootless 下属于正常无害现象；
    4. **代理配置重点**：明确区分终端代理与 Docker daemon 代理，给出 rootless `~/.config/systemd/user/docker.service.d/http-proxy.conf` 模板与生效命令；
  - 常见问题新增：`sh ./start.sh --doctor` 一键体检命令、旧版本 `.env` 自动升级说明、无需 `r-engine/r-library` 说明。
- **`docs/MAINTAINERS.md`**：
  - 更新 `SCCLOUD_ROOTLESS` 变量名（注明兼容旧 `SCLOUD_ROOTLESS`）；
  - 更新 `docker-compose.rootless.yml` 说明与基础镜像覆盖变量文档。

---

### Phase 3：自动化测试与 CI

#### T9. 新增 Mock 自动化测试脚本 `tests/test_start_sh.sh`

采用纯 POSIX `sh`（零外部依赖），在临时沙盒目录构造可控的 `fake-bin/`（mock `docker`、`curl`、`ss`、`df`、`uname`、`sudo`）前置到 `PATH`，覆盖全部关键分支：

| 编号 | 测试用例 | Mock 条件 | 断言标准 |
| --- | --- | --- | --- |
| U1 | 无 Docker 访问权限（共享机） | `docker info` 失败、无 rootless socket、`sudo -n` 失败 | 返回非零；不调用无 `-n` 的 `sudo`；输出包含 rootless 引导，不首推 `usermod -aG docker` |
| U2 | Rootless socket 自动发现 | 默认 `docker info` 失败，存在临时 `XDG_RUNTIME_DIR/docker.sock`，设置 `DOCKER_HOST` 后成功 | 自动切换并继续运行，打印 `DOCKER_HOST` / `context` 提示 |
| U3 | Shell 有代理但 `dockerd` 无代理 | `HTTPS_PROXY=http://127.0.0.1:7890`，`docker info` 返回空代理 + `rootless` | 打印错位警告，包含 `~/.config/systemd/user/docker.service.d/http-proxy.conf` |
| U4 | `pull` 网络超时失败 | `docker compose pull` 失败且输出 `i/o timeout`，本地缺镜像 | 返回非零；**绝不调用 `docker compose build`**；输出明确指向代理/网络 |
| U5 | `pull` 遇 `DENIED` / `manifest unknown` | `docker compose pull` 失败且输出 `denied`，本地缺镜像 | 返回非零；**绝不调用 `docker compose build`**；提示检查 `.env` 镜像名或登录 |
| U6 | `pull` 失败但本地 6 镜像全齐 | `docker compose pull` 返回非零，但 6 个镜像 `image inspect` 均成功 | 打印回退本地缓存警告，正常进入 `up -d` 并成功 |
| U7 | 无 `r-library` 执行 `--build`（且本地无 R 镜像） | 沙盒无 `r-engine/r-library/`，传 `--build` | 调用 `docker compose build` 前即退出；明确提示普通用户无需 `r-library` |
| U8 | 无 `r-library` 执行 `--build`（本地已有 R 镜像） | 沙盒无 `r-engine/r-library/`，本地有 `R_ENGINE_IMAGE`，传 `--build` | 仅调用 `docker compose build frontend backend`，不构建 `r-engine` |
| U9 | 旧 `.env` 自动迁移与权限保持 | 预置含 `ghcr.io/zyzhou-saffron/sccloud-*` 的 `.env` | 自动替换为 `ghcr.io/cangiolo/sccloud-*`，且 `.env` 权限为 `600` |
| U10 | 二次重跑端口不漂移 | `ss` 显示 `8091` 监听，且 `docker compose port web 8080` 返回 `0.0.0.0:8091` | `WEB_PORT` 保持 `8091`，不顺延到 `8092` |
| U11 | 端口被外部程序占用 | `ss` 显示 `8080` 监听，`docker compose port web 8080` 为空 | 自动改用 `8081`，写回 `.env` 且权限保持 `600` |
| U12 | ARM64 架构与 `DockerRootDir` 磁盘不足 | `uname -m` 返回 `aarch64`；`df` 返回 5 GB | 输出架构不兼容告警与磁盘不足告警 |
| U13 | `--doctor` 模式只读性 | 空目录或已有 `.env` 目录执行 `sh ./start.sh --doctor` | 不创建/修改 `.env` 和 `secrets/`，不调用 `pull`/`build`/`up`，输出完整体检项 |
| U14 | `sccloud-ops.sh` 安全与兼容 | 测 `SCCLOUD_ROOTLESS=1`、`SCLOUD_ROOTLESS=1`、`promote-admin` 非法用户名 | 叠加参数正确；含特殊字符用户名被拒绝 |

#### T10. 新增轻量 CI 工作流 `.github/workflows/deploy-scripts-test.yml`

- **触发路径**：`start.sh`、`scripts/**`、`docker-compose*.yml`、`.env.example`、`tests/test_start_sh.sh`、本 workflow 文件。
- **执行步骤**：
  1. `shellcheck` 检查 `start.sh`、`scripts/setup-wizard.sh`、`scripts/sccloud-ops.sh`、`tests/test_start_sh.sh`；
  2. `dash -n` 与 `bash -n` 语法检查，并用 `dash ./tests/test_start_sh.sh` 和 `bash ./tests/test_start_sh.sh` 双 Shell 运行全部单测；
  3. `docker compose` 渲染矩阵校验（默认配置、叠加 `docker-compose.rootless.yml` 带/不带 `WEB_BIND_ADDRESS`、自定义基础镜像环境变量）。

---

## 4. 四层验证与验收方案

> **生产安全红线**：本机当前已有线上 `sccloud` 栈运行在 `:8091`（7 个容器）。所有真实 Docker 测试必须显式设置独立 `COMPOSE_PROJECT_NAME`、独立端口与临时目录，**严禁在主仓库目录直接执行 `down -v` 或使用默认项目名 `sccloud`**。

### 第 1 层：静态语法与 Compose 渲染校验（秒级）

```bash
sh -n start.sh scripts/setup-wizard.sh scripts/sccloud-ops.sh tests/test_start_sh.sh
bash -n start.sh scripts/setup-wizard.sh scripts/sccloud-ops.sh tests/test_start_sh.sh

# 验证 rootless 叠加层在有/无 WEB_BIND_ADDRESS 下均只有 1 条 web.ports 映射
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.rootless.yml config \
  | grep -A 10 'ports:'
env -u WEB_BIND_ADDRESS docker compose --env-file /dev/null -f docker-compose.yml -f docker-compose.rootless.yml config \
  | grep -A 10 'ports:'
```

### 第 2 层：Mock 自动化单元测试（秒级）

```bash
sh ./tests/test_start_sh.sh
bash ./tests/test_start_sh.sh
```

验收：U1–U14 全部通过，退出码为 0。

### 第 3 层：本机真实 Rootless Docker 隔离沙盒冒烟（不碰线上 `:8091`）

```bash
# 1. 导出纯净工作区（不含 gitignored 的 r-engine/r-library、.env、secrets）
SMOKE_DIR=$(mktemp -d /tmp/sccloud-smoke.XXXXXX)
git archive HEAD | tar -x -C "$SMOKE_DIR"
cd "$SMOKE_DIR"

# 2. 体检模式（不改文件、不启容器）
COMPOSE_PROJECT_NAME=sccloud-smoke sh ./start.sh --doctor

# 3. 匿名拉取验证（空 DOCKER_CONFIG，排除本机登录态干扰）
ANON_DOCKER_CFG=$(mktemp -d /tmp/docker-anon.XXXXXX)
DOCKER_CONFIG="$ANON_DOCKER_CFG" docker manifest inspect ghcr.io/cangiolo/sccloud-frontend:latest >/dev/null
DOCKER_CONFIG="$ANON_DOCKER_CFG" docker manifest inspect ghcr.io/cangiolo/sccloud-backend:latest >/dev/null
DOCKER_CONFIG="$ANON_DOCKER_CFG" docker manifest inspect ghcr.io/cangiolo/sccloud-r-engine:latest >/dev/null

# 4. 隔离冷启动（全栈 7 容器）
COMPOSE_PROJECT_NAME=sccloud-smoke WEB_PORT=18091 WEB_BIND_ADDRESS=127.0.0.1 \
  sh ./start.sh

# 5. 权限与健康检查验收
stat -c '%a %n' .env secrets secrets/*
curl -fsS http://127.0.0.1:18091/healthz
curl -fsS http://127.0.0.1:18091/r-health

# 6. 二次重跑幂等性（确认 WEB_PORT 保持 18091，不漂移为 18092）
COMPOSE_PROJECT_NAME=sccloud-smoke sh ./start.sh --no-pull
grep '^WEB_PORT=18091$' .env

# 7. 运维脚本验证
COMPOSE_PROJECT_NAME=sccloud-smoke sh ./scripts/sccloud-ops.sh status

# 8. 无 r-library 下的 --build 保护验证
COMPOSE_PROJECT_NAME=sccloud-smoke sh ./start.sh --build

# 9. 确认线上 :8091 栈毫发无损，然后销毁沙盒
docker ps --filter "label=com.docker.compose.project=sccloud"
COMPOSE_PROJECT_NAME=sccloud-smoke docker compose --env-file .env down -v
rm -rf "$SMOKE_DIR" "$ANON_DOCKER_CFG"
```

### 第 4 层：真实新用户端到端复测（合并后）

1. **旧克隆目录升级路径**：让遇到问题的同学在原报错目录执行 `git pull && sh ./start.sh`，验证旧 `.env` 自动迁移为 `ghcr.io/cangiolo/...` 并一次拉起成功。
2. **故障引导路径**：在未配 `dockerd` 代理但终端有代理的账号下运行 `sh ./start.sh --doctor` 与 `sh ./start.sh`，确认输出直接给出 rootless `http-proxy.conf` 配置步骤，不再出现 `"/r-library": not found`。

---

## 5. 完成定义（Definition of Done）

- [x] PR #99 已合入或已基于其提交完成本分支变更
- [x] Phase 1（T1–T7）、Phase 2（T8）、Phase 3（T9–T10）全部完成
- [x] 第 1 层静态检查 + Compose 矩阵校验通过
- [x] 第 2 层 `tests/test_start_sh.sh` 14 项单测全部通过
- [x] 第 3 层本机 Rootless 隔离沙盒冒烟通过，线上 `:8091` 容器状态全程未受影响
- [x] 残留外部产品名（`闲鱼助手`）已从一键部署脚本与 compose 中全部清除
- [ ] 提交 PR 并完成 CI（`shellcheck` + `dash`/`bash` 单测 + `compose config`）全绿
