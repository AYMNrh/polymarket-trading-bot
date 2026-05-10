from self_learning import SelfLearningEngine
sl = SelfLearningEngine()
print(strategy_report() if hasattr(sl, "strategy_report") else "No strategy_report")
recs = sl.get_recommendations() if hasattr(sl, "get_recommendations") else []
print(f"Recommendations: {recs}")
