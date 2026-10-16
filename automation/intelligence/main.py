"""Compatibility entry point: use START_HERE/4_open_market_insights.py.

The old campaign HTML generator is no longer part of the pipeline.
--cli, --full and --status are handled by the shared launcher engine.
"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

if __name__=='__main__':
    from intelligence_pipeline import main
    raise SystemExit(main())
