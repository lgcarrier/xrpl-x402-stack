from __future__ import annotations

from pathlib import Path
import shutil

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class build_hook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        license_path = Path(self.root) / "LICENSE"
        self._created_license = False
        if license_path.exists():
            return

        source_path = Path(self.root).parents[1] / "LICENSE"
        if source_path.exists():
            shutil.copyfile(source_path, license_path)
            self._created_license = True

    def finalize(self, version: str, build_data: dict[str, object], artifact_path: str) -> None:
        if not getattr(self, "_created_license", False):
            return

        license_path = Path(self.root) / "LICENSE"
        if license_path.exists():
            license_path.unlink()
