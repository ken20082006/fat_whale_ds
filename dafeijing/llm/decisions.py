"""Jev 決策模型用戶端。

TypeSafe 的 Jev **不是聊天模型** —— 它不生成文字，只回傳帶校準機率的型別化
答案。走獨立端點 `/api/alpha/decisions`，所以它不在 `/api/v1/models` 清單裡，
也不能用 `/chat/completions` 打（會回 400）。

**為什麼需要它。** 這種「這一則該怎麼處理」的判斷，交給主模型自己做會失敗
—— 它同時想回答、又得先決定要不要想，有利益衝突（這正是 `[[搜尋:...]]`
標記機制失敗的原因，見 TODO.md）。交給一個只做決策、不生成文字的模型就
沒有這個問題，而且回的是校準過的機率而不是一句可以含糊過去的話。

**三種 primitive**（實測形狀，文件沒寫清楚）：

- `noul` —— 條件成不成立，回成立的機率。
- `choice` —— 多選一，回選中項與機率分布。`criteria` 是 **record**。
- `score` —— 有序尺度，回**連續**位置與機率分布。`criteria` 是 **array**。

`score` 與 `choice` 都附 `confidence`：分布越平，它越不肯定。本例拿它當閘 ——
信心不足就不要信那個判斷，退回後備。

**一次問多條。** `questions` 收一個 dict，所以分流、深度、語氣合成一次呼叫。

**這個用戶端永遠不會把例外往外丟。** 它是每則訊息的前置步驟，而端點還在
alpha —— 它掛掉不可以讓整個回覆失敗。失敗一律回 None，由呼叫端降級。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# 路由選項。名字會出現在回應裡，所以當成常數共用。
ROUTE_DIRECT = "direct"
ROUTE_REASON = "reason"
ROUTE_SEARCH = "search"
ROUTE_SEARCH_REASON = "search_reason"

_ROUTE_INSTRUCTIONS = (
    "判斷這一則應該怎樣處理。"
    "direct：打招呼、閒聊、情緒抒發、單一事實查詢 —— 直接答就好。"
    "reason：多步計算、邏輯推演、取捨比較、除錯 —— 要先想清楚才答得好。"
    "search：問的是會隨時間變的事（新聞、版本、價格、日期、賽果）；"
    "要確認某樣嘢係唔係真嘅（某個型號、產品、人物、事件係唔係存在）；"
    "或者對方明講要你上網查。"
    "search_reason：既要查證、又要推理才答得好。"
)

# score 的 criteria 必須是**陣列**（物件會回 400）。
_DEPTH_CRITERIA = ["唔使想，直接答", "要想一陣先答", "要逐步推演先答得好"]
_TONE_CRITERIA = ["收起演出，正經答", "正常", "放開玩，多啲角色演出"]

# 搜尋力度。搜尋引擎每次請求只搜一次、查詢由引擎自己推導，所以「搜幾多條」
# 是唯一能調召回率的旋鈕 —— 小眾或冷門的關鍵詞要撈多幾條才撞得中。
EFFORT_QUICK = "quick"
EFFORT_NORMAL = "normal"
EFFORT_THOROUGH = "thorough"

_EFFORT_INSTRUCTIONS = (
    "判斷要回答這一則，網上要撈幾多資料才夠。"
    "quick：查一個明確的事實就夠（某日天氣、某個價位）。"
    "normal：一般查證。"
    "thorough：小眾、冷門、或者可能唔存在嘅名詞；又或者講法有分歧、"
    "需要多角度對照。呢類撈得少就會漏。"
)

# 只要有一個問題認不出來就回 None，由呼叫端決定後備 —— 不要在這裡偷偷選一個。
_EFFORT_LEVELS = (EFFORT_QUICK, EFFORT_NORMAL, EFFORT_THOROUGH)

_Q_ROUTE = "route"
_Q_DEPTH = "depth"
_Q_TONE = "tone"
_Q_EFFORT = "search_effort"


@dataclass(frozen=True)
class Assessment:
    """一次呼叫拿到的全部判斷。

    每個欄位都可能是 None —— 那一題沒答、型別不對、或者信心不足。
    呼叫端遇到 None 就用自己的後備值，不要假設一定有答案。
    """

    route: str | None = None
    depth: float | None = None       # 0.0–1.0，越大越需要思考
    tone: float | None = None        # 0.0–1.0，越大越可以放開演出
    # 要搜的話，撈幾多資料才夠。搜尋引擎每次請求只搜一次、查詢由引擎自己
    # 推導，所以這是唯一能調召回率的旋鈕。
    search_effort: str | None = None
    cost: float = 0.0
    model: str = ""
    confidence: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.confidence is None:
            object.__setattr__(self, "confidence", {})


@dataclass(frozen=True)
class Decision:
    """單一 noul 判斷的結果。value 是「條件成立」的機率。"""

    value: float
    confidence: float = 0.0
    cost: float = 0.0
    model: str = ""


def resolve_reasoning(
    personal: int | None,
    indicated: bool | None,
    *,
    fallback: bool,
) -> bool:
    """把三層設定收成一個布林值。抽成純函式比較好測。

    1. 使用者明確指定（`/think on` / `off`）→ 照做，不理判斷結果。
    2. 跟隨預設（`/think auto`）→ 看路由判斷。
    3. 判斷沒回（indicated is None）→ 退回全域設定 `fallback`。
    """
    if personal is not None:
        return bool(personal)
    if indicated is None:
        return fallback
    return indicated


def route_flags(route: str | None) -> tuple[bool | None, bool | None]:
    """把路由選項拆成 (要不要推理, 要不要搜尋)。

    認不出來就回 (None, None) —— 由呼叫端決定後備，不要在這裡偷偷選一個。
    """
    if route == ROUTE_DIRECT:
        return False, False
    if route == ROUTE_REASON:
        return True, False
    if route == ROUTE_SEARCH:
        return False, True
    if route == ROUTE_SEARCH_REASON:
        return True, True
    return None, None


def search_results(effort: str | None, quick: int, normal: int, thorough: int) -> int:
    """把搜尋力度映射成要撈幾條結果。

    認不出來就用中間值 —— 這是後備，不是判斷結果。搜尋引擎每次請求只搜
    一次、查詢由引擎自己推導，所以撈幾多條是唯一能調召回率的旋鈕：
    小眾或冷門的關鍵詞要撈多幾條才撞得中。
    """
    if effort == EFFORT_QUICK:
        return quick
    if effort == EFFORT_THOROUGH:
        return thorough
    return normal


def budget_factor(depth: float | None, low: float, high: float) -> float:
    """把 0–1 的深度映射成 max_tokens 的倍率。

    推理 token 會**吃掉 max_tokens 額度**，額度用完 content 會變 null
    （使用者看到的訊息是「本鯨想得太久，額度用完了」）。所以思考越深，
    越需要多留一點額度；閒聊則可以把額度收窄，免得模型寫成洗版。
    """
    if depth is None:
        return 1.0
    return low + (high - low) * max(0.0, min(1.0, depth))


def tone_vibe(tone: float | None, current: str, order: tuple[str, ...]) -> str:
    """語氣分數只能把演出**收窄**，不能推高。

    使用者的 `/vibe` 是明確選擇，不該被逐則判斷蓋過去；但「對方情緒低落時
    收起角色扮演」本來就寫在人設裡，所以往下調是安全的。

    分數越低（越正經）降得越多：低於 0.15 直接收到最低，低於 0.35 降一級。
    """
    if tone is None or current not in order:
        return current
    if tone < 0.15:
        return order[0]
    index = order.index(current)
    if tone < 0.35 and index > 0:
        return order[index - 1]
    return current


class DecisionsClient:
    _SCORE_QUESTIONS = (_Q_DEPTH, _Q_TONE)

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            # 它擋在主回覆前面，逾時要短 —— 等太久不如直接降級。
            timeout=httpx.Timeout(cfg.decision_timeout_seconds, connect=5.0),
            headers={
                "Authorization": f"Bearer {cfg.openrouter_api_key}",
                "HTTP-Referer": cfg.openrouter_referer,
                "X-Title": cfg.openrouter_title,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── 主要入口 ────────────────────────────────────────

    async def assess(self, state: str, *, want_tone: bool = True) -> Assessment | None:
        """一次問齊：怎麼處理、要幾深、幾放開。

        合成一次呼叫而不是三次 —— 實測一次三問約 $0.00002，端點本身支援
        `questions` 收多條，分開打只是白付兩次網絡往返。
        """
        if not self._cfg.decision_enabled or not state.strip():
            return None

        questions: dict = {
            _Q_ROUTE: {
                "type": "choice",
                "instructions": _ROUTE_INSTRUCTIONS,
                "criteria": {
                    ROUTE_DIRECT: "可以直接答，不用查也不用想",
                    ROUTE_REASON: "不用查，但要想清楚才答得好",
                    ROUTE_SEARCH: "要先上網查證才答得準",
                    ROUTE_SEARCH_REASON: "既要查證又要推理",
                },
            },
            _Q_DEPTH: {
                "type": "score",
                "instructions": "要好好回答這一則，需要思考到什麼程度",
                "criteria": list(_DEPTH_CRITERIA),
            },
            _Q_EFFORT: {
                "type": "choice",
                "instructions": _EFFORT_INSTRUCTIONS,
                "criteria": {
                    EFFORT_QUICK: "一個明確的事實就夠",
                    EFFORT_NORMAL: "一般查證",
                    EFFORT_THOROUGH: "小眾冷門、或者講法有分歧，撈少會漏",
                },
            },
        }
        if want_tone:
            questions[_Q_TONE] = {
                "type": "score",
                "instructions": "回應這一則時，適合放開角色演出到什麼程度",
                "criteria": list(_TONE_CRITERIA),
            }

        data = await self._post(state, questions)
        if data is None:
            return None

        answers = data.get("answers") or {}
        usage = data.get("usage") or {}
        min_conf = self._cfg.decision_min_confidence

        route, route_conf = self._read_choice(answers.get(_Q_ROUTE), min_conf)
        depth, depth_conf = self._read_score(answers.get(_Q_DEPTH), len(_DEPTH_CRITERIA), min_conf)
        if want_tone:
            tone, tone_conf = self._read_score(answers.get(_Q_TONE), len(_TONE_CRITERIA), min_conf)
        else:
            tone, tone_conf = None, 0.0

        effort, effort_conf = self._read_choice(answers.get(_Q_EFFORT), min_conf)
        if effort not in _EFFORT_LEVELS:
            effort = None

        return Assessment(
            route=route,
            depth=depth,
            tone=tone,
            search_effort=effort,
            cost=float(usage.get("cost") or 0.0),
            model=str(data.get("model") or ""),
            confidence={
                _Q_ROUTE: route_conf,
                _Q_DEPTH: depth_conf,
                _Q_TONE: tone_conf,
                _Q_EFFORT: effort_conf,
            },
        )

    async def noul(self, state: str, instructions: str) -> Decision | None:
        """問一個「條件成不成立」的問題。回傳機率；任何失敗都回 None。"""
        if not self._cfg.decision_enabled:
            return None

        data = await self._post(
            state, {"answer": {"type": "noul", "instructions": instructions}}
        )
        if data is None:
            return None

        answer = ((data.get("answers") or {}).get("answer")) or {}
        if answer.get("type") != "noul":
            logger.warning("決策模型回傳了非 noul 的答案：%s", answer)
            return None
        try:
            value = float(answer["noul"])
        except (TypeError, ValueError, KeyError):
            logger.warning("決策模型的 noul 值看不懂：%s", answer)
            return None

        usage = data.get("usage") or {}
        return Decision(
            value=value,
            confidence=float(answer.get("confidence") or 0.0),
            cost=float(usage.get("cost") or 0.0),
            model=str(data.get("model") or ""),
        )

    async def needs_reasoning(self, text: str) -> Decision | None:
        return await self.noul(text, _REASONING_INSTRUCTIONS)

    # ── 傳輸 ────────────────────────────────────────────

    async def _post(self, state: str, questions: dict) -> dict | None:
        payload = {"model": self._cfg.decision_model, "state": state, "questions": questions}
        try:
            response = await self._client.post(self._cfg.decision_endpoint, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning("決策模型連不上，本次跳過：%s", exc)
            return None
        except Exception:
            logger.exception("決策模型呼叫失敗，本次跳過")
            return None

        if response.status_code >= 400:
            # alpha 端點，欄位或路由隨時可能變。記下來但不要吵。
            logger.warning("決策模型回 %s：%s", response.status_code, response.text[:200])
            return None

        try:
            return response.json()
        except Exception:
            logger.exception("決策模型回應看不懂，本次跳過")
            return None

    @staticmethod
    def _read_choice(answer: dict | None, min_confidence: float) -> tuple[str | None, float]:
        """讀 choice 的答案。信心不足就當作沒判斷 —— 分布很平時不該硬選一個。"""
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            return None, 0.0
        confidence = float(answer.get("confidence") or 0.0)
        chosen = answer.get("choice")
        if not isinstance(chosen, str) or confidence < min_confidence:
            if chosen:
                logger.debug("決策信心不足（%.2f），不採用：%s", confidence, chosen)
            return None, confidence
        return chosen, confidence

    @staticmethod
    def _read_score(
        answer: dict | None, levels: int, min_confidence: float
    ) -> tuple[float | None, float]:
        """讀 score 的答案，正規化成 0–1。

        回應的 `score` 是機率加權後的位置（0 到 levels-1），不是分桶，
        所以直接除就可以當連續值用。
        """
        if not isinstance(answer, dict) or answer.get("type") != "score":
            return None, 0.0
        confidence = float(answer.get("confidence") or 0.0)
        span = max(1, levels - 1)
        try:
            raw = float(answer["score"])
        except (TypeError, ValueError, KeyError):
            return None, confidence
        if confidence < min_confidence:
            logger.debug("決策信心不足（%.2f），不採用分數 %.2f", confidence, raw)
            return None, confidence
        return max(0.0, min(1.0, raw / span)), confidence


# 保留給 needs_reasoning() 的單題用法（scripts 與測試會用到）。
_REASONING_INSTRUCTIONS = (
    "判斷要好好回答這一則，需不需要逐步推理。"
    "需要推理的例子：多步計算、邏輯推演、取捨比較、除錯、要先想清楚才答得好的問題。"
    "不需要推理的例子：打招呼、閒聊、情緒抒發、單一事實查詢、簡短確認。"
)
