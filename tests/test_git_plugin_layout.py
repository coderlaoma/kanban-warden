from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]


class HookContext:
    config: dict[str, object] = {}

    def __init__(self) -> None:
        self.hooks: list[tuple[str, object]] = []

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.hooks.append((hook_name, callback))


@contextmanager
def _without_top_level_runtime_package() -> Iterator[None]:
    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "kanban_warden" or name.startswith("kanban_warden.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    sys.path = [
        entry
        for entry in sys.path
        if Path(entry or ".").resolve() != ROOT
    ]
    try:
        yield
    finally:
        sys.path = saved_path
        for name in list(sys.modules):
            if name == "kanban_warden" or name.startswith("kanban_warden."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def _load_root_plugin() -> ModuleType:
    root_init = ROOT / "__init__.py"
    parent_name = "hermes_plugins"
    module_name = "hermes_plugins.kanban_warden"
    if parent_name not in sys.modules:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []
        parent.__package__ = parent_name
        sys.modules[parent_name] = parent
    for name in list(sys.modules):
        if name == module_name or name.startswith(f"{module_name}."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        root_init,
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(ROOT)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_root_plugin_manifest_supports_hermes_git_install() -> None:
    plugin_yaml = ROOT / "plugin.yaml"

    assert plugin_yaml.exists()
    manifest = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))

    assert manifest["name"] == "kanban-warden"
    assert manifest["version"] == "0.8.4"
    assert manifest["hooks"] == ["pre_tool_call", "transform_tool_result"]
    assert manifest["provides_tools"] == []


def test_root_plugin_entrypoint_loads_as_hermes_directory_plugin() -> None:
    with _without_top_level_runtime_package():
        root_plugin = _load_root_plugin()
        runtime_plugin = importlib.import_module(
            "hermes_plugins.kanban_warden.kanban_warden"
        )
        ctx = HookContext()

        root_plugin.register(ctx)

        assert root_plugin.register is runtime_plugin.register
        assert root_plugin.unregister is runtime_plugin.unregister
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

    assert (
        "hermes --profile hairou-feishu plugins install "
        "coderlaoma/hermes-kanban-warden --enable"
    ) in readme
    assert "hermes --profile hairou-feishu plugins update kanban-warden" in readme
    assert "pip install" not in readme
    assert "hermes_agent.plugins" not in readme


def test_after_install_documents_profile_scoped_activation() -> None:
    after_install = (ROOT / "after-install.md").read_text(encoding="utf-8")

    assert "hermes --profile <profile> plugins enable kanban-warden" in after_install
    assert (
        "hermes --profile <profile> plugins install "
        "coderlaoma/hermes-kanban-warden --force --enable"
    ) in after_install
    assert "profile-scoped gateway" in after_install
