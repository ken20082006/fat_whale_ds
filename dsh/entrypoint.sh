#!/bin/sh
# dsh 啟動腳本 —— **由 Router spawn，唔係獨立容器**。
#
# Router 嘅 `DshClient` 會執行呢個腳本，用 stdin/stdout 傾 JSON-RPC。
#
# 本腳本做三件事：
#   1. 把 profile 由 image 嗰份複製到 $DSH_HOME（bind mount 會蓋走）
#   2. 把 SOUL.md 砌入 `system-prompt.personaPrefix`
#   3. 起 dsh（stdio，唔開 port）
#
# ⚠️ **stdout 屬於 JSON-RPC。** 本腳本所有輸出都要寫 stderr，
#    唔係嘅話 Router 會喺 stdout 收到非 JSON 行。
set -eu

PROFILE_DIR="${DSH_HOME:-/opt/dsh}/profiles/fatwhale-sdk"
PATCH="${PROFILE_DIR}/cordis.patch.yml"
SRC="${DSH_PROFILE_SRC:-/opt/dsh-src/profile}"
SOUL="${DSH_SOUL:-/opt/dsh-src/SOUL.md}"

mkdir -p "$PROFILE_DIR"

# ⚠️ compose 掛 `./dsh/data:/opt/dsh`，所以每次開機都要由 image 嗰份重砌。
for f in package.json pnpm-workspace.yaml cordis.patch.yml; do
    cp "${SRC}/${f}" "${PROFILE_DIR}/${f}"
done

# persona：SOUL.md 塞入 `system-prompt` 嗰 row 嘅 `personaPrefix`。
#
# ⚠️ **`personaPrefix`，唔係 `persona`** —— sdk-app bundle 將嗰 row 嘅
#    config 改咗做 `personaPrefix`。寫錯 key 會被 schema 忽略，人設靜靜消失。
#
# 用 Python 砌 YAML 而唔用 sed —— SOUL.md 有大量引號、冒號、換行。
if [ -f "$SOUL" ]; then
    python3 - "$PATCH" "$SOUL" <<'PY'
import io, sys, yaml

patch_path, soul_path = sys.argv[1], sys.argv[2]
rows = yaml.safe_load(io.open(patch_path, encoding="utf-8")) or []
if not isinstance(rows, list):
    rows = [rows]

soul = io.open(soul_path, encoding="utf-8").read()

# dsh 嘅 persona 係 `{{…}}` 樣板，會嚴格解譯。SOUL.md 本身冇大括號
# （2026-10-20 查過），但為安全起見驗一次。
if "{{" in soul or "}}" in soul:
    sys.exit("SOUL.md 含有 {{ }} —— dsh 會當成樣板變數而報錯")

row = next((r for r in rows if isinstance(r, dict) and r.get("id") == "system-prompt"), None)
if row is None:
    row = {"id": "system-prompt"}
    rows.append(row)
config = row.setdefault("config", {})
config.pop("persona", None)
config["personaPrefix"] = soul

with io.open(patch_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(rows, f, allow_unicode=True, sort_keys=False, width=4096)

print(f"[dsh] persona 已載入：{len(soul)} 字元", file=sys.stderr, flush=True)
PY
else
    echo "[dsh] 警告：搵唔到 $SOUL —— 會用 dsh 預設 persona" >&2
fi

echo "[dsh] profile: $PROFILE_DIR" >&2

# exec 令 dsh 直接食到 signal。**冇 --host / --port** —— SDK 用 stdio。
exec dsh --profile fatwhale-sdk
