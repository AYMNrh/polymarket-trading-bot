from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
pt = orch.tracker.position_tracker
positions = pt.positions  # dict
print(f"Total tracked positions: {len(positions)}")
print()
for key, pos in positions.items():
    whale = pos.get("whale", key.split(":")[0][:20])
    direction = pos.get("direction", "?")
    amount = pos.get("amount", 0)
    confidence = pos.get("confidence", 0)
    status = pos.get("status", "")
    token = pos.get("token_address", "")[:12]
    print(f"  {whale:20s} {direction:4s} ${amount:>6.0f}  conf:{confidence*100:3.0f}%  {status:10s}  token:{token}...")
