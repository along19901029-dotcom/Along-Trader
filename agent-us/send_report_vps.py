import os, sys, json
from pathlib import Path

agent_dir = sys.argv[1]
os.chdir(agent_dir)
sys.path.insert(0, agent_dir)

from dotenv import load_dotenv
load_dotenv(Path(agent_dir) / ".env")

import importlib.util
spec = importlib.util.spec_from_file_location("trader", f"{agent_dir}/trader.py")
trader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trader)

# Populate name dictionaries and sync latest positions
trader.get_universe()
trader.sync_positions()

state = trader.load_state()
print(f"Agent: {os.getenv('AGENT_NAME', 'unknown')}")
print(f"Positions: {len(state.get('positions', {}))}")
print(f"Trades today: {len(state.get('daily_trades', []))}")
print(f"Cash: {state.get('cash', 'N/A')}")

report = trader.generate_report(state)
trader.send_report_email(report)
print("Report sent OK")
