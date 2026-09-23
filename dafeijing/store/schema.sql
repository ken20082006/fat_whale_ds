-- 大肥鯨資料庫結構
-- 時間一律存 ISO8601 UTC 字串，方便直接比較與排序。

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id    INTEGER NOT NULL UNIQUE,
    status        TEXT    NOT NULL DEFAULT 'pending',   -- pending | active | blocked
    display_name  TEXT,
    username      TEXT,
    daily_cap     INTEGER,                              -- NULL = 不限
    vibe          TEXT    NOT NULL DEFAULT 'mid',       -- low | mid | high
    created_at    TEXT    NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash     TEXT    NOT NULL UNIQUE,
    label         TEXT,                                 -- 備註給誰
    bound_user_id INTEGER,
    bound_at      TEXT,
    expires_at    TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    created_by    INTEGER
);

-- 私聊以 user 為單位；群組以「串」為單位
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key        TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL,                   -- private | group_thread
    user_id         INTEGER,
    chat_id         INTEGER,
    thread_root_id  INTEGER,
    summary         TEXT,
    summary_tokens  INTEGER NOT NULL DEFAULT 0,
    last_active_at  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,                       -- user | assistant
    content     TEXT    NOT NULL,
    tokens      INTEGER NOT NULL DEFAULT 0,
    has_image   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

-- 跨 session 的長期記憶
CREATE TABLE IF NOT EXISTS memory_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'explicit',    -- explicit | auto
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON memory_notes(user_id);

-- 群組訊息短期快取，供回溯引用鏈。逾時自動清除，永不送進模型。
CREATE TABLE IF NOT EXISTS group_cache (
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    reply_to_id   INTEGER,
    user_id       INTEGER,
    display_name  TEXT,
    text          TEXT,
    has_media     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_cache_created ON group_cache(created_at);

-- 群組白名單。非白名單的群組一律退出。
CREATE TABLE IF NOT EXISTS groups (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    allowed     INTEGER NOT NULL DEFAULT 0,
    added_by    INTEGER,
    created_at  TEXT    NOT NULL
);

-- 貼圖對照表：Telegram 只給 file_unique_id 與 emoji，不含圖檔內容
CREATE TABLE IF NOT EXISTS stickers (
    file_unique_id TEXT PRIMARY KEY,
    set_name       TEXT,
    emoji          TEXT,
    meaning        TEXT,
    usage_hint     TEXT
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    chat_id           INTEGER,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    image_tokens      INTEGER NOT NULL DEFAULT 0,
    cost              REAL    NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_log(created_at);
