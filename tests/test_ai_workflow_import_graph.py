"""AST lock for ai_workflow* import direction, cycles, and host-kernel leafness."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

NEW_BUSINESS_MODULES = frozenset(
    {
        "ai_workflow_declarations",
        "ai_workflow_candidate_state",
        "ai_workflow_authorizations",
        "ai_workflow_verdicts",
        "ai_workflow_ownership",
        "ai_workflow_side_effects",
        "ai_workflow_preflight",
        "ai_workflow_dispatch_policy",
        "ai_workflow_evidence",
    }
)
FORBIDDEN_HOST_TARGETS = frozenset(
    {"ai_workflow", "ai_workflow_repairs", "sync_plugin"}
)

IMPORT_GRAPH_ALLOWED: Mapping[str, frozenset[str]] = {
    "ai_workflow_artifacts": frozenset(),
    "ai_workflow_routing": frozenset(
        {
            "ai_workflow_artifacts",
            "ai_workflow_costs",
            "ai_workflow",
            "ai_workflow_planning",
        }
    ),
    "ai_workflow_planning": frozenset({"ai_workflow_artifacts", "ai_workflow"}),
    "ai_workflow_costs": frozenset({"ai_workflow_artifacts", "ai_workflow"}),
    "ai_workflow_runtime": frozenset({"ai_workflow_artifacts", "ai_workflow"}),
    "ai_workflow_scheduler": frozenset(
        {
            "ai_workflow_artifacts",
            "ai_workflow_planning",
            "ai_workflow",
            "ai_workflow_repairs",
            "ai_workflow_declarations",
        }
    ),
    "ai_workflow_team_call": frozenset(),
    "ai_workflow_repairs": frozenset(
        {
            "ai_workflow",
            "ai_workflow_scheduler",
            "ai_workflow_dispatch_policy",
            "ai_workflow_verdicts",
            "ai_workflow_candidate_state",
            "ai_workflow_authorizations",
            "ai_workflow_side_effects",
            "ai_workflow_ownership",
            "ai_workflow_evidence",
        }
    ),
    "ai_workflow_router_probe": frozenset({"ai_workflow_costs"}),
    "ai_workflow": frozenset(
        {
            "ai_workflow_artifacts",
            "ai_workflow_costs",
            "ai_workflow_planning",
            "ai_workflow_repairs",
            "ai_workflow_routing",
            "ai_workflow_runtime",
            "ai_workflow_scheduler",
            "ai_workflow_team_call",
            "ai_workflow_dispatch_policy",
            "ai_workflow_side_effects",
            "ai_workflow_ownership",
            "ai_workflow_preflight",
            "ai_workflow_declarations",
            "ai_workflow_evidence",
        }
    ),
    "ai_workflow_declarations": frozenset(
        {"ai_workflow_routing", "ai_workflow_artifacts"}
    ),
    "ai_workflow_candidate_state": frozenset(
        {"ai_workflow_artifacts", "ai_workflow_planning"}
    ),
    "ai_workflow_authorizations": frozenset({"ai_workflow_artifacts"}),
    "ai_workflow_verdicts": frozenset(
        {
            "ai_workflow_candidate_state",
            "ai_workflow_authorizations",
            "ai_workflow_artifacts",
        }
    ),
    "ai_workflow_ownership": frozenset(
        {
            "ai_workflow_planning",
            "ai_workflow_authorizations",
            "ai_workflow_artifacts",
        }
    ),
    "ai_workflow_side_effects": frozenset(
        {
            "ai_workflow_ownership",
            "ai_workflow_candidate_state",
            "ai_workflow_artifacts",
        }
    ),
    "ai_workflow_preflight": frozenset(
        {"ai_workflow_declarations", "ai_workflow_artifacts"}
    ),
    "ai_workflow_dispatch_policy": frozenset(
        {
            "ai_workflow_declarations",
            "ai_workflow_preflight",
            "ai_workflow_routing",
            "ai_workflow_ownership",
            "ai_workflow_side_effects",
            "ai_workflow_artifacts",
        }
    ),
    "ai_workflow_evidence": frozenset(
        {
            "ai_workflow_declarations",
            "ai_workflow_preflight",
            "ai_workflow_artifacts",
        }
    ),
}


def _is_type_checking_if(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imported_workflow_modules(node: ast.AST, interesting: frozenset[str]) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in interesting:
                found.append(top)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            top = node.module.split(".")[0]
            if top in interesting:
                found.append(top)
        else:
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in interesting:
                    found.append(top)
    return tuple(found)


def _function_imported_names(tree: ast.AST, source_module: str) -> frozenset[str]:
    names: set[str] = set()

    def walk(node: ast.AST, in_func: bool) -> None:
        if _is_type_checking_if(node):
            return
        for child in ast.iter_child_nodes(node):
            child_in_func = in_func or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            )
            if child_in_func and isinstance(child, ast.ImportFrom) and child.module:
                top = child.module.split(".")[0]
                if top == source_module:
                    names.update(alias.name for alias in child.names)
            walk(child, child_in_func)

    walk(tree, False)
    return frozenset(names)


def _scan_import_graph() -> dict[str, dict[str, object]]:
    interesting = frozenset(IMPORT_GRAPH_ALLOWED) | NEW_BUSINESS_MODULES | frozenset(
        {"sync_plugin"}
    )
    result: dict[str, dict[str, object]] = {}
    for path in sorted(SCRIPTS.glob("ai_workflow*.py")):
        source = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_edges: set[str] = set()
        function_edges: set[str] = set()
        saw_function_import = False

        def walk(node: ast.AST, in_func: bool) -> None:
            nonlocal saw_function_import
            if _is_type_checking_if(node):
                return
            for child in ast.iter_child_nodes(node):
                child_in_func = in_func or isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                )
                imported = _imported_workflow_modules(child, interesting)
                if imported:
                    if child_in_func:
                        function_edges.update(imported)
                        saw_function_import = True
                    else:
                        module_edges.update(imported)
                walk(child, child_in_func)

        walk(tree, False)
        result[source] = {
            "tree": tree,
            "module_edges": frozenset(module_edges - {source}),
            "function_edges": frozenset(function_edges - {source}),
            "edges": frozenset((module_edges | function_edges) - {source}),
            "saw_function_import": saw_function_import,
        }
    return result


def _cycles(graph: Mapping[str, frozenset[str]]) -> tuple[tuple[str, ...], ...]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    found: list[tuple[str, ...]] = []

    def dfs(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            start = stack.index(node)
            found.append(tuple(stack[start:] + [node]))
            return
        visiting.add(node)
        stack.append(node)
        for dest in sorted(graph.get(node, frozenset())):
            if dest in graph:
                dfs(dest)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for source in sorted(graph):
        dfs(source)
    return tuple(found)


class ImportGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scanned = _scan_import_graph()

    def test_scan_covers_function_level_imports(self):
        self.assertTrue(
            any(entry["saw_function_import"] for entry in self.scanned.values()),
            "AST scan must visit import nodes inside function bodies",
        )
        routing = self.scanned["ai_workflow_routing"]
        self.assertIn("ai_workflow", routing["function_edges"])

    def test_routing_no_longer_function_imports_write_json_once_from_host(self):
        tree = self.scanned["ai_workflow_routing"]["tree"]
        imported = _function_imported_names(tree, "ai_workflow")
        self.assertNotIn("write_json_once", imported)
        source = (SCRIPTS / "ai_workflow_routing.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\ndef _write_json_once\(")

    def test_allowed_table_covers_all_existing_edges(self):
        for source, entry in self.scanned.items():
            allowed = IMPORT_GRAPH_ALLOWED.get(source, frozenset())
            unexpected = entry["edges"] - allowed
            self.assertFalse(
                unexpected,
                f"{source} has edges not in IMPORT_GRAPH_ALLOWED: {sorted(unexpected)}",
            )

    def test_new_business_modules_do_not_import_host(self):
        for source in NEW_BUSINESS_MODULES:
            if source not in self.scanned:
                continue
            forbidden = self.scanned[source]["edges"] & FORBIDDEN_HOST_TARGETS
            self.assertFalse(
                forbidden,
                f"{source} must not import {sorted(forbidden)}",
            )

    def test_leaf_and_new_modules_are_acyclic(self):
        existing_new = {
            name: self.scanned[name]["edges"]
            for name in ("ai_workflow_artifacts", *NEW_BUSINESS_MODULES)
            if name in self.scanned
        }
        self.assertEqual((), _cycles(existing_new))
        planned = {
            name: dests
            for name, dests in IMPORT_GRAPH_ALLOWED.items()
            if name in NEW_BUSINESS_MODULES or name == "ai_workflow_artifacts"
        }
        self.assertEqual((), _cycles(planned))

    def test_planned_ownership_and_repairs_edges_are_declared(self):
        self.assertIn(
            "ai_workflow_authorizations",
            IMPORT_GRAPH_ALLOWED["ai_workflow_ownership"],
        )
        self.assertIn(
            "ai_workflow_dispatch_policy",
            IMPORT_GRAPH_ALLOWED["ai_workflow_repairs"],
        )

    def test_preflight_module_exists_within_allowed_edges(self):
        self.assertIn("ai_workflow_preflight", self.scanned)
        self.assertEqual(
            IMPORT_GRAPH_ALLOWED["ai_workflow_preflight"],
            self.scanned["ai_workflow_preflight"]["edges"],
        )
        self.assertFalse(
            self.scanned["ai_workflow_preflight"]["edges"] & FORBIDDEN_HOST_TARGETS
        )

    def test_dispatch_policy_module_exists_within_allowed_edges(self):
        self.assertIn("ai_workflow_dispatch_policy", self.scanned)
        self.assertEqual(
            IMPORT_GRAPH_ALLOWED["ai_workflow_dispatch_policy"],
            self.scanned["ai_workflow_dispatch_policy"]["edges"],
        )
        self.assertFalse(
            self.scanned["ai_workflow_dispatch_policy"]["edges"] & FORBIDDEN_HOST_TARGETS
        )

    def test_evidence_module_exists_within_allowed_edges(self):
        self.assertIn("ai_workflow_evidence", self.scanned)
        self.assertEqual(
            IMPORT_GRAPH_ALLOWED["ai_workflow_evidence"],
            self.scanned["ai_workflow_evidence"]["edges"],
        )
        self.assertFalse(
            self.scanned["ai_workflow_evidence"]["edges"] & FORBIDDEN_HOST_TARGETS
        )


if __name__ == "__main__":
    unittest.main()
