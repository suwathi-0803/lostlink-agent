import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

_tmp = Path(tempfile.mkdtemp(prefix="lostlink-test-"))
os.environ["LOSTLINK_DB"] = str(_tmp / "test.db")
os.environ["LOSTLINK_CHECKPOINTS"] = str(_tmp / "checkpoints.db")
os.environ["OFFICER_TOKEN"] = "test-officer-token"
