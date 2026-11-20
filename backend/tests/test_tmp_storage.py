"""后端大临时文件存储与清理回归测试（避免撑爆 128MB /tmp tmpfs）。"""

from __future__ import annotations

import errno
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import models
from app.projects.router import download_project
from app.upload.router import (
    complete_upload,
    init_upload,
    inspect_file,
    upload_chunk,
    upload_status,
)
from app.utils.data_cleanup import DataCleanup


def _set_mtime(path: str | Path, age_seconds: float) -> None:
    ts = time.time() - age_seconds
    os.utime(path, (ts, ts))


class TestBackendTmpStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.projects_root = os.path.join(self._tmpdir.name, "projects")
        os.makedirs(self.projects_root, exist_ok=True)
        self._env_patch = patch.dict(
            os.environ,
            {"PROJECTS_ROOT": self.projects_root},
            clear=False,
        )
        self._env_patch.start()
        get_settings.cache_clear()

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.TestSession = sessionmaker(
            bind=self.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        self._session_patch = patch.object(models, "SessionLocal", self.TestSession)
        self._session_patch.start()

    def tearDown(self) -> None:
        self._session_patch.stop()
        models.Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self._env_patch.stop()
        get_settings.cache_clear()
        self._tmpdir.cleanup()

    def _create_user_and_project(self, role: str = "admin") -> tuple[models.User, models.Project]:
        db = self.TestSession()
        try:
            user = models.User(
                username="tester",
                email="tester@example.com",
                password_hash="not-a-real-hash",
                role=role,
                is_guest=False,
                is_active=True,
                max_projects=10,
                total_quota=100,
                used_quota=0,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            project_dir = os.path.join(self.projects_root, str(user.id), "demo_proj")
            os.makedirs(project_dir, exist_ok=True)
            project = models.Project(
                user_id=user.id,
                name="demo_proj",
                species="human",
                storage_path=project_dir,
            )
            db.add(project)
            db.commit()
            db.refresh(project)
            return user, project
        finally:
            db.close()

    def test_settings_tmp_paths_under_projects_root(self) -> None:
        settings = get_settings()
        self.assertEqual(settings.users_root, os.path.join(self.projects_root, "users"))
        self.assertEqual(settings.tmp_root, os.path.join(self.projects_root, "_tmp"))
        self.assertEqual(
            settings.export_tmp_dir,
            os.path.join(self.projects_root, "_tmp", "exports"),
        )
        self.assertEqual(
            settings.upload_chunk_dir,
            os.path.join(self.projects_root, "_tmp", "chunks"),
        )
        self.assertEqual(
            settings.inspect_tmp_dir,
            os.path.join(self.projects_root, "_tmp", "inspect"),
        )

    async def test_download_project_uses_export_tmp_dir_and_cleans_up(self) -> None:
        user, project = self._create_user_and_project()
        pdir = Path(project.storage_path)
        (pdir / "qc_20261120120000_violin.png").write_bytes(b"png-bytes")
        (pdir / "markers_result.json").write_text('{"ok": true}', encoding="utf-8")
        (pdir / ".gitkeep").write_text("", encoding="utf-8")
        (pdir / "tasks").mkdir()
        (pdir / "tasks" / "internal.log").write_text("skip-me", encoding="utf-8")

        db = self.TestSession()
        try:
            bg1 = BackgroundTasks()
            resp1 = await download_project(project.id, bg1, current_user=user, db=db)
            bg2 = BackgroundTasks()
            resp2 = await download_project(project.id, bg2, current_user=user, db=db)
        finally:
            db.close()

        export_dir = os.path.realpath(get_settings().export_tmp_dir)
        path1 = os.path.realpath(resp1.path)
        path2 = os.path.realpath(resp2.path)

        self.assertTrue(path1.startswith(export_dir + os.sep), path1)
        self.assertTrue(path2.startswith(export_dir + os.sep), path2)
        self.assertNotEqual(path1, path2)
        self.assertTrue(os.path.isfile(path1))
        self.assertTrue(os.path.isfile(path2))

        with zipfile.ZipFile(path1, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("demo_proj/1_Data_Preprocessing/qc_violin.png", names)
        self.assertIn("demo_proj/4_Differential_Genes/markers_result.json", names)
        self.assertFalse(any(".gitkeep" in n or "internal.log" in n for n in names))

        await bg1()
        await bg2()
        self.assertFalse(os.path.exists(path1))
        self.assertFalse(os.path.exists(path2))

    async def test_download_project_enospc_raises_and_cleans_partial_zip(self) -> None:
        user, project = self._create_user_and_project()
        (Path(project.storage_path) / "qc_plot.png").write_bytes(b"payload")

        def fail_enospc(self_zf, *args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        db = self.TestSession()
        try:
            with patch.object(zipfile.ZipFile, "write", fail_enospc):
                with self.assertRaises(OSError) as ctx:
                    await download_project(
                        project.id,
                        BackgroundTasks(),
                        current_user=user,
                        db=db,
                    )
            self.assertEqual(ctx.exception.errno, errno.ENOSPC)
        finally:
            db.close()

        export_dir = get_settings().export_tmp_dir
        self.assertEqual(os.listdir(export_dir), [])

    async def test_download_project_skips_missing_source_file_non_enospc(self) -> None:
        user, project = self._create_user_and_project()
        pdir = Path(project.storage_path)
        (pdir / "qc_a.png").write_bytes(b"a")
        (pdir / "qc_b.png").write_bytes(b"b")

        real_write = zipfile.ZipFile.write

        def flaky_write(self_zf, filename, arcname=None, *args, **kwargs):
            if str(filename).endswith("qc_a.png"):
                raise OSError(errno.ENOENT, "vanished")
            return real_write(self_zf, filename, arcname, *args, **kwargs)

        db = self.TestSession()
        try:
            bg = BackgroundTasks()
            with patch.object(zipfile.ZipFile, "write", flaky_write):
                resp = await download_project(project.id, bg, current_user=user, db=db)
        finally:
            db.close()

        with zipfile.ZipFile(resp.path, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("demo_proj/1_Data_Preprocessing/qc_b.png", names)
        self.assertNotIn("demo_proj/1_Data_Preprocessing/qc_a.png", names)
        await bg()
        self.assertFalse(os.path.exists(resp.path))

    async def test_chunked_upload_uses_projects_root_tmp_and_validates_id(self) -> None:
        user, project = self._create_user_and_project(role="admin")

        init_resp = await init_upload(
            filename="sample.rds",
            file_size=11,
            current_user=user,
        )
        upload_id = init_resp.upload_id
        expected_chunk_dir = os.path.join(get_settings().upload_chunk_dir, upload_id)
        self.assertTrue(os.path.isdir(expected_chunk_dir))
        self.assertFalse(os.path.exists(os.path.join("/tmp/sccloud_chunks", upload_id)))

        # 拒绝非法/路径穿越 upload_id
        for bad_id in ("../escape", "a/b", "..", "id with space"):
            with self.assertRaises(HTTPException) as ctx1:
                await upload_status(bad_id, current_user=user)
            self.assertEqual(ctx1.exception.status_code, 400)
            with self.assertRaises(HTTPException) as ctx2:
                await upload_chunk(
                    upload_id=bad_id,
                    chunk_index=0,
                    chunk=UploadFile(filename="c", file=io.BytesIO(b"x")),
                    current_user=user,
                )
            self.assertEqual(ctx2.exception.status_code, 400)
            with self.assertRaises(HTTPException) as ctx3:
                await complete_upload(
                    upload_id=bad_id,
                    project_id=project.id,
                    sample_groups=None,
                    current_user=user,
                )
            self.assertEqual(ctx3.exception.status_code, 400)
            with self.assertRaises(HTTPException) as ctx4:
                await inspect_file(upload_id=bad_id, current_user=user)
            self.assertEqual(ctx4.exception.status_code, 400)

        await upload_chunk(
            upload_id=upload_id,
            chunk_index=0,
            chunk=UploadFile(filename="part0", file=io.BytesIO(b"hello ")),
            current_user=user,
        )
        await upload_chunk(
            upload_id=upload_id,
            chunk_index=1,
            chunk=UploadFile(filename="part1", file=io.BytesIO(b"world")),
            current_user=user,
        )

        status_resp = await upload_status(upload_id, current_user=user)
        self.assertEqual(status_resp["received_chunks"], 2)

        complete_resp = await complete_upload(
            upload_id=upload_id,
            project_id=project.id,
            sample_groups=None,
            current_user=user,
        )
        self.assertEqual(complete_resp.status, "completed")
        self.assertEqual(complete_resp.total_chunks, 2)
        self.assertTrue(os.path.isfile(complete_resp.path))
        self.assertEqual(Path(complete_resp.path).read_bytes(), b"hello world")
        self.assertFalse(os.path.exists(expected_chunk_dir))

    def test_cleanup_stale_tmp_prunes_old_and_keeps_fresh(self) -> None:
        settings = get_settings()
        export_dir = Path(settings.export_tmp_dir)
        chunk_dir = Path(settings.upload_chunk_dir)
        tmp_root = Path(settings.tmp_root)
        inspect_dir = Path(settings.projects_root) / "_inspect_tmp"
        legacy_tmp = Path(self._tmpdir.name) / "legacy_tmp"
        legacy_chunks = legacy_tmp / "sccloud_chunks"

        for d in (export_dir, chunk_dir, inspect_dir, legacy_chunks):
            d.mkdir(parents=True, exist_ok=True)

        # 导出 ZIP: 旧(3h)应删，新(10m)应保留
        old_zip = export_dir / "sccloud_1_20261118_aaaaaaaa.zip"
        new_zip = export_dir / "sccloud_2_20261120_bbbbbbbb.zip"
        old_zip.write_bytes(b"x" * 2048)
        new_zip.write_bytes(b"y" * 2048)
        _set_mtime(old_zip, 3 * 3600)
        _set_mtime(new_zip, 600)

        # 分片目录: 旧(25h)应删，新(1h)应保留，外层旧但内层刚续传(10m)应保留
        old_chunk = chunk_dir / "stale_upload"
        old_chunk.mkdir()
        old_chunk_file = old_chunk / "chunk_000000"
        old_chunk_file.write_bytes(b"a" * 1024)
        _set_mtime(old_chunk_file, 25 * 3600)
        _set_mtime(old_chunk, 25 * 3600)

        fresh_chunk = chunk_dir / "fresh_upload"
        fresh_chunk.mkdir()
        fresh_chunk_file = fresh_chunk / "chunk_000000"
        fresh_chunk_file.write_bytes(b"b" * 1024)
        _set_mtime(fresh_chunk_file, 3600)
        _set_mtime(fresh_chunk, 3600)

        active_retry_chunk = chunk_dir / "retry_upload"
        active_retry_chunk.mkdir()
        retry_file = active_retry_chunk / "chunk_000001"
        retry_file.write_bytes(b"c" * 1024)
        _set_mtime(active_retry_chunk, 25 * 3600)
        _set_mtime(retry_file, 600)

        # tmp_root 直接残留文件（如意外遗留的 TMPDIR 文件）：旧删新留，不影响子目录
        old_stray = tmp_root / "tmp_stray_old"
        new_stray = tmp_root / "tmp_stray_new"
        old_stray.write_bytes(b"s" * 512)
        new_stray.write_bytes(b"s" * 512)
        _set_mtime(old_stray, 3 * 3600)
        _set_mtime(new_stray, 600)

        # _inspect_tmp: 旧删新留
        old_inspect = inspect_dir / "old.rds"
        new_inspect = inspect_dir / "new.rds"
        old_inspect.write_bytes(b"i" * 512)
        new_inspect.write_bytes(b"i" * 512)
        _set_mtime(old_inspect, 3 * 3600)
        _set_mtime(new_inspect, 600)

        # 兼容旧 /tmp：仅删过期 sccloud_*.zip；不误删 3h 内的 sccloud_chunks；删 25h+ 的旧分片子目录
        legacy_old_zip = legacy_tmp / "sccloud_3_20261118.zip"
        legacy_new_zip = legacy_tmp / "sccloud_4_20261120.zip"
        legacy_unrelated = legacy_tmp / "other_service.zip"
        legacy_old_zip.write_bytes(b"z" * 4096)
        legacy_new_zip.write_bytes(b"z" * 1024)
        legacy_unrelated.write_bytes(b"u" * 1024)
        _set_mtime(legacy_old_zip, 3 * 3600)
        _set_mtime(legacy_new_zip, 600)
        _set_mtime(legacy_unrelated, 5 * 3600)

        legacy_recent_chunk = legacy_chunks / "upload_3h"
        legacy_recent_chunk.mkdir()
        (legacy_recent_chunk / "chunk_000000").write_bytes(b"r")
        _set_mtime(legacy_recent_chunk / "chunk_000000", 3 * 3600)
        _set_mtime(legacy_recent_chunk, 3 * 3600)

        legacy_expired_chunk = legacy_chunks / "upload_25h"
        legacy_expired_chunk.mkdir()
        (legacy_expired_chunk / "chunk_000000").write_bytes(b"e" * 1024)
        _set_mtime(legacy_expired_chunk / "chunk_000000", 25 * 3600)
        _set_mtime(legacy_expired_chunk, 25 * 3600)
        _set_mtime(legacy_chunks, 3 * 3600)

        res = DataCleanup().cleanup_stale_tmp(legacy_tmp_dir=str(legacy_tmp))

        # 应清理 6 项：old_zip, old_chunk, old_stray, old_inspect, legacy_old_zip, legacy_expired_chunk
        self.assertEqual(res["removed"], 6)
        self.assertGreater(res["freed_mb"], 0)

        self.assertFalse(old_zip.exists())
        self.assertTrue(new_zip.exists())
        self.assertFalse(old_chunk.exists())
        self.assertTrue(fresh_chunk.exists())
        self.assertTrue(active_retry_chunk.exists())
        self.assertFalse(old_stray.exists())
        self.assertTrue(new_stray.exists())
        self.assertFalse(old_inspect.exists())
        self.assertTrue(new_inspect.exists())

        self.assertFalse(legacy_old_zip.exists())
        self.assertTrue(legacy_new_zip.exists())
        self.assertTrue(legacy_unrelated.exists())
        self.assertTrue(legacy_chunks.is_dir())
        self.assertTrue(legacy_recent_chunk.exists())
        self.assertFalse(legacy_expired_chunk.exists())

    def test_docker_entrypoint_sets_tmpdir(self) -> None:
        entrypoint = Path(__file__).resolve().parents[1] / "scripts" / "docker-entrypoint.sh"
        # 当测试以 uid 0 运行时，entrypoint 会降权到 uid 10001，需确保临时父目录可穿越
        os.chmod(self._tmpdir.name, 0o755)
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DATABASE_URL": "sqlite:///:memory:",
            "JWT_SECRET": "test-secret-value",
            "REDIS_URL": "redis://localhost:6379/0",
            "PROJECTS_ROOT": self.projects_root,
        }

        # 未设置 TMPDIR 时，自动指向 ${PROJECTS_ROOT}/_tmp 并预创建子目录
        proc = subprocess.run(
            [
                "sh",
                str(entrypoint),
                sys.executable,
                "-c",
                "import os, tempfile; print(os.environ.get('TMPDIR', '')); print(tempfile.gettempdir())",
            ],
            env=base_env,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = proc.stdout.strip().splitlines()
        expected_tmp = os.path.join(self.projects_root, "_tmp")
        self.assertEqual(lines, [expected_tmp, expected_tmp])
        self.assertTrue(os.path.isdir(os.path.join(self.projects_root, "users")))
        self.assertTrue(os.path.isdir(os.path.join(expected_tmp, "exports")))
        self.assertTrue(os.path.isdir(os.path.join(expected_tmp, "chunks")))
        self.assertTrue(os.path.isdir(os.path.join(expected_tmp, "inspect")))

        # 显式传入 TMPDIR 时保持用户覆盖
        custom_tmp = os.path.join(self._tmpdir.name, "custom_tmp")
        os.makedirs(custom_tmp, exist_ok=True)
        os.chmod(custom_tmp, 0o777)
        proc2 = subprocess.run(
            [
                "sh",
                str(entrypoint),
                sys.executable,
                "-c",
                "import os, tempfile; print(os.environ.get('TMPDIR', '')); print(tempfile.gettempdir())",
            ],
            env={**base_env, "TMPDIR": custom_tmp},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc2.stdout.strip().splitlines(), [custom_tmp, custom_tmp])


if __name__ == "__main__":
    unittest.main()
