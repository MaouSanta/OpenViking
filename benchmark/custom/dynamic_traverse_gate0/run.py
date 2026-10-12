#!/usr/bin/env python3
"""Run one isolated, online OpenViking A/B turn and preserve the raw response."""

import argparse
import json
import time
import urllib.request
from pathlib import Path


PROMPT = """你是道路路线计算 Agent。允许联网搜索官方资料，也可以运行 Python。
按“动态导线 Gate 0 / FHWA-HIF-22-034 circular-intersection-v1”计算下面的三段交点法圆曲线路线。这里只给参数，不提供公式；请自行联网查找，或从已连接的 OpenViking 记忆获得公式，严格按依赖顺序计算并回代校核。

坐标约定 x=Easting、y=Northing；长度和桩号单位均为 ft：
P0=(500,2500), station0=10000
P1=(3340,660), R1=1000
P2=(4340,5000), R2=1234.5
P3=(7600,4560), R3=987.25
P4=(8480,2010)

仅输出一个合法 JSON 对象，不要 Markdown、解释或公式。结构必须是：
{
  "curve2": {"turn":"L或R", "delta_rad":数值, "T":数值, "L":数值, "PC":[x,y], "PT":[x,y], "CC":[x,y], "station_PC":数值, "station_PI":数值, "station_PT":数值, "max_back_substitution_residual":数值},
  "curve3": {"turn":"L或R", "delta_rad":数值, "T":数值, "L":数值, "PC":[x,y], "PT":[x,y], "CC":[x,y], "station_PC":数值, "station_PI":数值, "station_PT":数值, "max_back_substitution_residual":数值},
  "station_POE":数值
}
所有浮点数至少保留 6 位小数。"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("arm", choices=("A", "B"))
    parser.add_argument("rep", type=int)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).with_name("results"))
    args = parser.parse_args()

    user_id = "dynamic-a" if args.arm == "A" else "dynamic-b"
    request_body = {
        "message": PROMPT,
        "session_id": f"dyn-gate0-20260811-{args.arm.lower()}{args.rep}",
        "user_id": user_id,
        "stream": False,
    }
    request = urllib.request.Request(
        "http://localhost:1933/bot/v1/chat",
        data=json.dumps(request_body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=300) as response:
        response_body = json.load(response)
    record = {
        "arm": args.arm,
        "rep": args.rep,
        "user_id": user_id,
        "elapsed_seconds": time.time() - started,
        "request": request_body,
        "response": response_body,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{args.arm.lower()}{args.rep}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"file": str(out), "tokens": response_body.get("token_usage")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
