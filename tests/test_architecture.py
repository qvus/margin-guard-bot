"""Automated Clean Architecture & secure-coding fitness tests."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "margin_guard"


def _iter_python_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def _extract_imported_modules(file_path: Path) -> set[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_domain_layer_has_zero_framework_or_outer_layer_dependencies() -> None:
    forbidden_prefixes = (
        "margin_guard.application",
        "margin_guard.infrastructure",
        "margin_guard.presentation",
        "margin_guard.bootstrap",
        "margin_guard.config",
        "aiogram",
        "httpx",
        "sqlite3",
        "cryptography",
        "pydantic",
        "apscheduler",
    )
    for py_file in _iter_python_files(SRC_ROOT / "domain"):
        for mod in _extract_imported_modules(py_file):
            assert not mod.startswith(forbidden_prefixes), (
                f"Domain file {py_file.name} illegally imports {mod}"
            )


def test_application_layer_depends_only_on_domain_and_stdlib() -> None:
    forbidden_prefixes = (
        "margin_guard.infrastructure",
        "margin_guard.presentation",
        "margin_guard.bootstrap",
        "margin_guard.config",
        "aiogram",
        "httpx",
        "sqlite3",
        "cryptography",
        "pydantic",
        "apscheduler",
    )
    for py_file in _iter_python_files(SRC_ROOT / "application"):
        for mod in _extract_imported_modules(py_file):
            assert not mod.startswith(forbidden_prefixes), (
                f"Application file {py_file} illegally imports {mod}"
            )


def test_presentation_layer_does_not_import_persistence_crypto_or_http_drivers() -> None:
    forbidden_prefixes = (
        "margin_guard.infrastructure",
        "sqlite3",
        "cryptography",
        "httpx",
    )
    for py_file in _iter_python_files(SRC_ROOT / "presentation"):
        for mod in _extract_imported_modules(py_file):
            assert not mod.startswith(forbidden_prefixes), (
                f"Presentation file {py_file} illegally imports infrastructure module {mod}"
            )


def test_production_source_contains_zero_assert_statements() -> None:
    for py_file in _iter_python_files(SRC_ROOT):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        asserts = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
        assert not asserts, f"Found forbidden assert statement in {py_file}"
