# 一键部署数据本地化（`./data/projects`）与项目目录命名重构计划

- **日期**：2026-11-20
- **状态**：已确认并实施（项目子目录命名选 **方案 A**，存量历史项目采用 **老新共存**，不附带离线改名脚本）
- **基线与前置依赖**：
  - `origin/main` 已合入 PR #100（`b65c7eb`，一键部署加固）。
  - PR #101（`fix/backend-tmp-storage`，已 CI 通过待合）已将导出 ZIP、分片上传与 `TMPDIR` 迁至容器 `${PROJECTS_ROOT}/_tmp`。
  - 本计划建议在 **PR #101 合入 `main` 后**，基于最新 `main` 新开分支（如 `feat/local-projects-storage-and-naming`）实施，避免与 PR #101 在 `config.py`、`data_cleanup.py`、`docker-entrypoint.sh` 上产生冲突。

---

## 一、背景与要解决的问题

1. **宿主机落盘位置脱离项目目录（普通服务器易撑爆系统根盘 `/`）**
   - 当前 [docker-compose.yml](../../docker-compose.yml) 将 `backend`、`r-engine`、`r-engine-worker` 的 `/data/projects` 挂载到 Docker 命名卷 `projects_data`。
   - 在默认 rootful Docker 服务器上，命名卷位于 `/var/lib/docker/volumes/sccloud_projects_data/_data`（通常在系统根盘 `/` 且在仓库目录外）；在 rootless Docker 下位于 `~/.local/share/docker/volumes/sccloud_projects_data/_data`（仍在项目目录外）。
   - 实际上 [scripts/setup-wizard.sh](../../scripts/setup-wizard.sh) 已经预创建了 `./data/projects`，但 `docker-compose.yml` 尚未改为挂载该目录。

2. **项目落盘目录命名不可读（`/6`、`/15`、`/19` 无法分辨归属与用途）**
   - 现状目录结构：
     ```text
     /data/projects/
     ├── 6/
     │   └── 0903/
     ├── 15/
     │   ├── 1118/
     │   ├── _convert/
     │   └── _uploads/
     ├── 19/
     │   └── test/
     ├── _inspect_tmp/
     └── _tmp/{exports,chunks}
     ```
   - 顶层直接是裸 `user_id`（`6`、`15`、`19`）与临时目录混排，运维或用户打开 `./data/projects` 时完全看不出哪个目录对应哪个账号、哪个项目。

3. **伴随发现的边界与隐患（顺手一并消除）**
   - `backend/scripts/docker-entrypoint.sh` 当前对整个 `${PROJECTS_ROOT}` 执行递归 `chown -R 10001:10001`：切为宿主机 bind mount `./data/projects` 后，不仅数据量大时启动慢，在 rootless Docker 下还会把 `./data/projects` 根目录属主改成 subuid（如 `437680`），导致宿主机用户无法直接管理目录。
   - `convert/router.py`（`download_converted`）与 `upload/router.py`（`inspect_file_by_path`）当前用 `abs_path.startswith(user_root)` 做路径越界校验，缺少路径分隔符边界（如 `/data/projects/15` 会误匹配前缀 `/data/projects/1`），应统一改为 `os.path.commonpath` 严格包含检查。
   - 游客升级正式账号（`POST /api/auth/upgrade`）会变更 `user.username`（从 `guest_<hex>` 改为正式用户名），且 `tasks.params`、`pipelines.params`、`tasks.result_path` 以及项目内 `*_result.json` 均持久化了绝对容器路径 `/data/projects/...`，设计新命名规则时必须保证**已有项目永不失效**。

---

## 二、目标架构设计

### 2.1 一键部署存储路径：默认收口到 `./data/projects`

- **环境变量分工清晰（双变量解耦）**：
  - `PROJECTS_DATA_DIR=./data/projects`（**宿主机目录**，新增于 `.env.example`，支持相对路径与绝对路径；需要挂独立大盘时可改为 `/mnt/data/sccloud-projects`）
  - `PROJECTS_ROOT=/data/projects`（**容器内固定挂载点**，保持不变；确保数据库和 `*_result.json` 内记录的容器路径与宿主机目录解耦，宿主机搬迁目录无需改库）
