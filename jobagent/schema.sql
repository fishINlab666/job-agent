-- 校招 Agent 契约层 schema
-- 设计要点：
--   1. 一家公司可能有多个招聘入口（腾讯 = careers.tencent.com + join.qq.com），
--      所以主键是 source_key 而不是 company。
--   2. snapshots 保留每次抓取的原文，漏报排查全靠它，不要省。
--   3. events 是 M4 的唯一输入。推送逻辑只读 events，不读 jobs。

-- 数据源：一个 source_key = 一个公司的一个招聘入口
CREATE TABLE IF NOT EXISTS sources (
    source_key  TEXT PRIMARY KEY,          -- tencent_careers / tencent_join
    company     TEXT NOT NULL,             -- 腾讯
    -- 招聘系统的厂商 key，取值必须是 ats.py VENDORS 里的（feishu / mokahr /
    -- tencent_join / ...）。原来注释里举的 "self_built" 是错的示例：那是一类系统的
    -- 形容词，不是某个系统，按它去路由等于把所有自建公司挤到同一个键上。
    system      TEXT,
    -- 租户。system 是多租户 SaaS 时才有意义（feishu:xiaopeng 的 xiaopeng）。
    -- 大多数情况能从 apply_url 里自动取到，这一列是给取不到的兜底：
    -- 北森/Moka 的租户页格式还没实测，可能不在 URL 里，只能人工配。
    tenant      TEXT,
    -- 这家公司最多接受投几个岗位（校招常见 1~3 个）。NULL = 不限。
    -- 只有人工来源：源站不声明自己的限投数。拿不到就留空，不许默认一个数——
    -- 猜的上限比真上限大会白拦，比真上限小会照样投穿，两种都不如不猜。
    -- 计数按 company 而不是 source_key，见 db.quota_state。
    apply_limit INTEGER,
    entry_url   TEXT,
    notes       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1
);

-- 抓取批次
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key  TEXT NOT NULL REFERENCES sources(source_key),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',   -- running / ok / partial / failed
    fetched     INTEGER DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source_key, started_at DESC);

-- 原始快照：每条岗位每次抓取的原文
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    source_key  TEXT NOT NULL,
    external_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    raw_json    TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_run ON snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_snap_job ON snapshots(source_key, external_id, captured_at DESC);

-- 岗位当前态
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key    TEXT NOT NULL REFERENCES sources(source_key),
    external_id   TEXT NOT NULL,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    job_family    TEXT,        -- 归一后：tech/product/operations/design/marketing/sales/hr/finance/legal/other
    raw_category  TEXT,        -- 源站原值，保留用于校准归一规则
    cities        TEXT,        -- JSON array，归一后的中文城市名
    raw_location  TEXT,
    country       TEXT,
    department    TEXT,        -- BG / 事业群
    recruit_type  TEXT,        -- campus / intern / social
    grad_year     TEXT,        -- 届别，抽不到就是 NULL
    apply_url     TEXT,
    apply_system  TEXT,        -- M6 路由依据：workday / tencent_self / ...
    description   TEXT,
    fingerprint   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    closed_at     TEXT,        -- 从源站消失的时刻
    UNIQUE(source_key, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_open ON jobs(closed_at, recruit_type, job_family);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company, recruit_type);

-- 事件流：M2 的产出，M4 的唯一输入
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,   -- job_opened / job_closed / job_updated
                                 -- family_first_seen: 某公司某岗位族从 0 变成非 0（最有价值的信号）
                                 -- batch_started: 单日新增量突增，疑似批次启动
    source_key  TEXT,
    company     TEXT,
    job_id      INTEGER REFERENCES jobs(id),
    payload     TEXT,            -- JSON：字段级 diff、批次规模等
    occurred_at TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(id),
    notified_at TEXT             -- NULL = 还没推给用户
);
CREATE INDEX IF NOT EXISTS idx_events_pending ON events(notified_at, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, occurred_at DESC);

-- 自动观察：一轮包含五个源；runs 仍保存每个源的底层采集证据，这两张表只负责
-- 把它们收成用户能判断的「今天这轮是否完整、官网是否已经核对」。
CREATE TABLE IF NOT EXISTS observation_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    observed_date TEXT NOT NULL,
    trigger     TEXT NOT NULL,       -- manual / scheduled
    slot        TEXT NOT NULL,       -- manual / 09:30 / 14:30 / 20:30
    is_workday  INTEGER NOT NULL,    -- 周一至周五为 1；法定节假日由验收时排除
    on_time     INTEGER NOT NULL,    -- 在计划时刻后 60 分钟内启动才算该时段有效
    status      TEXT NOT NULL        -- running / ok / partial / failed
);
CREATE INDEX IF NOT EXISTS idx_observation_date
    ON observation_batches(observed_date, slot);

-- 计划时段的唯一占位。单独建表而不是在历史批次上加 UNIQUE：早期候选允许同一
-- 时段重跑，直接建唯一索引会让带旧数据的数据库连迁移都无法启动。
CREATE TABLE IF NOT EXISTS observation_slot_claims (
    observed_date TEXT NOT NULL,
    slot          TEXT NOT NULL,
    observation_id INTEGER NOT NULL UNIQUE REFERENCES observation_batches(id),
    PRIMARY KEY(observed_date, slot)
);

