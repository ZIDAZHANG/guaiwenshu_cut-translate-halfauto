"""Start the existing desktop application from this repository layout."""
from pathlib import Path
import runpy
import sys

tools = Path(__file__).resolve().parents[1] / "app/tools"
sys.path.insert(0, str(tools))
runpy.run_path(str(tools / "one_click_roast.py"), run_name="__main__")