- **`docker-compose.yml` 挂载变更**：
  - `backend`、`r-engine`、`r-engine-worker` 三处统一改为：
    ```yaml
    - ${PROJECTS_DATA_DIR:-./data/projects}:${PROJECTS_ROOT:-/data/projects}
    ```
  - 顶层 `volumes:` 仅保留 `db_data:` 与 `redis_data:`（移除 `projects_data:` 声明；注：移除声明不会删除老部署已存在的 `sccloud_projects_data` 卷，便于安全迁移）。
- **`.gitignore` 防误提交**：
  - 新增 `data/projects/` 忽略规则，避免运行期项目文件与 `_tmp` 污染 `git status`。
- **目录预创建与跨容器权限策略**：
  - `scripts/setup-wizard.sh` 与 `start.sh` 在 `docker compose up` 前均按 `PROJECTS_DATA_DIR`（相对路径以 `$PROJECT_DIR` 为基准）执行 `mkdir -p`，避免 rootful Docker daemon 自动以 `root:root` 创建目录。
  - `backend/scripts/docker-entrypoint.sh` **取消递归 `chown -R 10001:10001 "$PROJECTS_ROOT_VAL"`**：
    - 仅确保 `${PROJECTS_ROOT}` 本身对容器内非 root 用户可写（如非可写时仅对顶层目录 `chmod 0777` 或 `a+rwx`，不改变宿主机 `./data/projects` 根目录属主，也不递归扫全盘）；
    - 创建 `${PROJECTS_ROOT}/users` 与 `${PROJECTS_ROOT}/_tmp/{exports,chunks,inspect}`，对 `_tmp` 及其子目录设置可写权限（`_tmp/inspect` 需 `r-engine(uid 1000)` 可读、`backend(uid 10001)` 可写删，统一 `chmod 0777` 或 `a+rwx`）。
    - 说明：在 rootless Docker 下，容器内 `backend(10001)` / `r-engine(1000)` 新建的子文件在宿主机上仍归对应 subuid 所有（内核 user namespace 机制决定），将在文档与 `sccloud-ops.sh` 中给出清理提示（如需宿主机整目录清理使用 `docker unshare rm -rf` 或运维子命令）。

---

### 2.2 项目落盘命名规则重构（消除裸 `/6`、`/15`、`/19`）

#### 顶层分桶（固定）
将用户数据与系统临时数据彻底分离，打开 `./data/projects/` 只看到两个清晰顶层目录：

```text
./data/projects/
├── users/          # 所有用户与项目数据
└── _tmp/           # 所有系统级临时区（导出包 / 上传分片 / inspect 临时合并）
    ├── exports/
    ├── chunks/
    └── inspect/    # 原 _inspect_tmp 收口至此
```

#### 用户目录命名：`{safe_username}_u{user_id}`
- 示例：
  - `users/admin_u6/`
  - `users/zhouy1_u15/`
  - `users/guest_a1b2c3d4_u19/`
- **设计理由**：
  1. **直观可读**：第一眼即见用户名，所有 `guest_*` 账号按字典序自然聚在一起。
  2. **数学上 100% 无碰撞**：注册正则要求 `username` 仅含 `[A-Za-z0-9_]`（并经安全清洗函数兜底，长度截断至 48 字符），后缀 `_u{user_id}` 绑定自增主键——即便旧用户注销后新用户复用同名，也不会发生路径冲突。
  3. **无需依赖用户目录名做反向解析**：项目路径以 DB `projects.storage_path` 为唯一真相源。

#### 项目目录命名（二选一，待你拍板）

- **方案 A（推荐 · 简洁且零侵入 R 引擎）**：
  ```text
  ./data/projects/users/{username}_u{user_id}/{project_name}/
  ```
  - 示例：`./data/projects/users/zhouy1_u15/PBMC_3k/`、`./data/projects/users/admin_u6/0903/`
  - 优点：
    - `r-engine/plumber.R` 的 `make_output_name()` 直接取 `basename(project_path)` 作为产物前缀（如 `PBMC_3k_qc_20261120175000_violin.png`），**R 引擎零改动**，输出文件名干净不冗长。
    - 同用户下项目名在 DB 层已校验唯一（`create_project` 判重返回 409）。
  - 不足：若用户自己给项目起名 `test` / `0903`，仅从目录名看不出创建日期与 `project_id`。

