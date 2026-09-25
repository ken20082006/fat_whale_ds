"""執行期可調參數。

管理員可以在 Telegram 直接改搜尋相關的參數，改完**立即生效**，而且寫進
資料庫，重啟之後仍然有效。

**三層優先順序**：`.env` 是基底 → 資料庫的覆寫疊在上面 → `/tune set`
即時改。`/tune reset` 會把某一項還原成 `.env` 的值。

為什麼要有這個：之前調 `FW_SEARCH_MODE` 要改檔案再重啟容器，而且改完
不一定生效（`.env` 被其他工具還原過一次，查了很久）。搜尋這種要憑感覺
調的參數，改一次重啟一次太慢。

**只開放搜尋、判斷與顯示相關的參數。** 金鑰、路徑、模型代號那些不開放 ——
它們改錯會令 bot 起不來，而改完要重啟才知道，還是在 `.env` 改安全。
判斷標準是「改錯會唔會令 bot 起唔到」，不是「屬唔屬於搜尋」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..settings import MAX_SEARCH_RESULTS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Knob:
    """一個可調參數。指令的說明、驗證與顯示全部由這裡推出來 ——
    這樣就不會出現「說明寫的跟實際可調的不一致」那種脫節。"""

    key: str          # 指令用的短名
    field: str        # Settings 的欄位名
    kind: type        # int / float / str / bool
    help: str
    choices: tuple[str, ...] = ()   # 只對 str 有意義
    # 數值項的容許範圍。**只寫下限是不夠的** —— 超出上限同樣會令請求失敗，
    # 而且失敗得更隱蔽（每一次搜尋都 400，睇落似搜尋本身壞咗）。
    # 實際撞過：`/tune set results_thorough 30`。None 代表不設限。
    minimum: int | float | None = None
    maximum: int | float | None = None

    @property
    def range_hint(self) -> str:
        """容許範圍，供 /tune 一覽顯示。沒有範圍就回空字串。"""
        if self.kind not in (int, float) or (self.minimum is None and self.maximum is None):
            return ""
        low = "−∞" if self.minimum is None else self.minimum
        high = "+∞" if self.maximum is None else self.maximum
        return f"（{low}–{high}）"


KNOBS: tuple[Knob, ...] = (
    Knob(
        "mode", "search_mode", str,
        "搜尋積極程度｜off 不搜、trigger 只認明講、auto 交給判斷、always 每則都搜",
        ("off", "trigger", "auto", "always"),
    ),
    Knob(
        "engine", "search_engine", str,
        "搜尋引擎｜parallel 最便宜、exa 是官方預設",
        ("parallel", "exa", "perplexity", "firecrawl"),
    ),
    Knob(
        "engine_mode", "search_engine_mode", str,
        "引擎自身的分級｜fast 最便宜、turbo 中庸；"
        "設成 - 代表留空（用引擎預設，最準但最貴）。"
        "各引擎支援的分級不同，所以不寫死驗證",
    ),
    # 撈幾多條有硬上限（見 settings.MAX_SEARCH_RESULTS）—— 超出會令每一次
    # 搜尋請求都 400，所以上下限一齊寫死。
    Knob(
        "results_quick", "search_results_quick", int,
        "判斷說「一個事實就夠」時撈幾條",
        minimum=1, maximum=MAX_SEARCH_RESULTS,
    ),
    Knob(
        "results", "search_max_results", int,
        "一般情況撈幾條",
        minimum=1, maximum=MAX_SEARCH_RESULTS,
    ),
    Knob(
        "results_thorough", "search_results_thorough", int,
        "判斷說「小眾冷門」時撈幾條",
        minimum=1, maximum=MAX_SEARCH_RESULTS,
    ),
    Knob(
        "results_total", "search_max_total_results", int,
        "一次請求的結果總上限（官網冇寫上限，未指定時預設 50）",
        minimum=1,
    ),
    Knob("fetch", "fetch_max_urls", int, "一則訊息最多讀幾個對方貼的連結"),
    Knob("context", "decision_context_messages", int, "判斷要看幾則上文"),
    Knob("confidence", "decision_min_confidence", float, "判斷信心低於此值就當作沒判斷"),
    Knob("reason_low", "reasoning_budget_low", float, "要思考時 max_tokens 最少放寬幾倍"),
    Knob("reason_high", "reasoning_budget_high", float, "要思考時 max_tokens 最多放寬幾倍"),
    Knob("memory_gate", "memory_decision_threshold", float, "記憶抽取的前置閘門檻"),
    Knob(
        "why", "show_reasoning", bool,
        "把模型的思考過程貼出來（beta）｜推理沒開就沒有內容可貼",
    ),
    Knob("why_chars", "show_reasoning_max_chars", int, "思考過程最多貼幾個字"),
)

_BY_KEY = {knob.key: knob for knob in KNOBS}
_BY_FIELD = {knob.field: knob for knob in KNOBS}


# Telegram 的指令參數不會帶空字串（空白會被切掉），所以「設成空」要用
# 一個代表空的寫法。`-` 最直覺，另外接受 none / 空 / （空）。
_EMPTY_TOKENS = {"-", "none", "空", "(空)", "（空）", "null"}

# 開關類參數的寫法。**不可以直接 `bool(value)`** —— 那樣 "off" 會變成 True，
# 而且係靜默錯誤：使用者以為關咗，其實開咗。所以只認列出來的寫法。
_TRUE_TOKENS = {"on", "true", "1", "yes", "y", "開", "開起", "開啟"}
_FALSE_TOKENS = {"off", "false", "0", "no", "n", "關", "關起", "關閉"}


def coerce(knob: Knob, raw: str) -> Any:
    """把使用者輸入的字串轉成該欄位的型別，並驗證。

    寧可報錯也不要靜靜接受 —— 打錯字而無效的設定比沒有設定更難查。
    """
    value = raw.strip()
    # 「設成空」：說明寫「留空用引擎預設」，但指令的參數永遠不會是空字串，
    # 所以要用一個代表空的寫法。以前沒有這個，於是說明講得到、做唔到。
    if value.lower() in _EMPTY_TOKENS and knob.kind is str:
        value = ""
    if knob.choices and value not in knob.choices:
        raise ValueError(f"{knob.key} 只能是 {'／'.join(knob.choices)}，收到 {value!r}")
    if knob.kind is bool:
        token = value.lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise ValueError(f"{knob.key} 要是開／關（on／off），收到 {raw!r}")
    try:
        converted = knob.kind(value)
    except ValueError as exc:
        kind_label = {int: "整數", float: "數字", str: "文字"}[knob.kind]
        raise ValueError(f"{knob.key} 要是{kind_label}，收到 {raw!r}") from exc

    # 範圍要在**設定那一刻**就擋。放佢過去，錯誤會延後到下一次請求才爆，
    # 而且訊息完全指唔返係邊個設定搞出嚟。
    if knob.minimum is not None and converted < knob.minimum:
        raise ValueError(f"{knob.key} 最少 {knob.minimum}，收到 {converted}")
    if knob.maximum is not None and converted > knob.maximum:
        raise ValueError(f"{knob.key} 最多 {knob.maximum}，收到 {converted}")
    return converted


class Tuning:
    """可調參數的存取。刻意只拿 db 與 cfg，不碰 Telegram。"""

    def __init__(self, cfg, db) -> None:
        self._cfg = cfg
        self._db = db
        # 記住 .env 的原始值，reset 時還原用
        self._base: dict[str, Any] = {
            knob.field: getattr(cfg, knob.field) for knob in KNOBS
        }

    async def load(self) -> int:
        """把資料庫的覆寫疊上去。回傳套用了幾項。"""
        rows = await self._db.fetchall(
            "SELECT key, value FROM runtime_settings"
        )
        applied = 0
        for row in rows:
            knob = _BY_KEY.get(row["key"])
            if knob is None:
                continue  # 舊版留下的、已經不在清單裡的，忽略
            try:
                setattr(self._cfg, knob.field, coerce(knob, row["value"]))
                applied += 1
            except ValueError:
                logger.warning(
                    "資料庫裡的 %s=%r 不合法，忽略（用 .env 的值）",
                    row["key"], row["value"],
                )
        if applied:
            logger.info("套用 %d 項執行期設定", applied)
        return applied

    def current(self, knob: Knob) -> Any:
        return getattr(self._cfg, knob.field)

    async def set(self, key: str, raw: str) -> tuple[Any, Any]:
        """回傳 (舊值, 新值)。值不���法就拋 ValueError。"""
        knob = _BY_KEY.get(key)
        if knob is None:
            raise ValueError(f"沒有這個可調項：{key}")
        value = coerce(knob, raw)
        old = getattr(self._cfg, knob.field)
        setattr(self._cfg, knob.field, value)
        await self._db.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, str(value), _now()),
        )
        logger.info("執行期設定：%s %r → %r", key, old, value)
        return old, value

    async def reset(self, key: str) -> tuple[Any, Any] | None:
        """還原成 .env 的值。回傳 (舊值, 新值)；本來就沒覆寫則回 None。"""
        knob = _BY_KEY.get(key)
        if knob is None:
            raise ValueError(f"沒有這個可調項：{key}")
        changed = await self._db.affect(
            "DELETE FROM runtime_settings WHERE key = ?", (key,)
        )
        if not changed:
            return None
        old = getattr(self._cfg, knob.field)
        new = self._base[knob.field]
        setattr(self._cfg, knob.field, new)
        logger.info("執行期設定還原：%s %r → %r", key, old, new)
        return old, new

    async def overridden(self) -> set[str]:
        """哪些項目前是被資料庫覆寫的（不是 .env 的值）。"""
        rows = await self._db.fetchall("SELECT key FROM runtime_settings")
        return {row["key"] for row in rows if row["key"] in _BY_KEY}


def _now() -> str:
    from .util import now_iso

    return now_iso()
