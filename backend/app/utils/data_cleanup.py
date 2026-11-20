"""
scCloud v2 — 数据清理服务

定期检查并删除过期项目（数据库记录 + 磁盘文件）。
保留策略根据用户角色决定：
  guest → 1 天
  user  → 7 天
  super → 30 天
  admin → 30 天

判断依据：项目 created_at（创建时间），固定到期。
"""

import asyncio
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.models import Project, SessionLocal, User, get_retention_days


class DataCleanup:
    """定期清理过期项目与过期临时文件的后台服务。"""

    async def run(self):
        settings = get_settings()
        interval = settings.cleanup_interval_hours * 3600
        print(f"[DataCleanup] 已启动, 每 {settings.cleanup_interval_hours} 小时检查一次")
        while True:
            await asyncio.sleep(interval)
            try:
                result = self._cleanup_expired_projects()
                if result["deleted"] > 0:
                    print(
                        f"[DataCleanup] 已清理 {result['deleted']} 个过期项目, "
                        f"释放 {result['freed_mb']:.1f} MB"
                    )
                tmp_res = self.cleanup_stale_tmp()
                if tmp_res["removed"] > 0:
                    print(
                        f"[DataCleanup] 已清理 {tmp_res['removed']} 个过期临时文件/目录, "
                        f"释放 {tmp_res['freed_mb']:.1f} MB"
                    )
            except Exception as e:
                print(f"[DataCleanup] 清理异常: {e}")

    def cleanup_stale_tmp(
        self,
        export_max_age_sec: int = 7200,
        chunk_max_age_sec: int = 86400,
        inspect_max_age_sec: int = 7200,
        legacy_tmp_dir: str = "/tmp",
    ) -> dict:
        """清理滞留的导出 ZIP、中断的分片上传目录与临时 inspect 文件。"""
        settings = get_settings()
        now = time.time()
        removed = 0
        freed_bytes = 0

        def _prune_dir(
            target_dir: str,
            max_age: int,
            name_prefix: str | None = None,
            name_suffix: str | None = None,
            files_only: bool = False,
        ) -> None:
            nonlocal removed, freed_bytes
            if not os.path.isdir(target_dir):
                return
            for entry in os.listdir(target_dir):
                if name_prefix and not entry.startswith(name_prefix):
                    continue
                if name_suffix and not entry.endswith(name_suffix):
                    continue
                full = os.path.join(target_dir, entry)
                try:
                    is_link = os.path.islink(full)
                    is_dir = os.path.isdir(full) and not is_link
                    if files_only and is_dir:
                        continue
                    mtime = os.lstat(full).st_mtime if is_link else _latest_mtime(full)
                    if now - mtime < max_age:
                        continue
                    if is_dir:
                        freed_bytes += _dir_size(full)
                        shutil.rmtree(full, ignore_errors=True)
                        removed += 1
                    elif os.path.isfile(full) or is_link:
                        freed_bytes += os.path.getsize(full) if (not is_link and os.path.isfile(full)) else 0
                        os.unlink(full)
                        removed += 1
                except OSError:
                    pass

        _prune_dir(settings.export_tmp_dir, export_max_age_sec)
        _prune_dir(settings.upload_chunk_dir, chunk_max_age_sec)
        _prune_dir(settings.tmp_root, export_max_age_sec, files_only=True)
        _prune_dir(os.path.join(settings.projects_root, "_inspect_tmp"), inspect_max_age_sec)
        # 兼容清理旧版本遗留在 /tmp 的导出包与分片（仅清理 sccloud_*.zip，不误删 sccloud_chunks 目录）
        if legacy_tmp_dir:
            _prune_dir(
                legacy_tmp_dir,
                export_max_age_sec,
                name_prefix="sccloud_",
                name_suffix=".zip",
                files_only=True,
            )
            _prune_dir(os.path.join(legacy_tmp_dir, "sccloud_chunks"), chunk_max_age_sec)

        return {"removed": removed, "freed_mb": freed_bytes / 1024 / 1024}

    def _cleanup_expired_projects(self) -> dict:
        """查询过期项目并删除（DB + 磁盘）。返回统计信息。"""
        db = SessionLocal()
        deleted = 0
        freed_bytes = 0
        try:
            now = datetime.now(timezone.utc)
            projects = db.query(Project).join(User, Project.user_id == User.id).all()

            for p in projects:
                role = p.owner.role if p.owner else "user"
                retention_days = get_retention_days(role)
                deadline = p.created_at.replace(tzinfo=timezone.utc) + timedelta(days=retention_days)
                if now >= deadline:
                    # 删磁盘
                    if p.storage_path:
                        try:
                            import os
                            if os.path.isdir(p.storage_path):
                                freed_bytes += _dir_size(p.storage_path)
                                shutil.rmtree(p.storage_path, ignore_errors=True)
                                # 清理用户级空目录
                                parent = os.path.dirname(p.storage_path)
                                if os.path.isdir(parent) and not os.listdir(parent):
                                    os.rmdir(parent)
                        except OSError:
                            pass
                    # 删 DB（cascade 自动删 tasks/pipelines）
                    db.delete(p)
                    deleted += 1

            if deleted > 0:
                db.commit()
            return {"deleted": deleted, "freed_mb": freed_bytes / 1024 / 1024}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def cleanup_now(self) -> dict:
        """手动触发一次清理（供管理员 API 调用）。"""
        proj = self._cleanup_expired_projects()
        tmp = self.cleanup_stale_tmp()
        return {
            "deleted": proj["deleted"],
            "freed_mb": round(proj["freed_mb"] + tmp["freed_mb"], 2),
            "tmp_removed": tmp["removed"],
        }

    def get_stats(self) -> dict:
        """获取即将过期和已过期的项目统计。"""
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            projects = db.query(Project).join(User, Project.user_id == User.id).all()

            expired = 0
            expiring_soon = 0  # ≤2 天内过期
            for p in projects:
                role = p.owner.role if p.owner else "user"
                retention_days = get_retention_days(role)
                deadline = p.created_at.replace(tzinfo=timezone.utc) + timedelta(days=retention_days)
                remaining = (deadline - now).total_seconds() / 86400
                if remaining <= 0:
                    expired += 1
                elif remaining <= 2:
                    expiring_soon += 1

            return {"expired_pending": expired, "expiring_soon": expiring_soon}
        finally:
            db.close()


def _latest_mtime(path: str) -> float:
    """获取文件或目录（含内部最新文件）的最近修改时间。"""
    latest = os.path.getmtime(path)
    if os.path.isdir(path) and not os.path.islink(path):
        for dirpath, _, filenames in os.walk(path):
            try:
                latest = max(latest, os.path.getmtime(dirpath))
            except OSError:
                pass
            for fn in filenames:
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(dirpath, fn)))
                except OSError:
                    pass
    return latest


def _dir_size(path: str) -> int:
    """递归计算目录大小（字节）。"""
    import os
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total
