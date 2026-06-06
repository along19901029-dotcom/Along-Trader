"""CLI entry point for backtesting.

Usage:
    python -m backtest.main --agent us --start 20250101 --end 20250601
    python -m backtest.main --agent ashare --start 20250301 --end 20250601 --no-llm
    python -m backtest.main --agent us --start 20250601 --end 20250605 -v
"""

import argparse
import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.runner import run_backtest
from backtest.report import print_report, send_report_email


def main():
    parser = argparse.ArgumentParser(description="AI-Trader Backtesting Tool")
    parser.add_argument("--agent", "-a", choices=["us", "ashare", "etf", "bond"],
                        default="us", help="Agent to backtest (default: us)")
    parser.add_argument("--start", "-s", default="20250601",
                        help="Start date YYYYMMDD (default: 20250601)")
    parser.add_argument("--end", "-e", default="20250605",
                        help="End date YYYYMMDD (default: 20250605)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM calls, use cache only")
    parser.add_argument("--cache-dir", default="backtest_cache",
                        help="LLM cache directory (default: backtest_cache)")
    parser.add_argument("--send", action="store_true",
                        help="Send HTML report via email")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose progress output")
    args = parser.parse_args()

    print("AI-Trader Backtest — {} Agent".format(args.agent.upper()))
    print("Date range: {} to {}".format(args.start, args.end))
    print()

    result = run_backtest(
        agent_name=args.agent,
        start_date=args.start,
        end_date=args.end,
        use_llm=not args.no_llm,
        cache_dir=args.cache_dir,
        verbose=args.verbose or True,
    )

    print_report(result)
    if args.send:
        send_report_email(result)


if __name__ == "__main__":
    main()
