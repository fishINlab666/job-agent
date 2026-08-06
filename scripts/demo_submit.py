#!/usr/bin/env python3
"""M6 投递器演示脚本 —— 模拟真实投递流程。

此脚本展示如何使用 TencentJoinSubmitter 进行投递，
但不会真正提交表单（dry-run 模式）。
"""
from pathlib import Path
import sys

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from jobagent import db
from jobagent.submitters.tencent_join import TencentJoinSubmitter


def demo_submit_dry_run():
    """演示投递流程（不真正提交）。"""
    print("=" * 60)
    print("M6 投递器演示 - DRY RUN 模式")
    print("=" * 60)

    # 1. 连接数据库，找一个合适的测试岗位
    conn = db.connect()
    row = conn.execute("""
        SELECT external_id, title, company, source_key, apply_url
        FROM jobs
        WHERE source_key = 'tencent_join'
          AND closed_at IS NULL
        LIMIT 1
    """).fetchone()

    if not row:
        print("❌ 数据库中没有腾讯岗位，请先运行：")
        print("   uv run python -m jobagent.cli sync")
        return

    job = dict(row)
    print(f"\n📋 测试岗位：{job['title']}")
    print(f"   公司：{job['company']}")
    print(f"   ID：{job['external_id']}")
    print(f"   链接：{job['apply_url']}")

    # 2. 准备用户画像
    profile = {
        "name": "测试用户",
        "phone": "13800138000",
        "email": "test@example.com",
        "school": "清华大学",
        "major": "计算机科学与技术",
        "degree": "硕士",
        "grad_year": 2027,
        "grad_month": 6,
    }
    print(f"\n👤 用户画像：")
    print(f"   姓名：{profile['name']}")
    print(f"   学校：{profile['school']} - {profile['major']}")
    print(f"   学历：{profile['degree']}（{profile['grad_year']}年{profile['grad_month']}月毕业）")

    # 3. 创建投递器（headless=False 以便观察）
    print(f"\n🤖 初始化投递器...")
    submitter = TencentJoinSubmitter(
        headless=False,  # 有头模式，可观察浏览器操作
        timeout=30.0,
    )
    print(f"   源：{submitter.source_key}")
    print(f"   公司：{submitter.company}")
    print(f"   无头模式：{submitter.headless}")

    # 4. 说明演示流程
    print(f"\n⚠️  DRY RUN 模式说明：")
    print(f"   - 浏览器会打开岗位页面")
    print(f"   - 会点击「立即申请」按钮")
    print(f"   - 会检测是否需要登录")
    print(f"   - 会填充表单字段（如果已登录）")
    print(f"   - 但【不会】真正点击「提交申请」")
    print(f"   - 截图保存在 screenshots/ 目录")

    print(f"\n按 Enter 继续，Ctrl+C 取消...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n已取消")
        return

    # 5. 执行投递（实际会执行，所以这里只展示如何调用）
    print(f"\n🚀 开始投递流程...")
    print(f"   （如需真实投递，请使用 CLI 命令）")
    print(f"   命令示例：")
    print(f"   uv run python -m jobagent.cli apply {job['external_id']} \\")
    print(f"     --profile-path profile.yaml \\")
    print(f"     --no-headless \\")
    print(f"     --user-data-dir ~/.cache/playwright-tencent")

    # 注释掉真实投递，避免误操作
    # result = submitter.submit(job['external_id'], profile)
    # print(f"\n✅ 投递结果：{result.success}")
    # if not result.success:
    #     print(f"   错误：{result.error}")
    # if result.screenshot_path:
    #     print(f"   截图：{result.screenshot_path}")

    print("\n" + "=" * 60)
    print("演示完成")
    print("=" * 60)


if __name__ == "__main__":
    demo_submit_dry_run()
