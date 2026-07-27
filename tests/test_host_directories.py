import os
import stat
import subprocess
from pathlib import Path

PREPARE_SCRIPT = Path("scripts/prepare_host_directories.sh").resolve()


def test_prepare_host_directories_creates_nested_mount_paths(tmp_path: Path) -> None:
    host_data_dir = tmp_path / "mounted-storage" / "trip-tracker"
    host_backup_dir = host_data_dir / "backups"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"HOST_DATA_DIR={host_data_dir}\nHOST_BACKUP_DIR={host_backup_dir}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(PREPARE_SCRIPT), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert host_data_dir.is_dir()
    assert host_backup_dir.is_dir()
    assert stat.S_IMODE(host_data_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(host_backup_dir.stat().st_mode) == 0o750


def test_prepare_host_directories_prefers_environment_and_preserves_contents(
    tmp_path: Path,
) -> None:
    host_data_dir = tmp_path / "existing-data"
    host_backup_dir = tmp_path / "existing-backups"
    host_data_dir.mkdir()
    marker = host_data_dir / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "HOST_DATA_DIR": str(host_data_dir),
            "HOST_BACKUP_DIR": str(host_backup_dir),
        }
    )
    result = subprocess.run(
        [str(PREPARE_SCRIPT), str(tmp_path / "missing.env")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert host_backup_dir.is_dir()
