from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import yaml


LABEL = "com.local.wechat-public-account-archive"
STATUS_LABEL = "com.local.wechat-public-account-archive-status"
PRODUCT_DIRECTORY = "WeChatPublicAccountArchive"
STATUS_APP_NAME = "WeChatArchiveStatus"


class LaunchAgentManager:
    def __init__(
        self,
        project_root: str | Path,
        *,
        product_root: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.product_root = Path(
            product_root
            or Path.home() / "Library" / "Application Support" / PRODUCT_DIRECTORY
        ).expanduser().resolve()
        self.runtime_root = self.product_root / "runtime"
        self.database_path = self.product_root / "data" / "archive.sqlite"
        self.output_dir = self.product_root / "output"
        self.logs_dir = self.product_root / "logs"
        self.plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.status_plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{STATUS_LABEL}.plist"
        )
        self.status_app_path = self.product_root / f"{STATUS_APP_NAME}.app"

    def install(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        source_database: str | Path | None = None,
        config: dict[str, Any] | None = None,
        install_status_item: bool = True,
    ) -> Path:
        self._validate_project()
        self._bootout(self.plist_path, ignore_errors=True)
        self._bootout(self.status_plist_path, ignore_errors=True)
        self._install_database(source_database)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._install_runtime(config)

        python = self.runtime_root / ".venv" / "bin" / "python"
        entrypoint = self.runtime_root / "main.py"
        installed_config = self.runtime_root / "config.yaml"
        payload: dict[str, Any] = {
            "Label": LABEL,
            "ProgramArguments": [
                str(python),
                str(entrypoint),
                "--config",
                str(installed_config),
                "web",
                "--host",
                host,
                "--port",
                str(port),
            ],
            "WorkingDirectory": str(self.runtime_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(self.logs_dir / "automation.stdout.log"),
            "StandardErrorPath": str(self.logs_dir / "automation.stderr.log"),
        }
        self._write_plist(self.plist_path, payload)
        self._bootstrap(self.plist_path)
        if install_status_item:
            # Preserve an existing authorized app bundle. Replacing its binary
            # can make macOS treat it as a new responsible process and ask for
            # Accessibility approval again even though the UI code is unchanged.
            if not self._status_app_ready():
                self._install_status_app()
            self._write_plist(self.status_plist_path, self._status_agent_payload())
            self._bootstrap(self.status_plist_path)
        else:
            self.status_plist_path.unlink(missing_ok=True)
        return self.plist_path

    def uninstall(self) -> None:
        self._bootout(self.status_plist_path, ignore_errors=True)
        self._bootout(self.plist_path, ignore_errors=True)
        self.status_plist_path.unlink(missing_ok=True)
        self.plist_path.unlink(missing_ok=True)

    def status(self) -> dict[str, Any]:
        result = subprocess.run(
            ["launchctl", "print", f"{self._domain()}/{LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        status_result = subprocess.run(
            ["launchctl", "print", f"{self._domain()}/{STATUS_LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        health = self._healthcheck()
        return {
            "label": LABEL,
            "installed": self.plist_path.exists(),
            "loaded": result.returncode == 0,
            "healthy": health is not None,
            "health": health,
            "plist_path": str(self.plist_path),
            "status_item_label": STATUS_LABEL,
            "status_item_installed": self.status_plist_path.exists() and self.status_app_path.exists(),
            "status_item_loaded": status_result.returncode == 0,
            "status_item_plist_path": str(self.status_plist_path),
            "status_app_path": str(self.status_app_path),
            "product_root": str(self.product_root),
            "database_path": str(self.database_path),
            "output_dir": str(self.output_dir),
            "details": result.stdout.strip() if result.returncode == 0 else "",
        }

    def _install_status_app(self) -> None:
        source = self.project_root / "wechat_crawler" / "macos_status" / "StatusApp.swift"
        if not source.exists():
            raise FileNotFoundError(f"status app source not found: {source}")
        compiler = shutil.which("swiftc")
        if not compiler:
            raise FileNotFoundError("swiftc is required to install the macOS status app")

        temporary = self.product_root / f"{STATUS_APP_NAME}.next.app"
        if temporary.exists():
            shutil.rmtree(temporary)
        executable_dir = temporary / "Contents" / "MacOS"
        executable_dir.mkdir(parents=True)
        executable = executable_dir / STATUS_APP_NAME
        result = subprocess.run(
            [
                compiler,
                "-swift-version",
                "5",
                "-parse-as-library",
                "-framework",
                "AppKit",
                "-framework",
                "Foundation",
                "-framework",
                "ApplicationServices",
                str(source),
                "-o",
                str(executable),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError((result.stderr or result.stdout or "status app compilation failed").strip())
        info = {
            "CFBundleDisplayName": "微信公众号归档",
            "CFBundleExecutable": STATUS_APP_NAME,
            "CFBundleIdentifier": STATUS_LABEL,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": STATUS_APP_NAME,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
        }
        with (temporary / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(info, handle, sort_keys=True)
        if self.status_app_path.exists():
            shutil.rmtree(self.status_app_path)
        temporary.replace(self.status_app_path)

    def _status_app_ready(self) -> bool:
        executable = self.status_app_path / "Contents" / "MacOS" / STATUS_APP_NAME
        info_path = self.status_app_path / "Contents" / "Info.plist"
        if not executable.is_file() or not os.access(executable, os.X_OK) or not info_path.is_file():
            return False
        try:
            with info_path.open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return False
        return info.get("CFBundleIdentifier") == STATUS_LABEL

    def _status_agent_payload(self) -> dict[str, Any]:
        return {
            "Label": STATUS_LABEL,
            "ProgramArguments": [
                str(self.status_app_path / "Contents" / "MacOS" / STATUS_APP_NAME),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
            "StandardOutPath": str(self.logs_dir / "status-item.stdout.log"),
            "StandardErrorPath": str(self.logs_dir / "status-item.stderr.log"),
        }

    @staticmethod
    def _write_plist(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".plist.tmp")
        with temporary.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
        temporary.replace(path)

    def _bootstrap(self, plist_path: Path) -> None:
        result = subprocess.run(
            ["launchctl", "bootstrap", self._domain(), str(plist_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "launchctl bootstrap failed").strip())

    def _install_database(self, source_database: str | Path | None) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.exists() or not source_database:
            return
        source = Path(source_database).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"source database not found: {source}")
        temporary = self.database_path.with_suffix(".sqlite.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(self.database_path)

    def _install_runtime(self, config: dict[str, Any] | None) -> None:
        temporary = self.product_root / "runtime.next"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        shutil.copy2(self.project_root / "main.py", temporary / "main.py")
        shutil.copytree(
            self.project_root / "wechat_crawler",
            temporary / "wechat_crawler",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        shutil.copytree(
            self.project_root / ".venv",
            temporary / ".venv",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        installed_config = json.loads(json.dumps(config or self._read_project_config()))
        installed_config.setdefault("database", {})["path"] = str(self.database_path)
        installed_config.setdefault("output", {})["path"] = str(self.output_dir)
        (temporary / "config.yaml").write_text(
            yaml.safe_dump(installed_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        if self.runtime_root.exists():
            shutil.rmtree(self.runtime_root)
        temporary.replace(self.runtime_root)

    def _read_project_config(self) -> dict[str, Any]:
        config_path = self.project_root / "config.yaml"
        if not config_path.exists():
            return {}
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    def _validate_project(self) -> None:
        required = (
            self.project_root / ".venv" / "bin" / "python",
            self.project_root / "main.py",
            self.project_root / "wechat_crawler",
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError(f"project runtime is incomplete: {', '.join(missing)}")

    def _healthcheck(self) -> dict[str, Any] | None:
        if not self.plist_path.exists():
            return None
        try:
            with self.plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload.get("ProgramArguments", [])
            port_index = arguments.index("--port") + 1
            host_index = arguments.index("--host") + 1
            url = f"http://{arguments[host_index]}:{arguments[port_index]}/api/health"
            with urlopen(url, timeout=1.5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError, URLError):
            return None

    def _bootout(self, plist_path: Path, *, ignore_errors: bool) -> None:
        result = subprocess.run(
            ["launchctl", "bootout", self._domain(), str(plist_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and not ignore_errors:
            raise RuntimeError((result.stderr or result.stdout or "launchctl bootout failed").strip())

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"