CREATE TABLE IF NOT EXISTS observation_sources (
    observation_id INTEGER NOT NULL REFERENCES observation_batches(id),
    source_key     TEXT NOT NULL,
    company        TEXT NOT NULL,
    run_id         INTEGER REFERENCES runs(id),
    status         TEXT NOT NULL,    -- ok / partial / failed
    bootstrap      INTEGER NOT NULL DEFAULT 0,
    fetched        INTEGER NOT NULL DEFAULT 0,
    opened         INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    closed         INTEGER NOT NULL DEFAULT 0,
    change_count   INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    truth_status   TEXT NOT NULL DEFAULT 'pending', -- pending / verified / mismatch
    missed_count   INTEGER,
    false_positive_count INTEGER,
    unverified_change_count INTEGER,
    checked_at     TEXT,
    truth_note     TEXT,
    PRIMARY KEY(observation_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_observation_truth
    ON observation_sources(truth_status, observation_id);

-- 本机通知也是观察闭环的一部分。skipped 表示按策略不打扰，不等于发送失败。
CREATE TABLE IF NOT EXISTS observation_notifications (
    observation_id INTEGER PRIMARY KEY REFERENCES observation_batches(id),
    policy         TEXT NOT NULL, -- failure / changes / daily-complete / daily-incomplete / no-change
    status         TEXT NOT NULL, -- sent / skipped / failed
    attempted_at   TEXT NOT NULL,
    error          TEXT
);

-- 独立官方接口生成的待审候选。它不能代签最终真值，每轮每源只保存一次。
CREATE TABLE IF NOT EXISTS observation_truth_candidates (
    observation_id INTEGER NOT NULL,
    source_key     TEXT NOT NULL,
    status         TEXT NOT NULL, -- captured / failed / skipped
    official_url  TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    official_ids_json TEXT,
    official_ids_sha256 TEXT,
    error          TEXT,
    PRIMARY KEY(observation_id, source_key),
    FOREIGN KEY(observation_id, source_key)
        REFERENCES observation_sources(observation_id, source_key)
);

-- 独立官网核对证据。保存完整官方岗位编号清单及本轮变化事件清单，不能只靠人手填
-- “漏报=0/误报=0”代签。每个源每轮只允许一份，不覆盖历史结论。
CREATE TABLE IF NOT EXISTS observation_truth_evidence (
    observation_id INTEGER NOT NULL,
    source_key     TEXT NOT NULL,
    official_url  TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    reviewer      TEXT NOT NULL,
    official_ids_json TEXT NOT NULL,
    official_ids_sha256 TEXT NOT NULL,
    system_ids_sha256 TEXT NOT NULL,
    missed_ids_json TEXT NOT NULL,
    false_positive_ids_json TEXT NOT NULL,
    change_event_ids_json TEXT NOT NULL,
    verified_event_ids_json TEXT NOT NULL,
    unverified_event_ids_json TEXT NOT NULL,
    unexpected_event_ids_json TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    note          TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL UNIQUE,
    PRIMARY KEY(observation_id, source_key),
    FOREIGN KEY(observation_id, source_key)
        REFERENCES observation_sources(observation_id, source_key)
);

-- 投递记录：代投的副产品
-- 代投状态链全部记在同一行：
--   防重/额度占位 → reserved
--   prepare 成功  → prefilled（表填好了，等人确认）
--   execute 开始  → submitting
--   execute 之后  → submitted / duplicate / unknown / closed
--   用户放弃    → abandoned
--   没走到提交  → blocked（岗位关了、要登录、token 失效）
-- 一次投递会先写 prefilled 再更新成终态，所以 confirm_token 上有唯一索引，
-- 保证同一个确认令牌不会落出两条记录来。
CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_submission_id INTEGER,      -- 遗留 submissions.id；迁移去重的稳定身份
    job_id         INTEGER NOT NULL REFERENCES jobs(id),
    source_key     TEXT,
    external_id    TEXT,            -- 源站岗位 id，jobs 行被重建也能追溯
    company        TEXT,
    status         TEXT NOT NULL,   -- reserved / prefilled / submitting /
                                    -- submitted / duplicate / unknown / failed /
                                    -- closed / blocked / abandoned
    submitted_at   TEXT,
    filled_fields  TEXT,            -- JSON：实际填了什么（敏感值已打码），便于复投和排错
    skipped_fields TEXT,            -- JSON：没把握留空的字段（半自动降级用）
    error          TEXT,
    screenshot_path TEXT,           -- 提交那一刻的页面存证
    confirm_token  TEXT,            -- 关联 prepare 与 execute 的同一次确认
    prepared_at    TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_apps_job ON applications(job_id);
-- idx_apps_status 和 idx_apps_token 不在这里建，见 db.migrate()。
-- 它们引用的是 APPLICATION_COLUMNS 里补的列（created_at / confirm_token），
-- 而 init() 是先跑这个文件、后跑 migrate()。老库缺列时这两条会在这里
-- 直接报 `no such column`，整个 init 挂掉——不是索引没建上而已。
-- 新库走到 migrate() 时表已经是全的，那边的 IF NOT EXISTS 照样幂等。
