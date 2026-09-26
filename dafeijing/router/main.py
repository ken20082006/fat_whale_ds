"""Router 進入點。

啟動方式：
    python -m dafeijing.router.main

同舊版嘅 `dafeijing.main` 一樣係 long polling（舊版喺 legacy-fatwhale branch），但**唔會自己打模型** ——
對話交去 Hermes 嘅 API server，用 conversation 參數指定邊一條引用串。

⚠️ 同一時間只可以有一個 process 揸住同一個 bot token。Router 同大肥鯨
用唔同 token 就冇事；要用同一個 token 就要先停另一邊，否則 Telegram 回
409，而 Hermes 更會主動踢走爭 `getUpdates` 嘅對手。
"""

from __future__ import annotations

import logging

from telegram import Update

from ..logging_setup import setup_logging
from ..settings import get_settings
from .app import build_application
from .services import create_services

logger = logging.getLogger(__name__)


def main() -> int:
    cfg = get_settings()
    cfg.ensure_dirs()
    # 一定要用唔同名嘅 log 檔 —— 同大肥鯨寫同一個檔會令兩邊紀錄都爛。
    setup_logging(cfg.log_dir, name="router")

    if not cfg.hermes_key:
        logger.error("FW_HERMES_KEY 未設定 —— 冇佢就入唔到 Hermes API server")
        return 1

    logger.info("Router 準備下水…（Hermes 喺 %s）", cfg.hermes_url)
    services = create_services(cfg)
    application = build_application(services, cfg)

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except KeyboardInterrupt:
        logger.info("收到中斷訊號。")
        return 0
    except Exception:
        logger.exception("Router 異常結束")
        return 1

    logger.info("輪詢結束，程式返回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