- **方案 B（更精细 · 项目目录同时携带日期与项目 ID）**：
  ```text
  ./data/projects/users/{username}_u{user_id}/{YYYYMMDD}_p{project_id}_{project_name}/
  ```
  - 示例：`./data/projects/users/zhouy1_u15/20261120_p12_PBMC_3k/`、`./data/projects/users/guest_a1b2c3d4_u19/20261120_p19_test/`
  - 优点：
    - 哪怕项目叫 `test`、`0903`，在磁盘上一眼能看到**创建日期 + 项目 ID + 项目名**，按日期天然排序。
    - 删除同名项目后立刻重建同名项目，不会复用旧目录名。
  - 联动改动（很小）：
    - `create_project` 先 `db.add(project); db.flush()` 拿到自增 `project.id`，再拼出 `storage_path` 并建目录（失败回滚并清目录）。
    - `r-engine/plumber.R` 的 `make_output_name()` 增加 1 行正则剥离 `^\d{8}_p\d+_` 前缀，避免产出文件出现双日期（`plumber.R` 通过 compose 只读挂入容器，无需重编 R 镜像）。

#### 统一路径辅助模块：`backend/app/utils/storage_paths.py`
新建单一模块集中管理所有路径拼装、目录清理与归属校验，杜绝散落拼接 `os.path.join(settings.projects_root, str(user.id), ...)`：
- `user_dir_name(user_id: int, username: str) -> str`：清洗用户名并生成 `{safe_username}_u{user_id}`。
- `user_root_dir(projects_root: str, user_id: int, username: str) -> str`：`{projects_root}/users/{safe_username}_u{user_id}`。
- `project_storage_dir(projects_root: str, user_id: int, username: str, project_name: str, project_id: int | None = None, created_at: datetime | None = None) -> str`：按最终确认的方案 A/B 生成项目目录。
- `user_convert_dir(...)` / `user_uploads_dir(...)`：返回 `{user_root}/_convert` 与 `{user_root}/_uploads`。
- `inspect_tmp_dir(projects_root: str)`：返回 `{projects_root}/_tmp/inspect`。
- `is_path_within(path: str, base_dir: str) -> bool`：基于 `os.path.realpath` + `os.path.commonpath` 做严格子路径判断，修掉 `startswith` 前缀穿透漏洞。
- `is_path_owned_by_user(path: str, projects_root: str, user_id: int, username: str, project_paths: Iterable[str]) -> bool`：同时兼容以下三种合法目录（保证老项目和 `upgrade_guest` 后的老文件不断链）：
  1. 当前用户的任一 `project.storage_path`；
  2. 新规则下当前用户的 `users/{username}_u{user_id}` 以及历史上同 `user_id` 的 `users/*_u{user_id}`（覆盖游客升级改名场景）；
  3. 历史旧规则的 `{projects_root}/{user_id}`。
- `cleanup_empty_user_dir(project_path: str, projects_root: str) -> None`：删除项目后仅当父目录是用户目录（不是 `projects_root` 也不是 `projects_root/users`）且为空时才 `rmdir`，防止误删顶层 `users/` 或根挂载点。

---

## 三、向后兼容与存量迁移策略（针对本机 `:8091` 及已有部署）

### 3.1 Docker 命名卷 `sccloud_projects_data` → `./data/projects` 无损迁移
老部署（包括本机运行中的 `:8091`）的项目数据目前在 Docker 命名卷 `sccloud_projects_data` 中。若直接切 `docker-compose.yml` 为 bind mount `./data/projects`，老用户直接跑 `sh ./start.sh` 会挂进一个空目录导致已有项目报 `404 Project files not found`。

**防踩坑机制（双保险）**：
1. **`start.sh` 启动前自动检测与拦截**：
   - 在 `compose up` 前检测：若 `PROJECTS_DATA_DIR` 目录为空（或不存在），且 Docker 中存在**非空**的命名卷 `sccloud_projects_data`（用轻量容器或 `docker volume inspect` 检查）：
     - 交互终端下给出清晰提示并支持一键自动同步（或通过 `sh ./scripts/sccloud-ops.sh migrate-projects-dir` 执行）；
     - 非交互环境下给出明确报错与可执行命令，**绝不静默以空目录启动覆盖视野**。
2. **`scripts/sccloud-ops.sh migrate-projects-dir` 子命令**：
   - 使用已有 `backend` 镜像起一次性容器，只读挂载旧卷、读写挂载目标 `${PROJECTS_DATA_DIR}` 执行 `cp -a /src/. /dst/`（保留时间戳与权限），校验文件数/大小后提示完成；
   - **默认保留旧命名卷 `sccloud_projects_data` 不删除**，作为人工确认后的回滚备份。

