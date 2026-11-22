-- ============================================================
-- 后台管理：账号 / 邀请码 / 登录记录
-- ------------------------------------------------------------
-- 设计原则与 umf_* 一致：这里存"事实"，不存"视图"。
--   · 管理员账号独立于用户体系（朋友是用户，你是管理员，两套权限）
--   · 邀请码一人一个、一次性，绑定期望昵称，注册后回填 used_by
--   · 登录记录只追加不修改，是审计底稿
-- ============================================================

-- 后台管理员。密码用 PBKDF2-HMAC-SHA256（标准库实现，不引 bcrypt 依赖）。
-- salt 与 hash 分开存，迭代次数写进列里：以后调高迭代数不需要迁移旧记录。
CREATE TABLE IF NOT EXISTS admin_users (
    username    TEXT PRIMARY KEY,
    pw_salt     TEXT NOT NULL,
    pw_hash     TEXT NOT NULL,
    iterations  INT  NOT NULL DEFAULT 260000,
    display_name TEXT NOT NULL DEFAULT '',
    is_owner    BOOLEAN NOT NULL DEFAULT false,
    disabled    BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- 后台会话。不放 JWT：这是单人后台，服务端记住一个随机串最省事，
-- 也能"一键踢下线"（删行即可），比吊销列表简单。
CREATE TABLE IF NOT EXISTS admin_sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_exp ON admin_sessions (expires_at);

-- 邀请码：一人一个。
--   nickname 是"这个人来了应该叫什么"，注册时用来对照，防止码被转发给陌生人。
--   used_by 存最终注册成功的 user_id（Mongo 的 _id），方便后台跳到该用户。
CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    nickname    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT 'admin',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    used_at     TIMESTAMPTZ,
    used_by     TEXT,
    used_email  TEXT,
    revoked     BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_invites_state ON invites (revoked, used_at);

-- 登录记录：用户与管理员共用一张表，actor 区分。
--   为什么不只解析 Caddy 日志：日志会轮转、字段是文本、查起来要 grep；
--   这里写成结构化行，后台能直接按人/按时间/按结果筛。
CREATE TABLE IF NOT EXISTS login_events (
    id         BIGSERIAL PRIMARY KEY,
    actor      TEXT NOT NULL DEFAULT 'user',   -- user | admin
    user_id    TEXT,
    email      TEXT,
    ok         BOOLEAN NOT NULL DEFAULT true,
    reason     TEXT NOT NULL DEFAULT '',
    ip         TEXT,
    user_agent TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_login_at ON login_events (at DESC);
CREATE INDEX IF NOT EXISTS idx_login_email ON login_events (email, at DESC);

-- 用户画像评价缓存：对一个人的整体判断（由便宜模型生成，按需刷新）
--   放这里而不是每次现算，是因为"评价"要稳定——同一份数据看两次给出两种判断没有意义。
CREATE TABLE IF NOT EXISTS user_reviews (
    user_id     TEXT PRIMARY KEY,
    summary     TEXT NOT NULL DEFAULT '',
    tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    model       TEXT,
    based_on    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 统计口径快照，便于判断是否过期
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 2026-11 改版：邀请码从"注册门票"变成"通行证"
--   · 第一次使用时由服务端建号，之后凭同一个码登录
--   · 账号的邮箱是生成的（auto_email），密码随机生成后加密回存（pw_enc）
--     否则第二次登录时拿不到密码 —— 朋友那边只有一个码，没有密码可记
--   · 这两列只对新流程有意义；老码（used_at 有值但这两列为空）走邮箱密码登录
-- ----------------------------------------------------------------------------
ALTER TABLE invites ADD COLUMN IF NOT EXISTS auto_email TEXT;
ALTER TABLE invites ADD COLUMN IF NOT EXISTS pw_enc TEXT;
