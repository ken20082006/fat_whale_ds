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
    reasoning     INTEGER,                              -- NULL = 跟隨全域設定；0/1 = 個人指定
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
-- scope 把場合隔開：'private' 或 'group:<chat_id>'。
-- 私聊記的事永遠不會在群組被讀到，反之亦然 —— 否則助理會脫口說出私下的內容。
CREATE TABLE IF NOT EXISTS memory_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    scope       TEXT    NOT NULL DEFAULT 'private',
    content     TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'explicit',    -- explicit | auto
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON memory_notes(user_id, scope);

-- 群組訊息短期快取，供回溯引用鏈。逾時自動清除。
-- 除了被指名的那條串，其餘內容永不送進模型。
-- media_file_id 是必要欄位，不是冗餘：引用串還原時要能把當初那張圖真的抓下來，
-- 只存「〔圖片〕」這種文字標註的話，事後就沒有東西可下載了。
CREATE TABLE IF NOT EXISTS group_cache (
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    reply_to_id   INTEGER,
    user_id       INTEGER,
    display_name  TEXT,
    username      TEXT,                                 -- 供解析 @username 對應到誰
    text          TEXT,
    has_media     INTEGER NOT NULL DEFAULT 0,
    media_file_id TEXT,
    media_source  TEXT,
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

-- 貼圖對照表。
-- Telegram 只給 file_unique_id 與 emoji，不含圖檔內容，所以「看得懂」要靠讀圖；
-- 但「挑得出來」得先離線標註一次 —— 總不能每次回覆都把一百多張圖塞給模型選。
-- file_id 是發送時用的，與 file_unique_id 不同：前者可用於 sendSticker。
-- featured 標記哪些要進提示裡的精選清單。標註可以有一百多張，
-- 但每次回覆都放全部會把 prompt 撐爆，所以只挑一份精選。
CREATE TABLE IF NOT EXISTS stickers (
    file_unique_id TEXT PRIMARY KEY,
    set_name       TEXT,
    file_id        TEXT,
    emoji          TEXT,
    meaning        TEXT,
    usage_hint     TEXT,
    featured       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    chat_id           INTEGER,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,       -- 推理額度，以輸出計價
    image_tokens      INTEGER NOT NULL DEFAULT 0,
    -- 這一輪搜尋了幾次。取自 usage.server_tool_use_details。
    search_requests   INTEGER NOT NULL DEFAULT 0,
    -- 當中屬於搜尋的部分。usage.cost 是總額且已含這筆，所以這個欄位是
    -- cost 的子集，不是額外加上去的。
    search_cost       REAL    NOT NULL DEFAULT 0,
    cost              REAL    NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_log(created_at);

-- 群組概況。每個群一則，描述「這個群體本身」—— 主題、氣氛、慣例、近期話題。
--
-- 與 memory_notes 的分別：筆記是「關於某個人」的事實，每一則都屬於一個
-- user_id；概況是「關於這個群」的，不屬於任何人。兩者需要的東西不同，
-- 所以分開存，而不是把 memory_notes.user_id 改成可為 NULL。
--
-- 素材限於**機器人親自參與過的交流**（被 @ 或被回覆的那些），不會把它
-- 在 group_cache 裡旁觀到的內容沉澱成永久記錄 —— 那些只保留 72 小時。
--
-- content 是一段概況文字而不是一條條筆記，所以天生有界，不需要濃縮機制。
-- summarized_at 是涵蓋到哪個時間點，下次只讀這之後的對話。
CREATE TABLE IF NOT EXISTS group_profile (
    chat_id       INTEGER PRIMARY KEY,
    content       TEXT    NOT NULL,
    summarized_at TEXT    NOT NULL
);