### 3.2 历史项目目录（`/6/0903`、`/15/1118`、`/19/test`）兼容与可选改名
由于历史项目的绝对路径已写入 MariaDB（`projects.storage_path`、`tasks.result_path`、`tasks.params`、`pipelines.params`）以及磁盘上的 `*_result.json`：
- **默认运行策略（推荐，零风险）**：
  - 卷数据迁到 `./data/projects` 后，历史目录 `/6/0903`、`/15/1118`、`/19/test` 路径与容器内 `/data/projects/...` 完全一致，**直接可用、零停机改表风险**；
  - 升级后所有新建项目、新上传与格式转换自动落到 `./data/projects/users/{username}_u{id}/...`；
  - 历史项目随数据保留策略（1/7/30 天）或手动删除后自然清空。
- **可选离线整理脚本 `backend/scripts/migrate_legacy_project_dirs.py`**：
  - 若你希望把本机现有的 `/6/0903`、`/15/1118`、`/19/test` 也一次性重命名为新格式，提供独立脚本：
    - 默认 `--dry-run` 只打印迁移计划，需显式 `--apply` 才落盘；
    - 执行前校验无 `pending/running` 任务；
    - 逐项目安全移动目录，并同步更新 `projects.storage_path`、`tasks.result_path`、`tasks.params`、`pipelines.params` 及项目目录内 `*_result.json` 中的旧容器路径前缀；
    - 失败时单项目回滚，确保不产生半迁移状态。

---

## 四、涉及文件清单与改动细则

| 模块 | 文件 | 改动内容 |
|---|---|---|
| **部署编排** | [docker-compose.yml](../../docker-compose.yml) | `backend` / `r-engine` / `r-engine-worker` 的 `projects_data` 改为 `${PROJECTS_DATA_DIR:-./data/projects}:${PROJECTS_ROOT:-/data/projects}`；移除 `volumes.projects_data` 声明 |
| **环境模板** | [.env.example](../../.env.example) | 新增 `PROJECTS_DATA_DIR=./data/projects` 及与 `PROJECTS_ROOT` 的关系说明 |
| **忽略规则** | [.gitignore](../../.gitignore) | 新增 `data/projects/` |
| **一键部署脚本** | [start.sh](../../start.sh) | 启动前创建 `PROJECTS_DATA_DIR`；检测“目标目录为空 + 旧命名卷 `sccloud_projects_data` 非空”时拦截并引导/执行迁移 |
| **初始化向导** | [scripts/setup-wizard.sh](../../scripts/setup-wizard.sh) | 按 `.env` 中 `PROJECTS_DATA_DIR`（默认 `./data/projects`）预建目录；已有 `.env` 补齐默认说明（不破坏已有自定义配置） |
| **运维脚本** | [scripts/sccloud-ops.sh](../../scripts/sccloud-ops.sh) | 新增 `migrate-projects-dir` 子命令封装卷到目录的无损迁移与校验 |
| **容器入口** | [backend/scripts/docker-entrypoint.sh](../../backend/scripts/docker-entrypoint.sh) | 取消对整个 `${PROJECTS_ROOT}` 的递归 `chown -R`；改为保证顶层、`users/`、`_tmp/{exports,chunks,inspect}` 对跨容器可写 |
| **路径核心模块** | `backend/app/utils/storage_paths.py`（新增） | 统一封装用户目录、项目目录、`_convert`、`_uploads`、`_tmp/inspect` 路径生成、安全空父目录清理与 `commonpath` 归属校验 |
| **后端配置** | [backend/app/config.py](../../backend/app/config.py) | 补充 `inspect_tmp_dir`（`{tmp_root}/inspect`）与 `users_root`（`{projects_root}/users`）属性 |
| **项目路由** | [backend/app/projects/router.py](../../backend/app/projects/router.py) | `create_project` 改用 `storage_paths.project_storage_dir()`；`delete_project` 改用 `cleanup_empty_user_dir()` |
| **上传路由** | [backend/app/upload/router.py](../../backend/app/upload/router.py) | 无 `project_id` 上传改用 `user_uploads_dir()`；`inspect_file` 改用 `settings.inspect_tmp_dir`；`inspect_file_by_path` 改用 `is_path_within()` 严格校验 |
| **转换路由** | [backend/app/convert/router.py](../../backend/app/convert/router.py) | `upload_for_convert`、`merge_mtx_to_rds` 改用 `user_convert_dir()`；`download_converted` 改用 `is_path_owned_by_user()`（兼容新旧路径与游客升级，并修复前缀越界） |
| **清理服务** | [backend/app/utils/data_cleanup.py](../../backend/app/utils/data_cleanup.py) | `cleanup_stale_tmp()` 同时覆盖新 `_tmp/inspect` 与旧 `_inspect_tmp`；`_cleanup_expired_projects()` 改用 `cleanup_empty_user_dir()` |
| **管理员路由** | [backend/app/admin/router.py](../../backend/app/admin/router.py) | `delete_user` 删除项目后一并清理该用户新/旧目录（含残留 `_convert` / `_uploads`） |
| **R 引擎（仅方案 B）** | [r-engine/plumber.R](../../r-engine/plumber.R) | 若选方案 B，`make_output_name()` 剥离 `^\d{8}_p\d+_` 前缀保持产物文件名简洁 |
| **可选迁移脚本** | `backend/scripts/migrate_legacy_project_dirs.py`（新增） | 可选离线工具（默认 `--dry-run`），将旧 `/6/0903` 类目录批量重命名并同步 DB 与 `*_result.json` |
| **文档** | [README.md](../../README.md)、[docs/MAINTAINERS.md](../../docs/MAINTAINERS.md) | 更新数据目录说明（默认 `./data/projects`）、自定义路径方法、老卷迁移指引与 rootless 权限说明 |
| **自动化测试** | `backend/tests/test_storage_paths.py`（新增）、[backend/tests/test_tmp_storage.py](../../backend/tests/test_tmp_storage.py)、[tests/test_start_sh.sh](../../tests/test_start_sh.sh) | 覆盖新路径规则、游客升级兼容、老路径兼容、防路径穿透/前缀越界、空父目录安全清理、`docker-entrypoint.sh` 不递归 `chown`、`docker compose config` 渲染 `./data/projects` 与 `start.sh` 旧卷检测拦截 |

