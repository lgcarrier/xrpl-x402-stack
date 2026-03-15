import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_SRC_ROOTS = [
    PROJECT_ROOT / "packages" / "core" / "src",
    PROJECT_ROOT / "packages" / "facilitator" / "src",
    PROJECT_ROOT / "packages" / "middleware" / "src",
    PROJECT_ROOT / "packages" / "client" / "src",
]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for package_src_root in PACKAGE_SRC_ROOTS:
    if str(package_src_root) not in sys.path:
        sys.path.insert(0, str(package_src_root))
