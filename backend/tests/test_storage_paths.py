"""项目与用户落盘路径命名规则（方案 A）及老新共存兼容测试。"""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.admin.router import delete_user
from app.config import get_settings
from app.convert.router import download_converted, upload_for_convert
from app.db import models
from app.projects.router import (
    ProjectCreate,
    create_project,
    delete_project,
    download_project,
)
from app.upload.router import (
    complete_upload,
    init_upload,
    inspect_file,
    inspect_file_by_path,
    upload_chunk,
)
from app.utils.data_cleanup import DataCleanup
from app.utils.storage_paths import (
    cleanup_empty_user_dir,
    is_path_owned_by_user,
    is_path_within,
    project_storage_dir,
    sanitize_username,
    user_dir_name,
)


class TestStoragePaths(unittest.IsolatedAsyncioTestCase):
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

    def _create_user(
        self,
        *,
        user_id: int | None = None,
        username: str = "zhouy1",
        role: str = "admin",
        is_guest: bool = False,
    ) -> models.User:
        db = self.TestSession()
        try:
            kwargs = dict(
                username=username,
                email=f"{username}@example.com",
                password_hash="not-a-real-hash",
                role=role,
                is_guest=is_guest,
                is_active=True,
                max_projects=10,
                total_quota=100,
                used_quota=0,
            )
            if user_id is not None:
                kwargs["id"] = user_id
            user = models.User(**kwargs)
            db.add(user)
            db.commit()
            db.refresh(user)
            return user
        finally:
            db.close()

    def test_user_dir_name_sanitizes_and_is_unique(self) -> None:
        self.assertEqual(user_dir_name(6, "admin"), "admin_u6")
        self.assertEqual(user_dir_name(15, "zhouy1"), "zhouy1_u15")
        self.assertEqual(user_dir_name(19, "guest_a1b2c3d4"), "guest_a1b2c3d4_u19")
        self.assertEqual(user_dir_name(20, "../evil/name"), "evil_name_u20")
        self.assertEqual(user_dir_name(21, "///"), "user_u21")
        self.assertEqual(len(sanitize_username("a" * 80)), 48)
        with self.assertRaises(ValueError):
            project_storage_dir(self.projects_root, 1, "alice", "../escape")

    async def test_create_and_delete_project_uses_scheme_a_and_keeps_users_root(self) -> None:
        user = self._create_user(user_id=15, username="zhouy1")
        db = self.TestSession()
        try:
            proj = await create_project(
                ProjectCreate(name="PBMC_3k", species="human"),
                current_user=user,
                db=db,
            )
        finally:
            db.close()

        expected_user_dir = os.path.join(self.projects_root, "users", "zhouy1_u15")
        expected_proj_dir = os.path.join(expected_user_dir, "PBMC_3k")
        self.assertEqual(proj.storage_path, expected_proj_dir)
        self.assertTrue(os.path.isdir(expected_proj_dir))

        db = self.TestSession()
        try:
            await delete_project(proj.id, current_user=user, db=db)
        finally:
            db.close()

        self.assertFalse(os.path.exists(expected_proj_dir))
        self.assertFalse(os.path.exists(expected_user_dir))
        # 顶层 users/ 与 projects_root 绝不被误删
        self.assertTrue(os.path.isdir(os.path.join(self.projects_root, "users")))
        self.assertTrue(os.path.isdir(self.projects_root))

    async def test_legacy_and_new_projects_coexist(self) -> None:
        user = self._create_user(user_id=6, username="admin")
        legacy_dir = os.path.join(self.projects_root, "6", "0903")
        os.makedirs(legacy_dir, exist_ok=True)
        (Path(legacy_dir) / "qc_plot.png").write_bytes(b"legacy-png")

        db = self.TestSession()
        try:
            legacy_proj = models.Project(
                user_id=user.id,
                name="0903",
                species="human",
                storage_path=legacy_dir,
            )
            db.add(legacy_proj)
            db.commit()
            db.refresh(legacy_proj)

            new_proj = await create_project(
                ProjectCreate(name="PBMC_new", species="human"),
                current_user=user,
                db=db,
            )
            (Path(new_proj.storage_path) / "qc_new.png").write_bytes(b"new-png")

            # 老项目与新项目均可正常导出
            bg1 = BackgroundTasks()
            resp_legacy = await download_project(legacy_proj.id, bg1, current_user=user, db=db)
            bg2 = BackgroundTasks()
            resp_new = await download_project(new_proj.id, bg2, current_user=user, db=db)
            self.assertTrue(os.path.isfile(resp_legacy.path))
            self.assertTrue(os.path.isfile(resp_new.path))
            await bg1()
            await bg2()

            # 删除老项目会清掉空的 /6，但不影响新项目 users/admin_u6/PBMC_new
            await delete_project(legacy_proj.id, current_user=user, db=db)
        finally:
            db.close()

        self.assertFalse(os.path.exists(os.path.join(self.projects_root, "6")))
        self.assertTrue(os.path.isdir(new_proj.storage_path))

    async def test_upload_without_project_and_inspect_use_new_paths(self) -> None:
        user = self._create_user(user_id=19, username="guest_a1b2c3d4", role="admin", is_guest=True)

        init_resp = await init_upload(filename="demo.rds", file_size=5, current_user=user)
        await upload_chunk(
            upload_id=init_resp.upload_id,
            chunk_index=0,
            chunk=UploadFile(filename="p0", file=io.BytesIO(b"hello")),
            current_user=user,
        )

        # inspect 应写入 _tmp/inspect/ 并在 finally 自动清理
        observed_paths: list[str] = []

        async def fake_inspect(file_path: str, filename: str) -> dict:
            observed_paths.append(file_path)
            self.assertTrue(os.path.isfile(file_path))
            return {
                "filename": filename,
                "n_cells": 10,
                "n_genes": 20,
                "n_rows": 10,
                "n_cols": 20,
                "genes": ["CD3D"],
                "gene_ids": ["ENSG00000167286"],
                "file_size_mb": 0.01,
                "metadata_columns": [],
                "samples": [],
                "ensembl_version": "unknown",
            }

        with patch("app.upload.router._call_r_engine_inspect", new=AsyncMock(side_effect=fake_inspect)):
            inspect_resp = await inspect_file(upload_id=init_resp.upload_id, current_user=user)
        self.assertEqual(inspect_resp.n_cells, 10)
        self.assertEqual(len(observed_paths), 1)
        self.assertTrue(
            is_path_within(observed_paths[0], get_settings().inspect_tmp_dir),
            observed_paths[0],
        )
        self.assertFalse(os.path.exists(observed_paths[0]))

        # 未绑定 project_id 时，合并文件落在 users/{username}_u{id}/_uploads/
        complete_resp = await complete_upload(
            upload_id=init_resp.upload_id,
            project_id=None,
            sample_groups=None,
            current_user=user,
        )
        expected_uploads = os.path.join(
            self.projects_root, "users", "guest_a1b2c3d4_u19", "_uploads"
        )
        self.assertTrue(is_path_within(complete_resp.path, expected_uploads), complete_resp.path)
        self.assertEqual(Path(complete_resp.path).read_bytes(), b"hello")

    async def test_convert_upload_and_download_security_and_compat(self) -> None:
        user1 = self._create_user(user_id=1, username="alice", role="user")
        user15 = self._create_user(user_id=15, username="alice_other", role="user")

        # 新上传转换文件落在 users/alice_u1/_convert/
        up_resp = await upload_for_convert(
            file=UploadFile(filename="data.rds", file=io.BytesIO(b"rds-bytes")),
            current_user=user1,
        )
        expected_convert = os.path.join(self.projects_root, "users", "alice_u1", "_convert")
        self.assertTrue(is_path_within(up_resp["path"], expected_convert), up_resp["path"])
        dl_new = await download_converted(up_resp["path"], current_user=user1)
        self.assertEqual(os.path.realpath(dl_new.path), os.path.realpath(up_resp["path"]))

        # 兼容老路径 /{id}/_convert/
        legacy_file = Path(self.projects_root) / "1" / "_convert" / "legacy.rds"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_bytes(b"old")
        dl_legacy = await download_converted(str(legacy_file), current_user=user1)
        self.assertEqual(os.path.realpath(dl_legacy.path), os.path.realpath(legacy_file))

        # 兼容游客升级改名后留下的 users/guest_deadbeef_u1/
        upgraded_guest_file = (
            Path(self.projects_root) / "users" / "guest_deadbeef_u1" / "_convert" / "g.rds"
        )
        upgraded_guest_file.parent.mkdir(parents=True, exist_ok=True)
        upgraded_guest_file.write_bytes(b"guest")
        dl_guest = await download_converted(str(upgraded_guest_file), current_user=user1)
        self.assertEqual(os.path.realpath(dl_guest.path), os.path.realpath(upgraded_guest_file))

        # 拦截前缀碰撞（user_id=1 不得访问 /15/ 或 users/alice_other_u15/）
        other_legacy = Path(self.projects_root) / "15" / "_convert" / "secret.rds"
        other_legacy.parent.mkdir(parents=True, exist_ok=True)
        other_legacy.write_bytes(b"secret15")
        other_new = Path(self.projects_root) / "users" / "alice_other_u15" / "_convert" / "s.rds"
        other_new.parent.mkdir(parents=True, exist_ok=True)
        other_new.write_bytes(b"secret_new")

        # 拦截符号链接逃逸
        outside = Path(self._tmpdir.name) / "outside_secret.txt"
        outside.write_text("top-secret", encoding="utf-8")
        escape_link = Path(expected_convert) / "escape.rds"
        os.symlink(outside, escape_link)

        for forbidden in (str(other_legacy), str(other_new), str(escape_link)):
            with self.assertRaises(HTTPException) as ctx:
                await download_converted(forbidden, current_user=user1)
            self.assertEqual(ctx.exception.status_code, 403, forbidden)

        # user15 可正常访问自己的文件
        dl_u15 = await download_converted(str(other_new), current_user=user15)
        self.assertEqual(os.path.realpath(dl_u15.path), os.path.realpath(other_new))

    async def test_inspect_file_by_path_blocks_sibling_prefix_collision(self) -> None:
        user1 = self._create_user(user_id=1, username="alice")
        user2 = self._create_user(user_id=2, username="bob")

        proj_dir = Path(self.projects_root) / "users" / "alice_u1" / "proj"
        sibling_dir = Path(self.projects_root) / "users" / "alice_u1" / "proj_other"
        proj_dir.mkdir(parents=True, exist_ok=True)
        sibling_dir.mkdir(parents=True, exist_ok=True)
        sibling_file = sibling_dir / "secret.rds"
        sibling_file.write_bytes(b"nope")

        db = self.TestSession()
        try:
            db.add(
                models.Project(
                    user_id=user1.id,
                    name="proj",
                    species="human",
                    storage_path=str(proj_dir),
                )
            )
            db.add(
                models.Project(
                    user_id=user2.id,
                    name="proj_other",
                    species="human",
                    storage_path=str(sibling_dir),
                )
            )
            db.commit()
        finally:
            db.close()

        with self.assertRaises(HTTPException) as ctx:
            await inspect_file_by_path(file_path=str(sibling_file), current_user=user1)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_admin_delete_user_cleans_new_and_legacy_dirs(self) -> None:
        admin = self._create_user(user_id=1, username="admin", role="admin")
        target = self._create_user(user_id=19, username="alice", role="user")
        keep = self._create_user(user_id=20, username="bob", role="user")

        new_dir = Path(self.projects_root) / "users" / "alice_u19"
        guest_dir = Path(self.projects_root) / "users" / "guest_12345678_u19"
        legacy_dir = Path(self.projects_root) / "19"
        keep_dir = Path(self.projects_root) / "users" / "bob_u20" / "keep_proj"
        for d in (new_dir / "_convert", guest_dir / "old_proj", legacy_dir / "_uploads", keep_dir):
            d.mkdir(parents=True, exist_ok=True)
        (new_dir / "_convert" / "a.rds").write_bytes(b"a")
        (guest_dir / "old_proj" / "b.rds").write_bytes(b"b")
        (legacy_dir / "_uploads" / "c.rds").write_bytes(b"c")
        (keep_dir / "keep.rds").write_bytes(b"k")

        db = self.TestSession()
        try:
            db.add(
                models.Project(
                    user_id=target.id,
                    name="old_proj",
                    species="human",
                    storage_path=str(guest_dir / "old_proj"),
                )
            )
            db.commit()
            await delete_user(target.id, admin=admin, db=db)
        finally:
            db.close()

        self.assertFalse(new_dir.exists())
        self.assertFalse(guest_dir.exists())
        self.assertFalse(legacy_dir.exists())
        self.assertTrue((keep_dir / "keep.rds").is_file())
        self.assertTrue((Path(self.projects_root) / "users").is_dir())
        self.assertIsNotNone(keep)

    def test_cleanup_expired_projects_and_inspect_tmp(self) -> None:
        guest = self._create_user(user_id=25, username="guest_9999aaaa", role="guest", is_guest=True)
        proj_dir = Path(self.projects_root) / "users" / "guest_9999aaaa_u25" / "tmp_proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "data.rds").write_bytes(b"x" * 1024)

        db = self.TestSession()
        try:
            db.add(
                models.Project(
                    user_id=guest.id,
                    name="tmp_proj",
                    species="human",
                    storage_path=str(proj_dir),
                    created_at=datetime.utcnow() - timedelta(days=3),
                )
            )
            db.commit()
        finally:
            db.close()

        # 新 _tmp/inspect 与旧 _inspect_tmp 过期文件都应被清理
        new_inspect = Path(get_settings().inspect_tmp_dir)
        legacy_inspect = Path(self.projects_root) / "_inspect_tmp"
        new_inspect.mkdir(parents=True, exist_ok=True)
        legacy_inspect.mkdir(parents=True, exist_ok=True)
        stale_new = new_inspect / "stale.rds"
        stale_legacy = legacy_inspect / "stale.rds"
        stale_new.write_bytes(b"1")
        stale_legacy.write_bytes(b"2")
        old_ts = time.time() - 3 * 3600
        os.utime(stale_new, (old_ts, old_ts))
        os.utime(stale_legacy, (old_ts, old_ts))

        dc = DataCleanup()
        with patch("app.utils.data_cleanup.SessionLocal", self.TestSession):
            res = dc.cleanup_now()
        self.assertEqual(res["deleted"], 1)
        self.assertFalse(proj_dir.exists())
        self.assertFalse(proj_dir.parent.exists())
        self.assertTrue((Path(self.projects_root) / "users").is_dir())
        self.assertFalse(stale_new.exists())
        self.assertFalse(stale_legacy.exists())

        # 防御性检查：cleanup_empty_user_dir 永不删除 projects_root 或 users/
        cleanup_empty_user_dir(os.path.join(self.projects_root, "users", "ghost"), self.projects_root)
        self.assertTrue((Path(self.projects_root) / "users").is_dir())
        self.assertFalse(is_path_owned_by_user("/etc/passwd", self.projects_root, 1, "admin"))


if __name__ == "__main__":
    unittest.main()
