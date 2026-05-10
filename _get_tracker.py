from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
summary = orch.tracker.position_tracker.summary()
print("\n--- RAW TRACKER DATA ---")
import json
tracked = orch.tracker.position_tracker.positions if hasattr(orch.tracker.position_tracker, 'positions') else {}
print(f"Tracked positions count: {len(tracked)}")
if isinstance(tracked, dict):
    for k, v in list(tracked.items())[:8]:
        print(f"  {k}: {json.dumps({kk:str(vv) for kk,vv in v.items()}, default=str)[:200]}")
