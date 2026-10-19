"""Exact source binding and conservative Python change classification."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


class _Normalize(ast.NodeTransformer):
    def __init__(self, mask_literals=False):
        self.mask_literals = mask_literals

    def visit_Constant(self, node):
        return ast.Constant(value='<literal>') if self.mask_literals else node

    def generic_visit(self, node):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:]
        return super().generic_visit(node)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def source_snapshot(entry: Path, root: Path | None = None, dependencies=()) -> dict:
    """Follow local static imports, never execute code or scan backup files.

    Dynamic imports, non-Python inputs and external packages need explicit
    dependencies/toolchain identity. AST changes are review leads, not a proof
    that a changed statement is reached at runtime.
    """
    entry = entry.resolve()
    root = (root or entry.parent).resolve()
    if not entry.is_relative_to(root):
        raise ValueError('entry must be inside source_root')
    pending = [entry, *(Path(p).resolve() for p in dependencies)]
    files, semantics, structures, unknown = {}, {}, {}, []

    def add_module(parts):
        path = root.joinpath(*parts)
        for candidate in (path.with_suffix('.py'), path / '__init__.py'):
            if candidate.is_file():
                pending.append(candidate.resolve())

    while pending:
        path = pending.pop()
        if str(path) in files:
            continue
        if len(files) >= 128:
            raise ValueError('source closure exceeds 128 files; use a smaller source_root')
        data = path.read_bytes()
        key = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        files[str(path)] = _hash(data)
        if path.suffix != '.py':
            semantics[key] = structures[key] = _hash(data)
            continue
        tree = ast.parse(data, filename=str(path))
        semantics[key] = _hash(ast.dump(_Normalize().visit(ast.parse(data)), include_attributes=False).encode())
        structures[key] = _hash(ast.dump(_Normalize(True).visit(ast.parse(data)), include_attributes=False).encode())
        if not path.is_relative_to(root):
            unknown.append(f'{path}: imports outside source_root require explicit dependencies')
            continue
        package = list(path.parent.relative_to(root).parts)
        for i in range(len(package) + 1):
            init = root.joinpath(*package[:i], '__init__.py')
            if init.is_file():
                pending.append(init.resolve())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split('.')
                    for i in range(1, len(parts) + 1):
                        add_module(parts[:i])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package) + 1:
                        unknown.append(f'{path}:{node.lineno}: relative import escapes source_root')
                        continue
                    prefix = package[:len(package) - node.level + 1]
                else:
                    prefix = []
                parts = prefix + (node.module.split('.') if node.module else [])
                for i in range(1, len(parts) + 1):
                    add_module(parts[:i])
                for alias in node.names:
                    if alias.name != '*':
                        add_module(parts + [alias.name])
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == '__import__'
                or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'
            ):
                unknown.append(f'{path}:{node.lineno}: dynamic import requires explicit dependencies')
    return {'entry': str(entry), 'root': str(root), 'dependencies': [str(Path(p).resolve()) for p in dependencies],
            'files': files, 'semantic_sha256': _hash(json.dumps(semantics, sort_keys=True).encode()),
            'structure_sha256': _hash(json.dumps(structures, sort_keys=True).encode()),
            'coverage': 'local_static_imports_and_explicit_dependencies', 'unknown': unknown}


def refresh(snapshot):
    return source_snapshot(Path(snapshot['entry']), Path(snapshot['root']), snapshot['dependencies'])


def change_kind(baseline, current):
    if baseline['semantic_sha256'] == current['semantic_sha256']:
        return 'unchanged'
    if baseline['structure_sha256'] == current['structure_sha256']:
        return 'parameters'
    return 'implementation_candidate'
