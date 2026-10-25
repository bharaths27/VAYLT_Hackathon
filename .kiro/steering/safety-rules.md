# Safety rules (never break these)

1. The model only writes a JSON plan. It never sends motor commands.
2. Only actions in engram/capabilities.py exist. Add new ones there, with bounds, and a test.
3. engram/checker.py must approve a plan before it can run. Keep it deterministic: no model calls inside it.
4. A person approves the exact plan digest. Approvals are single use.
5. If an action's outcome is unknown, stop the robot and never retry automatically.
6. Observations are never edited. New beliefs supersede old ones; history stays.
7. The Dreamer (skills.compile_all) only makes CANDIDATE skills. Only a person can approve.
8. Stale object locations are said as "last seen at", never as fact.
9. Stop must always work, including when the network is down.
10. An ambiguous or misheard command stops a running task with the latching stop a stop word uses, and asks:
    "I stopped. <reason> Please clarify." A stopped task never resumes on its own. With nothing running,
    nothing is stopped ("<reason> Please clarify."). Understood refusals (unsafe, a limit, "don't") do not stop.
