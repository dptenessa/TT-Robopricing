"""Open the Market Insights pipeline panel from VS Code or Windows Explorer."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'automation'))

if __name__=='__main__':
    from intelligence_pipeline import main
    raise SystemExit(main())
