"""驗證 Google Maps MCP（自寫新版 API server）。

直接對 stdio 講 JSON-RPC，唔經模型 —— 快、準、唔使等。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SERVER = "/opt/dsh-mcp/google_maps_server.py"


def main() -> int:
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-u", SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )

    def call(msg: dict) -> dict:
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line)

    try:
        print("initialize:", call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]["serverInfo"])
        tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]
        print("tools:", ", ".join(t["name"] for t in tools))

        tests = [
            ("maps_geocode", {"address": "Hong Kong Central"}),
            ("maps_place_search", {"query": "咖啡店", "latitude": 22.3049, "longitude": 114.1716, "radius_m": 800}),
            ("maps_directions", {"origin": "Hong Kong Central", "destination": "Tsim Sha Tsui", "mode": "DRIVE"}),
        ]
        for i, (name, args) in enumerate(tests, start=10):
            res = call({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
            body = res["result"]["content"][0]["text"]
            print(f"\n--- {name} (isError={res['result'].get('isError')}) ---")
            print(body[:700])

        # details：用 search 搵到嘅第一個 place_id
        res = call({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                    "params": {"name": "maps_place_search", "arguments": {"query": "Liberty Coffee Jordan"}}})
        places = json.loads(res["result"]["content"][0]["text"]).get("places") or []
        if places:
            pid = places[0]["place_id"]
            res = call({"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                        "params": {"name": "maps_place_details", "arguments": {"place_id": pid}}})
            print("\n--- maps_place_details ---")
            print(res["result"]["content"][0]["text"][:900])
        return 0
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read()
        if err.strip():
            print("\n[stderr]", err[:500], file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
