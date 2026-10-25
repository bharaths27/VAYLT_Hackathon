"""Runs on the BRAIN Pi. Needs only robot/client.py (standard library). Shows the intended loop:
observe -> plan -> ask approval -> execute -> trust only SUCCEEDED.

    ENGRAM_ROBOT_SECRET=... python3 brain_example.py 7
"""
import os
import sys

from client import RobotClient

TAG = int(sys.argv[1]) if len(sys.argv) > 1 else 7
bot = RobotClient("http://10.0.0.2:8200", os.environ.get("ENGRAM_ROBOT_SECRET", "dev-secret-change-me"),
                  name="brain")
try:
    st = bot.state()
    print("robot:", st["driver"], "| e-stop:", st["safety"]["estop_latched"])
    seen = bot.execute("observe")["data"]["tags"]
    print("tags visible:", [(t["tag_id"], t["distance_m"]) for t in seen])

    plan = [("search_tag", {"tag_id": TAG}), ("pick_tagged", {"tag_id": TAG})]
    print("plan:", plan)
    if input("approve plan? [y/N] ").strip().lower() != "y":   # stands in for the iPad approval
        raise SystemExit("not approved; nothing moved")
    for op, args in plan:
        r = bot.execute(op, args, approved=True)
        print(f"{op}: {r['status']} {r['code']} - {r['message']}")
        if r["status"] != "SUCCEEDED":
            print("stopping plan: only SUCCEEDED counts as done")
            break
finally:
    bot.close()
