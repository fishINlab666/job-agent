"""unsure 分组与整列降级检测测试（issue #7）。

测试 cli._missing_dims / _degraded_dims / _print_unsure 三个辅助函数。
核实点：
- 读库里的列，不解析 _why 文案
- 空城市列表（字符串 "[]"）也算缺失
- 一条岗位的多列状态能同时检查
- 缺失率阈值 0.9 正确计算
- 降级源会被收拢成一行 + 说明
- 按缺失维度数排序（少的排前面）
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobagent import cli, db

runner = CliRunner()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时库，复用 test_cli.py 的模式。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    conn = db.connect()
    db.init(conn)
    db.register_source(
        conn, "test_source", "测试公司", "test_system", "https://test.example.com"
    )
    db.start_run(conn, "test_source")
    conn.commit()
    return conn


class TestMissingDims:
    """_missing_dims() 逐条检测缺失维度。"""

    def test_reads_columns_not_prose(self) -> None:
        """看 grad_year / job_family / cities / recruit_type 列，不解析 _why。"""
        job = {
            "grad_year": None,
            "job_family": "engineer",
            "cities": "[]",
            "recruit_type": "campus",
            "_why": "届别未知",
        }
        missing = cli._missing_dims(job)
        assert "grad_year" in missing  # None 算缺
        assert "cities" in missing      # "[]" 算缺
        assert "job_family" not in missing
        assert "recruit_type" not in missing

    def test_empty_city_list_counts_as_missing(self) -> None:
        """空城市列表（字符串 "[]"）算缺失，不是「不限」。"""
        assert "cities" in cli._missing_dims({"cities": "[]"})
        assert "cities" in cli._missing_dims({"cities": []})
        assert "cities" not in cli._missing_dims({"cities": '["北京"]'})
        assert "cities" not in cli._missing_dims({"cities": ["北京"]})

    def test_checks_all_four_dimensions(self) -> None:
        """一条岗位的四个维度状态能同时检查。"""
        job = {
            "grad_year": None,
            "job_family": None,
            "cities": None,
            "recruit_type": None,
        }
        missing = cli._missing_dims(job)
        assert set(missing) == {"grad_year", "job_family", "cities", "recruit_type"}


class TestDegradedDims:
    """_degraded_dims() 整列降级检测。"""

    def test_ninety_percent_threshold(self, tmp_db) -> None:
        """缺失率 >= 0.9 才算降级。只统计开放岗位。"""
        ts = db.now()
        # 10 条开放，9 条缺 grad_year —— 正好 90%
        for i in range(10):
            tmp_db.execute(
                "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, grad_year) "
                "VALUES ('test_source', ?, '岗位', '测试公司', ?, ?, ?, ?)",
                (f"id{i}", f"fp{i}", ts, ts, None if i < 9 else 2026),
            )
        # 1 条已关闭且缺 grad_year —— 不算进去
        tmp_db.execute(
            "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, grad_year, closed_at) "
            "VALUES ('test_source', 'closed', '已关', '测试公司', 'fp_closed', ?, ?, NULL, '2026-08-01T00:00:00')",
            (ts, ts),
        )
        tmp_db.commit()

        degraded = cli._degraded_dims(tmp_db)
        assert ("test_source", "grad_year") in degraded
        null_count, total = degraded[("test_source", "grad_year")]
        assert null_count == 9
        assert total == 10  # 不含已关闭那条

    def test_below_threshold_not_degraded(self, tmp_db) -> None:
        """89% 缺失不算降级。"""
        ts = db.now()
        # 10 条开放，8 条缺 —— 80%
        for i in range(10):
            tmp_db.execute(
                "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, job_family) "
                "VALUES ('test_source', ?, '岗位', '测试公司', ?, ?, ?, ?)",
                (f"id{i}", f"fp{i}", ts, ts, None if i < 8 else "engineer"),
            )
        tmp_db.commit()

        degraded = cli._degraded_dims(tmp_db)
        assert ("test_source", "job_family") not in degraded


class TestPrintUnsure:
    """_print_unsure() 渲染逻辑。"""

    def test_degraded_sources_collapsed_to_one_line(self, tmp_db, capsys) -> None:
        """整列降级的源收拢成一行 + 说明哪些维度不可得。"""
        ts = db.now()
        # 源 A：10 条岗位，9 条缺 grad_year（降级）
        db.register_source(
            tmp_db, "degraded_src", "A公司", "test_system", "https://a.example.com"
        )
        for i in range(10):
            tmp_db.execute(
                "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, grad_year, closed_at) "
                "VALUES ('degraded_src', ?, '岗位A', 'A公司', ?, ?, ?, ?, NULL)",
                (f"a{i}", f"fp_a{i}", ts, ts, None if i < 9 else 2026),
            )
        # unsure 列表里来自这个源的 5 条
        unsure = [
            {"source_key": "degraded_src", "external_id": f"a{i}",
             "company": "A公司", "title": f"岗位{i}",
             "cities": "[]", "apply_url": "-", "_why": "届别未知"}
            for i in range(5)
        ]
        tmp_db.commit()

        cli._print_unsure(tmp_db, unsure, limit=15)
        out = capsys.readouterr().out
        assert "整列缺失 5 条" in out
        assert "degraded_src" in out
        assert "不可得" in out
        # grad_year 应该出现在不可得列表里
        assert "grad_year" in out
        # 不该逐条打印这 5 条
        assert out.count("岗位0") == 0

    def test_sorts_by_missing_count_fewest_first(self, tmp_db, capsys) -> None:
        """按缺失维度数排序，缺 1 个的排在缺 3 个的前面。"""
        ts = db.now()
        db.register_source(
            tmp_db, "ok_src", "公司", "test_system", "https://ok.example.com"
        )
        # 造 10 条岗位，其中 2 条在 unsure 列表里，确保不触发降级（< 90%）
        for i in range(8):
            tmp_db.execute(
                "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, grad_year, job_family, cities, recruit_type, closed_at) "
                "VALUES ('ok_src', ?, '填充', '公司', ?, ?, ?, 2026, 'engineer', '[\"北京\"]', 'campus', NULL)",
                (f"filler{i}", f"fp_f{i}", ts, ts),
            )
        tmp_db.execute(
            "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, grad_year, job_family, cities, recruit_type, closed_at) "
            "VALUES ('ok_src', 'j1', '缺1个', '公司', 'fp1', ?, ?, 2026, NULL, '[\"上海\"]', 'campus', NULL)",
            (ts, ts),
        )
        tmp_db.execute(
            "INSERT INTO jobs (source_key, external_id, title, company, fingerprint, first_seen_at, last_seen_at, grad_year, job_family, cities, recruit_type, closed_at) "
            "VALUES ('ok_src', 'j2', '缺3个', '公司', 'fp2', ?, ?, NULL, NULL, '[]', 'campus', NULL)",
            (ts, ts),
        )
        tmp_db.commit()

        unsure = [
            {"source_key": "ok_src", "external_id": "j2",
             "company": "公司", "title": "缺3个",
             "grad_year": None, "job_family": None, "cities": "[]",
             "recruit_type": "campus", "apply_url": "-", "_why": "xx"},
            {"source_key": "ok_src", "external_id": "j1",
             "company": "公司", "title": "缺1个",
             "grad_year": 2026, "job_family": None, "cities": "[\"上海\"]",
             "recruit_type": "campus", "apply_url": "-", "_why": "xx"},
        ]

        cli._print_unsure(tmp_db, unsure, limit=15)
        out = capsys.readouterr().out
        # 不该收拢，应该逐条打印
        assert "整列缺失" not in out
        # 缺1个的应该排在缺3个的前面
        idx_1 = out.index("缺1个")
        idx_3 = out.index("缺3个")
        assert idx_1 < idx_3
