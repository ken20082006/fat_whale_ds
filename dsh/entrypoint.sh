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

# ⚠️ profile 嘅設定檔由 image 嗰份覆蓋（`profiles/` 唔再係 bind mount，
# 但保留呢步係為咗確保 image 更新之後一定用新設定）。
for f in package.json pnpm-workspace.yaml cordis.patch.yml; do
    cp "${SRC}/${f}" "${PROFILE_DIR}/${f}"
done

# node_modules 由 image build 時 `pnpm install` 落去，唔使再補。如果
# 唔見咗，即係 image 有問題 —— 講清楚，唔好靜靜咁跑落去。
if [ ! -d "${PROFILE_DIR}/node_modules" ] && grep -q '"dependencies"' "${PROFILE_DIR}/package.json" 2>/dev/null; then
    echo "[dsh] 警告：node_modules 唔見咗，out-of-tree plugin 會載入失敗" >&2
fi

# persona：SOUL.md 塞入 `system-prompt` 嗰 row 嘅 `personaPrefix`。
#
# ⚠️ **`personaPrefix`，唔係 `persona`** —— sdk-app bundle 將嗰 row 嘅
#    config 改咗做 `personaPrefix`。寫錯 key 會被 schema 忽略，人設靜靜消失。
#
# 用 Python 砌 YAML 而唔用 sed —— SOUL.md 有大量引號、冒號、換行。
if [ -f "$SOUL" ]; then
    python3 - "$PATCH" "$SOUL" <<'PY'
import io, os, sys, yaml

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

print(f"[dsh] persona 已載入：{len(soul)} 字元", file=sys.stderr, flush=True)

# ── 條件式 MCP ──────────────────────────────────────────
#
# ⚠️ **唔可以喺 cordis.patch.yml 用 `!!js`** —— 下面會用
# `yaml.safe_dump` 重寫成個檔，而 safe_load 唔識 `!!js` tag，
# 會直接拋錯。所以條件式邏輯放喺呢度。
#
# 有 key 才掛；冇 key 就乜都唔加（MCP row 完全唔存在，
# 唔會因為 401 拖慢或拖死 startup）。
rows[:] = [
    r for r in rows
    if not (isinstance(r, dict) and "insert" in r and isinstance(r.get("insert"), list)
            and any(isinstance(i, dict) and i.get("id") in ("mcp-google-maps", "mcp-hasdata")
                    for i in r["insert"]))
]

gkey = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
hkey = os.environ.get("HASDATA_API_KEY", "").strip()

if gkey:
    # 我哋自己寫嘅 server（`dsh/mcp/google_maps_server.py`），只用新版 API。
    # npm 上嗰啲用 legacy API，新 project 開唔到。
    # ⚠️ **insert patch 唔可以有 `id`。**
    # 有 `id` 嘅話 dsh 當成「改現有 row」，會報
    #   patch insert: entry "..." not found
    # 而且唔會插任何嘢（2026-10-20 撞到）。
    rows.append({
        "insert": [{
            "id": "mcp-google-maps",
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": "google_maps",
                "transport": "stdio",
                "command": "/usr/local/bin/python3",
                "args": ["-u", "/opt/dsh-mcp/google_maps_server.py"],
                "env": {"GOOGLE_MAPS_API_KEY": gkey},
            },
        }],
    })
    print("[dsh] Google Maps MCP 已掛（新版 API，自寫 server）", file=sys.stderr, flush=True)
elif hkey:
    # HasData（Google Maps pack）—— 2026-10-20 實測帳戶 under review，
    # 工具列得出但一叫就 403。留低以備審核完之後用。
    rows.append({
        "insert": [{
            "id": "mcp-hasdata",
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": "hasdata_maps",
                "transport": "streamable-http",
                "url": "https://mcp.hasdata.com/mcp?apis=google_maps",
                "headers": {"x-api-key": hkey},
            },
        }],
    })
    print("[dsh] HasData Maps MCP 已掛", file=sys.stderr, flush=True)
else:
    print("[dsh] 冇 GOOGLE_MAPS_API_KEY / HASDATA_API_KEY —— Maps MCP 唔掛", file=sys.stderr, flush=True)

# ⚠️ **一定要喺所有 row 改完之後才寫檔。**
# 之前將 dump 放喺 persona 之後、MCP 之前，結果 MCP row 永遠寫唔入 ——
# 但 log 照樣印「已掛」，好難察覺（2026-10-20 撞到）。
with io.open(patch_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(rows, f, allow_unicode=True, sort_keys=False, width=4096)
PY
else
    echo "[dsh] 警告：搵唔到 $SOUL —— 會用 dsh 預設 persona" >&2
fi

echo "[dsh] profile: $PROFILE_DIR" >&2

# exec 令 dsh 直接食到 signal。**冇 --host / --port** —— SDK 用 stdio。
exec dsh --profile fatwhale-sdk
