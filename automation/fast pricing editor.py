
from pricing_editor.main_window import run
import sys

if __name__ == "__main__":
    workspace = "campaigns" if "--campaigns" in sys.argv[1:] else "prices"
    run(initial_workspace=workspace)
