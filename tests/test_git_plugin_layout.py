from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import tomllib
import yaml

import kanban_warden

ROOT = Path(__file__).resolve().parents[1]


class HookContext:
    config: dict[str, object] = {}

    def __init__(self) -> None:
        self.hooks: list[tuple[str, object]] = []

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.hooks.append((hook_name, callback))


def _load_root_plugin() -> ModuleType:
    root_init = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("kanban_warden_git_plugin", root_init)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_root_plugin_manifest_supports_hermes_git_install() -> None:
    plugin_yaml = ROOT / "plugin.yaml"

    assert plugin_yaml.exists()
    manifest = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))

    assert manifest["name"] == "kanban-warden"
    assert manifest["version"] == "0.5.0"
    assert manifest["hooks"] == ["pre_tool_call", "transform_tool_result"]
    assert manifest["provides_tools"] == []


def test_root_plugin_entrypoint_delegates_to_runtime_package() -> None:
    root_plugin = _load_root_plugin()
    ctx = HookContext()

    root_plugin.register(ctx)

    assert root_plugin.register is kanban_warden.register
    assert root_plugin.unregister is kanban_warden.unregister
    assert [hook_name for hook_name, _ in ctx.hooks] == [
        "pre_tool_call",
        "transform_tool_result",
    ]


def test_pyproject_no_longer_declares_pip_plugin_entrypoints() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "build-system" not in pyproject
    assert "project" not in pyproject


def test_readme_documents_hermes_git_install_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "hermes plugins install coderlaoma/hermes-kanban-warden" in readme
    assert "hermes plugins update kanban-warden" in readme
    assert "pip install" not in readme
    assert "hermes_agent.plugins" not in readme
