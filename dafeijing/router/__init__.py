"""Router：大肥鯨嘅外殼 + Hermes 嘅腦。

設計見 `C:\\ds\\hermes_ds\\ARCHITECTURE.md`。

一句話：Telegram 嘅**每條引用串**對應 Hermes 嘅**一條獨立對話**，
因為 Hermes 原生嘅群組 session 係「每人一條、永不過期」，
做唔到「每串一條」。
"""
