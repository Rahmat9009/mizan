"""Invariant 23 - Dispatch Addendum section 2 / Master Plan E8: the deterministic path has ZERO LLM dependencies.

Pass criterion: static, not behavioural. No module under mizan/risk, mizan/policy or mizan/governor imports an
LLM client, an HTTP client, or mizan.advisory - checked by parsing the AST, so a lazily-imported client inside a
function body is caught exactly like a top-level one. The lane brief states it plainly: "If your module imports
an LLM client, you have failed."

Behavioural coverage lives in invariant 13 (the engine runs with the LLM offline). This one closes the door the
other way round: 13 could pass on a module that imports a client and merely tolerates its absence. A deterministic
verdict that depends on a network at all is not deterministic.
"""
from __future__ import annotations

import ast

from tests.invariants._support import imported_modules, offenders_message, parse, python_files, rel

#: The deterministic path. mizan.advisory is deliberately NOT in this list - it is the semantic layer.
DETERMINISTIC_PACKAGES = ("risk", "policy", "governor")

#: Import roots that would put a model, a provider or a socket into the deterministic path.
FORBIDDEN_ROOTS = frozenset({
    "openai", "anthropic", "featherless", "litellm", "langchain", "langchain_openai", "langchain_core",
    "google", "cohere", "mistralai", "ollama", "transformers", "torch", "llama_cpp", "vllm",
    "httpx", "requests", "aiohttp", "urllib3", "socket", "http",
})

#: mizan.advisory is the LLM boundary; the deterministic path must not reach across it.
FORBIDDEN_MIZAN = ("mizan.advisory",)


def _offenders() -> list[str]:
    found: list[str] = []
    for path in python_files(*DETERMINISTIC_PACKAGES):
        for module, lineno in imported_modules(parse(path)):
            root = module.split(".")[0]
            if root in FORBIDDEN_ROOTS or any(
                module == m or module.startswith(m + ".") for m in FORBIDDEN_MIZAN
            ):
                found.append(f"{rel(path)}:{lineno} imports {module}")
    return sorted(found)


def test_no_llm_in_deterministic_path():
    offenders = _offenders()
    assert not offenders, offenders_message(
        "the deterministic path must have zero LLM/network dependencies", offenders
    )


def test_the_check_actually_inspects_something():
    """A scan that silently matched no files would pass this invariant for the wrong reason."""
    files = [p for p in python_files(*DETERMINISTIC_PACKAGES)]
    assert len(files) >= 3, f"expected the deterministic path to contain modules; found {len(files)}"
    parsed = [parse(p) for p in files]
    assert any(isinstance(n, ast.Import | ast.ImportFrom) for tree in parsed for n in ast.walk(tree)), (
        "no imports were parsed at all, so the forbidden-import scan proves nothing"
    )
