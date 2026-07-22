import asyncio
import json
import mt5_bridge
import order_executor
from main import data_agent, technical_agent, risk_manager, risk_agent, ceo_agent, SYMBOLS

async def debug_cycle():
    print("--- MT5 Bridge Check ---")
    snapshot_path = mt5_bridge.SNAPSHOT_PATH
    print(f"Snapshot path: {snapshot_path}")
    print(f"Exists: {snapshot_path.exists()}")
    live = mt5_bridge.read_snapshot()
    if live:
        print(f"MT5 Data Loaded: Mode={live.get('account', {}).get('trade_mode')}")
    else:
        print("NO LIVE MT5 DATA (or stale).")
    
    print(f"\nIs Demo Account? {order_executor.is_demo_account()}")

    print("\n--- Running 1 Cycle ---")
    symbol = "EURUSD"
    snapshot = data_agent.tick(symbol)
    print(f"Data: {snapshot['symbol']} at {snapshot['price']}")

    technical = technical_agent.analyze(snapshot)
    print(f"Technical: bias={technical['bias']}, conf={technical.get('confidence')}%, reason={technical.get('reason')}")

    risk = risk_agent.evaluate(symbol, technical["bias"])
    print(f"Risk: approved={risk.get('approved')}, reason={risk.get('reason')}")

    decision = ceo_agent.decide(technical, {}, risk, snapshot)
    print(f"CEO Decision: {decision['action']}")
    print(f"CEO Council: {json.dumps(decision.get('council', {}), ensure_ascii=False)}")

if __name__ == '__main__':
    asyncio.run(debug_cycle())
