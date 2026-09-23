"""進入點。

啟動方式：
    python -m dafeijing.main
"""

from __future__ import annotations

import logging

from telegram import Update

from .bot.app import build_application, create_services
from .logging_setup import setup_logging
from .settings import get_settings

logger = logging.getLogger(__name__)


def main() -> int:
    cfg = get_settings()
    cfg.ensure_dirs()
    setup_logging(cfg.log_dir)

    logger.info("大肥鯨準備下水…")
    services = create_services(cfg)
    application = build_application(services)

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except KeyboardInterrupt:
        logger.info("收到中斷訊號。")
        return 0
    except Exception:
        # run_polling 自己會記錄多數錯誤，這裡是萬一它沒攔到的情況
        logger.exception("bot 異常結束")
        return 1

    # 正常情況下 run_polling 只有被停止才會返回。
    # 沒有這一行，就不容易分辨「正常收工」與「被硬生生殺掉」。
    logger.info("輪詢結束，程式返回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
