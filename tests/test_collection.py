from jobagent.adapters.base import RawJob
from jobagent.collection import (
    close_guard_tripped,
    job_fingerprint,
    snapshot_digest,
    to_public_payload,
)


def _job(*, description: str = "old", cities: list[str] | None = None) -> RawJob:
    return RawJob(
        external_id="J1",
        title="算法工程师",
        raw_json={"private_upstream_shape": "must-not-leave-the-collector"},
        job_family="tech",
        raw_category="研发",
        cities=cities or ["上海", "北京"],
        raw_location="上海 北京",
        country="中国",
        department="基础架构",
        recruit_type="campus",
        grad_year="27",
        apply_url="https://example.test/J1",
        apply_system="test",
        description=description,
    )


def test_public_payload_contains_only_approved_public_job_fields() -> None:
    payload = to_public_payload("source", "公司", _job())

    assert payload == {
        "source_key": "source",
        "external_id": "J1",
        "company": "公司",
        "title": "算法工程师",
        "job_family": "tech",
        "raw_category": "研发",
        "cities": ["上海", "北京"],
        "raw_location": "上海 北京",
        "country": "中国",
        "department": "基础架构",
        "recruit_type": "campus",
        "grad_year": "27",
        "apply_url": "https://example.test/J1",
        "apply_system": "test",
        "description": "old",
    }


def test_fingerprint_ignores_description_and_city_order() -> None:
    first = _job(description="old", cities=["上海", "北京"])
    second = _job(description="new", cities=["北京", "上海"])

    assert job_fingerprint(first) == job_fingerprint(second)


def test_snapshot_digest_covers_full_public_payload_in_stable_order() -> None:
    first = to_public_payload("s", "c", _job(description="old"))
    changed = to_public_payload("s", "c", _job(description="new"))
    other = {**first, "external_id": "J0"}

    assert snapshot_digest([first, other]) == snapshot_digest([other, first])
    assert snapshot_digest([first]) != snapshot_digest([changed])
    assert len(snapshot_digest([first])) == 64


def test_close_guard_is_shared_by_local_and_cloud_collectors() -> None:
    assert close_guard_tripped(live_before=10, disappeared=8) is True
    assert close_guard_tripped(live_before=10, disappeared=4) is False
    assert close_guard_tripped(live_before=4, disappeared=3) is False
