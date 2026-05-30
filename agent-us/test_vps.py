import os, sys

for agent_dir, func_name in [("/opt/ai-trader-ashare", "get_stock_name"),
                               ("/opt/ai-trader-etf", "get_etf_name"),
                               ("/opt/ai-trader-bond", "get_bond_name")]:
    os.chdir(agent_dir)
    sys.path.insert(0, agent_dir)
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(agent_dir) / ".env")
    import importlib.util
    spec = importlib.util.spec_from_file_location("trader", agent_dir + "/trader.py")
    trader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trader)

    print(f"=== {agent_dir.split('/')[-1]} ===")

    # Load universe to populate names
    universe = trader.get_universe()
    print(f"Universe: {len(universe)}")

    # Check name dict size
    for attr in ["_STOCK_NAMES", "_ETF_NAMES", "_BOND_NAMES"]:
        if hasattr(trader, attr):
            d = getattr(trader, attr)
            print(f"{attr}: {len(d)} entries")
            if d:
                for k, v in list(d.items())[:3]:
                    print(f"  {k} -> {v}")

    # Test with actual positions from state
    state = trader.load_state()
    positions = state.get("positions", {})
    print(f"Positions: {len(positions)}")
    for code in list(positions.keys())[:3]:
        name_func = getattr(trader, func_name)
        name = name_func(code)
        print(f"  {code} -> name='{name}'")

    print()