---

## 五、分阶段实施与验证计划

1. **前置合入**：确认并 squash 合并已绿灯的 PR #101（`fix/backend-tmp-storage`），本地拉取最新 `main`，新建分支 `feat/local-projects-storage-and-naming`。
2. **阶段 1（后端路径模块与路由接入）**：实现 `storage_paths.py`，改造 `config.py` / `projects` / `upload` / `convert` / `data_cleanup` / `admin`（如选方案 B 同步改 `r-engine/plumber.R`），编写并运行全部 `backend/tests/` 单测。
3. **阶段 2（一键部署与入口脚本）**：修改 `docker-compose.yml`、`.env.example`、`.gitignore`、`backend/scripts/docker-entrypoint.sh`、`start.sh`、`scripts/setup-wizard.sh`、`scripts/sccloud-ops.sh`，补充 `tests/test_start_sh.sh` 用例并通过 ShellCheck + `dash`/`bash` 全量测试 + `docker compose config` 矩阵校验。
4. **阶段 3（迁移工具与文档）**：落盘 `migrate_legacy_project_dirs.py`（含单元测试验证 `--dry-run` 与 `--apply` 完整同步 `projects`/`tasks`/`pipelines`/`*_result.json`），更新 `README.md` 与 `docs/MAINTAINERS.md`。
5. **阶段 4（容器级集成验证 & 提 PR）**：
   - 在隔离容器中验证 `backend` + `docker-entrypoint.sh` 对 bind mount 目录的权限与读写（不递归篡改宿主机根目录属主）；
   - 提交 PR 并等待 `backend tests (pytest)` 与 `deploy-scripts-test` 全部 CI 通过；
   - **生产 `:8091` 切换（仅在你确认后执行）**：按步骤备份旧卷 → `migrate-projects-dir` 复制数据到 `./data/projects` →（可选）执行历史目录改名 → 重建启动并做冒烟验证。

---

## 六、开写代码前需要你确认的 2 个选项

1. **项目子目录命名选方案 A 还是方案 B？**
   - **方案 A（推荐）**：`./data/projects/users/{username}_u{user_id}/{project_name}/`（最简洁，R 引擎零改动）
   - **方案 B**：`./data/projects/users/{username}_u{user_id}/{YYYYMMDD}_p{project_id}_{project_name}/`（目录自带日期与项目 ID，联动 2 行修改 `r-engine/plumber.R` 去重前缀）
2. **是否需要同时附带“历史目录改名迁移脚本”？**（默认新老共存零风险；若你也想顺手把本机现存的 `/6/0903`、`/15/1118`、`/19/test` 整理成新名字，我们就把离线迁移脚本一起带上。）
