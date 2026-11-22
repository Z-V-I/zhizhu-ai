-- UMF 真相源。原则：Mem0 只是索引，这里才是"记忆资产"本身。
-- 所以每张表都必须能完整导出成 umf.json，不依赖任何外部系统的内部结构。

CREATE EXTENSION IF NOT EXISTS vector;

-- 用户画像：结构化、可手改、永远常驻上下文
CREATE TABLE IF NOT EXISTS umf_profiles (
    user_id    TEXT PRIMARY KEY,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    version    INT   NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 记忆条目
-- layer 沿用 LobeHub v2 的五层模型（identity/preference/experience/activity/context）：
--   identity   我是谁（职业、城市、家庭、称呼）—— 最稳，几乎不变，所有 Agent 都能读
--   preference 我喜欢怎样（沟通风格、格式、禁忌）—— 决定"像不像懂你"
--   experience 我做过什么、踩过什么坑（可复用的经验）—— 技术顾问专属价值
--   activity   我正在做什么（项目、计划、进行中的事）—— 有时效，需要 valid_to
--   context    某次对话里的临时背景 —— 只活在 topic 级，不进 global
-- 分层比扁平 kind 更能表达人格，也是时间线"观点何时改变"的索引维度。
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'global',   -- global | agent | topic
    agent_id      TEXT,
    topic_id      TEXT,
    layer         TEXT NOT NULL DEFAULT 'context',  -- identity|preference|experience|activity|context
    kind          TEXT NOT NULL DEFAULT 'fact',     -- 兼容旧数据：fact|preference|decision|relation|event
    text          TEXT NOT NULL,
    entities      JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- 1024 维：对应 models.json 里的 text-embedding-v3（dashscope）。
    -- 换 embedding 供应商时这里必须一起改，否则插入直接报维度不匹配。
    embedding     vector(1024),
    valid_from    DATE,
    valid_to      DATE,
    supersedes_id TEXT,
    source        JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence    REAL  NOT NULL DEFAULT 0.8,
    import_batch  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mem_user_scope
    ON memories (user_id, scope, agent_id)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mem_created
    ON memories (user_id, created_at DESC)
    WHERE deleted_at IS NULL;
-- 按层取记忆：注入时先拿 identity+preference（短、必带），再按需检索其余三层
CREATE INDEX IF NOT EXISTS idx_mem_layer
    ON memories (user_id, layer, scope)
    WHERE deleted_at IS NULL;
-- 没有 embedding 时用全文检索兜底；有 embedding 时用 HNSW
CREATE INDEX IF NOT EXISTS idx_mem_text
    ON memories USING gin (to_tsvector('simple', text));

-- 时间线：一条事件一行，前端直接按 at 排序就是 Memory Timeline
CREATE TABLE IF NOT EXISTS memory_events (
    id      BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    type    TEXT NOT NULL,          -- memory_added|preference_change|topic_start|import
    ref_id  TEXT,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_user_at ON memory_events (user_id, at DESC);

-- 滚动摘要：会话超过预算后，老对话压成一段，按会话缓存，避免每轮重算
-- covered_message_ids 是为了"压缩可见"：记忆中心里能展开看"哪几条被压成了这句"。
-- 网关层压缩对前端透明，用户看不见丢掉了什么，这张表就是唯一的证据。
CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id   TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    up_to_message_id  TEXT,
    covered_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    covered_messages  INT NOT NULL DEFAULT 0,
    summary           TEXT NOT NULL,
    model             TEXT,
    tokens_before     INT NOT NULL DEFAULT 0,
    tokens            INT NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 话题工作区（L2 注入层用）
CREATE TABLE IF NOT EXISTS topics (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    summary    TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 导入批次：整批可回滚
CREATE TABLE IF NOT EXISTS import_batches (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    platform   TEXT NOT NULL,
    file_name  TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    stats      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS import_items (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     TEXT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    conv_id      TEXT,
    msg_id       TEXT,
    action       TEXT NOT NULL DEFAULT 'add',
    UNIQUE (batch_id, content_hash)
);

-- 审计：谁在什么时候读了谁的记忆（串号事故的第一现场证据）
CREATE TABLE IF NOT EXISTS audit_log (
    id      BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    action  TEXT NOT NULL,
    target  TEXT,
    meta    JSONB NOT NULL DEFAULT '{}'::jsonb,
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 厂商注册表：用户切"厂商"，管理员决定"这家用哪个模型"
-- ------------------------------------------------------------
-- 为什么不用 models.json 静态配置：
--   静态配置改一次模型就要改文件 + 重启容器，只有管理员一个人用，
--   但"看模型列表、比较、切换"这件事每天都会发生。放数据库里才能配面板。
-- 密钥列存的是 Fernet 密文（f: 前缀）或明文（p: 前缀，无 SECRET_KEY 时降级）。
-- ============================================================
CREATE TABLE IF NOT EXISTS providers (
    code            TEXT PRIMARY KEY,               -- 同时也是暴露给前端的 model id
    label           TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'openai', -- openai | gemini
    base_url        TEXT NOT NULL,
    list_url        TEXT,                           -- 拉模型列表用；空则 base_url + /models
    api_key_enc     TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT false,
    model_id        TEXT,                           -- 管理员选定的真实模型
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_fetch_at   TIMESTAMPTZ,
    last_fetch_error TEXT,
    sort            INT NOT NULL DEFAULT 100
);

-- 拉取过的模型列表缓存，只用于管理面板展示
CREATE TABLE IF NOT EXISTS provider_models (
    provider_code TEXT NOT NULL REFERENCES providers(code) ON DELETE CASCADE,
    model_id      TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_code, model_id)
);

-- 会话当前用的是哪个厂商：检测"聊天中途切模型"的唯一依据
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    model           TEXT,
    prev_model      TEXT,
    switch_count    INT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 用户偏好（键值对）。目前只存一件东西：vendor = 上一次在聊天里选的厂商。
-- 智能体对话不显示模型选择器，网关靠它把"你选的厂商"延续到智能体里，
-- 这样"换智能体"= 换回复风格，而不是换模型。
CREATE TABLE IF NOT EXISTS umf_prefs (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

-- 头像托管表：R2 只存对象，URL 和归属还是记在这里，
-- 这样换 bucket / 换域名时只要改这张表，不用回头翻聊天记录。
CREATE TABLE IF NOT EXISTS umf_avatars (
    key        TEXT PRIMARY KEY,          -- 对象键：u/{user_id}/{hash}.png 或 a/{agent_id}.svg
    owner_id   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'user',  -- user | agent
    url        TEXT NOT NULL,
    bytes      INT  NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_avatars_owner ON umf_avatars (owner_id, kind);

-- ============================================================================
-- 长期记忆的"原料层"：每个历史对话一行
-- ----------------------------------------------------------------------------
-- 为什么单独一张表，而不是每次现算：
--   · 话题聚类、时间线、画像都要读同一份"这段聊了什么"
--   · 现算就要反复调模型，414 个对话一次几十分钟，用户等不起
--   · 存下来之后，重跑只处理 status <> 'done' 的行，天然可断点续跑
-- conversation_id 做主键 —— 同一对话重跑是覆盖，不会长出重复记录。
-- ============================================================================
CREATE TABLE IF NOT EXISTS conv_digests (
    conversation_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    one_line        TEXT NOT NULL DEFAULT '',   -- 这段聊了什么，一句话（话题/时间线的最小单位）
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    msg_count       INT NOT NULL DEFAULT 0,
    user_msg_count  INT NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    memories        INT NOT NULL DEFAULT 0,     -- 这个对话贡献了几条记忆
    topic_id        TEXT,
    status          TEXT NOT NULL DEFAULT 'done',  -- done | empty | failed
    error           TEXT,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_digest_user_at ON conv_digests (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_digest_topic ON conv_digests (user_id, topic_id);

-- 记忆整理任务：一次 backfill 一行。前端轮询这张表看进度，
-- 不依赖进程内变量 —— 重建容器 / 多 worker 时进度不会丢。
CREATE TABLE IF NOT EXISTS memory_jobs (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'backfill',
    status     TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | canceled
    stage      TEXT NOT NULL DEFAULT '',         -- 正在做哪一步（人话）
    total      INT NOT NULL DEFAULT 0,
    done       INT NOT NULL DEFAULT 0,
    stats      JSONB NOT NULL DEFAULT '{}'::jsonb,
    error      TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON memory_jobs (user_id, started_at DESC);

-- 话题表的运行期扩展：话题要能回答"什么时候开始、什么时候停、包含哪些会话"
ALTER TABLE topics ADD COLUMN IF NOT EXISTS ended_at   TIMESTAMPTZ;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS conv_ids   JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS conv_count INT NOT NULL DEFAULT 0;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS keywords   JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ---------------------------------------------------------------------------
-- 用户对记忆内容的「更正」
--
-- 为什么单独一张表、而不是只改 memories 那一行：
-- 用户编辑或删除一条记忆，不只是在改数据 —— 他是在**裁决这条信息对不对**。
-- 这个裁决有三个用途，缺一不可：
--   ① 审计：时间线上要看得到"他什么时候纠正了什么"，站长也要看得到。
--   ② 重新生成时优先采纳：整理跑一遍会把错误信息从原始对话里再抽出来，
--      所以 prompt 里必须带上"本人已明确否认"的清单。
--   ③ 防止复活：删除 ≠ 不再抽取。对话里确实讲过"某某是创始人"，
--      下一轮 backfill 仍会抽出同样一句。写入前要拿这条否决记录去比对。
--
-- 只记文本不记向量：向量在写入时现算（否决记录一般只有几十条，成本可忽略），
-- 存向量反而会在换 embedding 模型时全部失效。
CREATE TABLE IF NOT EXISTS memory_corrections (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- memory_edit|memory_delete|topic_edit|topic_delete|digest_edit|profile_edit
    target      TEXT NOT NULL,        -- memory|topic|digest|profile
    target_id   TEXT,                 -- 被改对象的 id（profile 为空）
    before_text TEXT NOT NULL DEFAULT '',
    after_text  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    embedding   vector(1024),
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_corr_user_at ON memory_corrections (user_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_corr_user_target ON memory_corrections (user_id, target);

-- 维度必须和 memories.embedding 一致（当前 embedding 模型是 1024 维）。
-- 写成 1536 的话，任何一次落向量都会报 "expected 1536 dimensions, not 1024"，
-- 表现是"删除记忆接口 500"，而真正原因在表结构里，很不好查。
--
-- ★ 绝对不要在这里放 ALTER ... TYPE ... USING NULL。
--   schema.sql 每次启动都会整段执行，USING NULL 会把**所有已有向量清空**，
--   于是"刚补算完向量、下次重启又没了"，而且表现为"防复活时灵时不灵"，
--   极难排查（这个坑真踩过）。要改维度请单独手工执行：
--     ALTER TABLE memory_corrections
--         ALTER COLUMN embedding TYPE vector(N) USING NULL;
--   然后调 store.backfill_correction_vectors() 把向量补回来。
