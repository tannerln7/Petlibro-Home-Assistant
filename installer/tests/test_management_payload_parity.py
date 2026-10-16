from pathlib import Path

from installer.payload import APP_START_TEMPLATE, INSTALLER


ROOT = Path(__file__).resolve().parents[2]


def test_native_worker_shell_assets_match_installer_reference() -> None:
    assets = ROOT / "management" / "worker" / "assets"
    assert (assets / "bootstrap-installer.sh").read_bytes() == INSTALLER
    assert (assets / "app-start.sh").read_text(encoding="ascii") == APP_START_TEMPLATE
