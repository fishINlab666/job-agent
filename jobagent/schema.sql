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

-- 投递记录：代投的副产品
-- 两阶段代投的两半都记在这张表里：
--   prepare 成功 → prefilled（表填好了，等人确认）
--   execute 之后 → submitted / duplicate / failed / closed
--   用户放弃    → abandoned
--   没走到提交  → blocked（岗位关了、要登录、token 失效）
-- 一次投递会先写 prefilled 再更新成终态，所以 confirm_token 上有唯一索引，
-- 保证同一个确认令牌不会落出两条记录来。
CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER NOT NULL REFERENCES jobs(id),
    source_key     TEXT,
    external_id    TEXT,            -- 源站岗位 id，jobs 行被重建也能追溯
    company        TEXT,
    status         TEXT NOT NULL,   -- prefilled / submitted / duplicate /
                                    -- failed / closed / blocked / abandoned
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
