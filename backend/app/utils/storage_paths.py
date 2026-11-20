"""
scCloud v2 — 项目与用户磁盘路径统一管理模块

目标目录布局（以 PROJECTS_ROOT=/data/projects 为例）：
  /data/projects/
  ├── users/
  │   └── {safe_username}_u{user_id}/
  │       ├── {project_name}/        # 方案 A：最末级保持项目名，R 引擎产出文件前缀不变
  │       ├── _uploads/              # 未绑定项目时的分片上传目录
  │       └── _convert/              # 格式转换临时目录
  └── _tmp/
      ├── exports/                   # 导出 ZIP 临时目录
      ├── chunks/                    # 分片上传暂存目录
      └── inspect/                   # 上传前 R inspect 临时合并目录

向后兼容（老新共存）：
  - 历史项目 DB 中的 storage_path（如 /data/projects/6/0903）保持原样可用；
  - 游客升级账号（POST /api/auth/upgrade）会变更 username，已建项目仍通过 DB storage_path 访问，
    归属校验同时认可 users/{username}_u{id}、历史 users/*_u{id} 与旧版 {id}。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable

_SAFE_USER_RE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_username(username: str | None) -> str:
    """将用户名清洗为安全的目录名前缀（不含路径分隔符或特殊字符）。"""
    cleaned = _SAFE_USER_RE.sub("_", (username or "").strip()).strip("_-")
    if not cleaned:
        return "user"
    return cleaned[:48]


def user_dir_name(user_id: int, username: str | None) -> str:
    """生成人类可读且全局唯一的用户目录名：{safe_username}_u{user_id}。"""
    return f"{sanitize_username(username)}_u{int(user_id)}"


def users_root_dir(projects_root: str) -> str:
    """所有用户数据的统一父目录：{projects_root}/users。"""
    return os.path.join(projects_root, "users")


def user_root_dir(projects_root: str, user_id: int, username: str | None) -> str:
    """返回新规则下的用户根目录：{projects_root}/users/{safe_username}_u{user_id}。"""
    return os.path.join(users_root_dir(projects_root), user_dir_name(user_id, username))


def legacy_user_root_dir(projects_root: str, user_id: int) -> str:
    """返回旧规则下的裸数字用户目录：{projects_root}/{user_id}（仅用于老新共存兼容）。"""
    return os.path.join(projects_root, str(int(user_id)))


def ensure_dir_mode(path: str, mode: int) -> None:
    """确保目录存在并尽可能放宽权限，便于 backend(10001) 与 r-engine(1000) 跨容器协作。"""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_user_root(projects_root: str, user_id: int, username: str | None) -> str:
    """创建并确保 users/ 与用户根目录对同挂载点的 R 引擎容器可穿越访问。"""
    u_root = users_root_dir(projects_root)
    ensure_dir_mode(u_root, 0o777)
    user_dir = user_root_dir(projects_root, user_id, username)
    ensure_dir_mode(user_dir, 0o777)
    return user_dir


def project_storage_dir(
    projects_root: str,
    user_id: int,
    username: str | None,
    project_name: str,
) -> str:
    """生成项目落盘目录（方案 A）：{projects_root}/users/{safe_username}_u{user_id}/{project_name}。"""
    name = (project_name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or ".." in name:
        raise ValueError(f"非法项目目录名: {project_name!r}")
    base = user_root_dir(projects_root, user_id, username)
    candidate = os.path.join(base, name)
    if not is_path_within(candidate, base):
        raise ValueError(f"项目路径越界: {project_name!r}")
    return candidate


def user_convert_dir(projects_root: str, user_id: int, username: str | None) -> str:
    """返回用户的格式转换目录：{user_root}/_convert。"""
    return os.path.join(user_root_dir(projects_root, user_id, username), "_convert")


def user_uploads_dir(projects_root: str, user_id: int, username: str | None) -> str:
    """返回用户未绑定项目时的分片上传目录：{user_root}/_uploads。"""
    return os.path.join(user_root_dir(projects_root, user_id, username), "_uploads")


def is_path_within(path: str | None, base_dir: str | None, *, allow_equal: bool = False) -> bool:
    """基于 realpath + commonpath 的严格目录包含校验，防止前缀误判与符号链接逃逸。"""
    if not path or not base_dir:
        return False
    try:
        real_path = os.path.realpath(path)
        real_base = os.path.realpath(base_dir)
        common = os.path.commonpath([real_path, real_base])
    except (ValueError, OSError):
        return False
    if common != real_base:
        return False
    if not allow_equal and real_path == real_base:
        return False
    return True


def user_owned_roots(projects_root: str, user_id: int, username: str | None) -> list[str]:
    """
    列出归属该用户的全部合法根目录（老新共存 + 游客升级改名兼容）：
      1. 当前新目录：{projects_root}/users/{safe_username}_u{user_id}
      2. 同 user_id 的历史更名目录（如 guest_xxxxxxxx_u{user_id}）
      3. 旧版数字目录：{projects_root}/{user_id}
    """
    uid = int(user_id)
    roots: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            roots.append(norm)

    _add(user_root_dir(projects_root, uid, username))

    u_root = users_root_dir(projects_root)
    suffix = f"_u{uid}"
    if os.path.isdir(u_root):
        try:
            for entry in os.listdir(u_root):
                if entry.endswith(suffix):
                    full = os.path.join(u_root, entry)
                    if os.path.isdir(full) and not os.path.islink(full):
                        _add(full)
        except OSError:
            pass

    _add(legacy_user_root_dir(projects_root, uid))
    return roots


def is_path_owned_by_user(
    path: str | None,
    projects_root: str,
    user_id: int,
    username: str | None,
    project_paths: Iterable[str | None] = (),
) -> bool:
    """判断给定文件路径是否严格位于当前用户归属目录或任一所属项目目录下。"""
    if not path:
        return False
    for root in user_owned_roots(projects_root, user_id, username):
        if is_path_within(path, root):
            return True
    for p in project_paths:
        if p and is_path_within(path, p):
            return True
    return False


def cleanup_empty_user_dir(project_path: str | None, projects_root: str) -> None:
    """删除项目目录后，如其父目录为非顶层的空用户目录则顺带清理，绝不删除 projects_root 或 users/。"""
    if not project_path:
        return
    try:
        parent = os.path.dirname(os.path.normpath(project_path))
        if not os.path.isdir(parent) or os.path.islink(parent):
            return
        real_parent = os.path.realpath(parent)
        real_root = os.path.realpath(projects_root)
        real_users = os.path.realpath(users_root_dir(projects_root))
        if real_parent in (real_root, real_users):
            return
        if not is_path_within(real_parent, real_root):
            return
        if not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


def remove_user_dirs(projects_root: str, user_id: int, username: str | None) -> None:
    """管理员删除用户时，清理该用户对应的新旧根目录（含 _convert / _uploads 残留）。"""
    real_root = os.path.realpath(projects_root)
    real_users = os.path.realpath(users_root_dir(projects_root))
    for root in user_owned_roots(projects_root, user_id, username):
        try:
            if not os.path.isdir(root) or os.path.islink(root):
                continue
            real_candidate = os.path.realpath(root)
            if real_candidate in (real_root, real_users):
                continue
            if not is_path_within(real_candidate, real_root):
                continue
            shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass
