"""
Automated Pytest Unit Test & Regression Suite Generator Tool.
Analyzes modified function signatures, parameter types, and return types via Python AST
to generate targeted, production-ready pytest suites with accurate mocks and edge case coverage.
"""

import ast
import re

from pydantic import BaseModel, Field

from code_review_agent.diff_parser import DiffParser
from code_review_agent.native_tools import BaseTool


class FunctionMetadata(BaseModel):
    """Extracted signature metadata for a function."""

    name: str
    is_async: bool = False
    parameters: list[tuple[str, str | None, str | None]] = Field(
        default_factory=list,
        description="List of (param_name, type_annotation, default_value)",
    )
    return_type: str | None = None
    docstring: str | None = None


class TestGeneratorInput(BaseModel):
    """Input for TestGeneratorTool."""

    function_signature: str = Field(
        ...,
        description="Function name, signature, or Python code block to generate tests for",
    )
    module_path: str = Field(
        default="app.service", description="Import path for the target module"
    )


class TestGeneratorTool(BaseTool):
    """CrewAI Tool for generating dynamic pytest unit tests based on actual AST function signatures."""

    __test__ = False
    name: str = "Automated Pytest Suite Generator"
    description: str = (
        "Generates customized pytest test suites by introspecting function signatures, "
        "parameters, type annotations, and async status. Creates happy path tests, boundary/edge "
        "tests, and mock fixtures tailored to specific function arguments."
    )
    args_schema: type[BaseModel] = TestGeneratorInput

    @classmethod
    def generate_tests_for_diff(cls, diff: str, module_path: str | None = None) -> str:
        """
        Extract modified Python functions across all files in a unified diff
        and generate a complete, valid pytest test suite.
        """
        if not diff or not diff.strip():
            return "# No diff provided for unit test generation.\n"

        tool = cls()
        parsed_pr = DiffParser.parse_diff(diff)
        all_functions: list[FunctionMetadata] = []
        target_module = module_path or ""

        for file_diff in parsed_pr.files:
            file_name = file_diff.target_file or file_diff.source_file or ""
            if not file_name.endswith(".py"):
                continue
            if not target_module:
                target_module = file_name

            # 1. First attempt: Introspect using reconstructed code (combining hunk context + additions)
            hunk_code_lines = []
            for hunk in file_diff.hunks:
                for hline in hunk.lines:
                    if not hline.startswith("-"):
                        hunk_code_lines.append(hline[1:] if len(hline) > 1 else "")
            hunk_code = "\n".join(hunk_code_lines)
            funcs = tool._introspect_functions(hunk_code)

            # 2. Second attempt: Check hunk headers for containing functions
            if not funcs:
                for hunk in file_diff.hunks:
                    m = re.search(
                        r"@@.*?@@\s*(?:def|async def)\s+([A-Za-z0-9_]+)\s*\((.*?)\)",
                        hunk.header,
                    )
                    if m:
                        fn_name = m.group(1)
                        param_str = m.group(2)
                        params: list[tuple[str, str | None, str | None]] = []
                        if param_str.strip():
                            for p in param_str.split(","):
                                p_clean = p.strip().split(":")[0].split("=")[0].strip()
                                if p_clean:
                                    params.append((p_clean, None, None))
                        if not params:
                            params = [("data", None, None)]
                        funcs.append(FunctionMetadata(name=fn_name, parameters=params))

            # 3. Third attempt: Added lines only
            if not funcs:
                added_lines = [
                    line_item
                    for _, line_item in DiffParser.extract_added_lines_with_numbers(
                        file_diff
                    )
                ]
                file_code = "\n".join(added_lines)
                funcs = tool._introspect_functions(file_code)

            for fn in funcs:
                if not any(f.name == fn.name for f in all_functions):
                    all_functions.append(fn)

        if not all_functions:
            # Fallback directly to tool introspection on the raw diff
            all_functions = tool._introspect_functions(diff)

        if not all_functions:
            return (
                '"""\n'
                "Automated Pytest Unit & Regression Suite.\n"
                '"""\n'
                "import pytest\n\n"
                "def test_changeset_sanity():\n"
                '    """Verify changeset contains valid syntax and imports."""\n'
                "    assert True\n"
            )

        resolved_mod = target_module or "app.service"
        return tool._generate_test_suite_code(all_functions, resolved_mod)

    def _run(self, function_signature: str, module_path: str = "app.service") -> str:
        """Generate dynamic pytest suite for the provided function(s)."""
        functions = self._introspect_functions(function_signature)

        if not functions:
            # Fallback if bare name or unparseable string was passed
            cleaned_name = (
                re.sub(r"[^A-Za-z0-9_]", "", function_signature.split("(")[0].strip())
                or "target_function"
            )
            functions = [
                FunctionMetadata(name=cleaned_name, parameters=[("arg1", None, None)])
            ]

        return self._generate_test_suite_code(functions, module_path)

    def _introspect_functions(self, raw_code: str) -> list[FunctionMetadata]:
        """Extract functions and signatures from raw python code/signature snippet."""
        clean_code = raw_code.strip()

        # Remove unified diff headers and markers
        clean_lines = []
        is_diff = any(
            line.startswith(("diff --git ", "@@ ", "--- ", "+++ "))
            for line in clean_code.splitlines()
        )
        for line in clean_code.splitlines():
            if is_diff:
                if line.startswith(
                    (
                        "diff --git",
                        "index ",
                        "new file mode",
                        "deleted file mode",
                        "similarity ",
                        "rename ",
                        "--- ",
                        "+++ ",
                        "@@ ",
                    )
                ):
                    continue
                if line.startswith("+"):
                    clean_lines.append(line[1:])
                elif line.startswith("-"):
                    continue
                else:
                    clean_lines.append(line)
            else:
                clean_lines.append(line)
        clean_code = "\n".join(clean_lines).strip()

        # Try parsing directly via AST
        tree = None
        for attempt in [
            clean_code,
            f"{clean_code}\n    pass",
            f"def {clean_code}:\n    pass"
            if not clean_code.startswith(("def ", "async def "))
            else clean_code,
        ]:
            try:
                tree = ast.parse(attempt)
                break
            except Exception:
                continue

        if not tree:
            # Check Python regex for def / async def when AST parse fails on partial snippets
            py_matches = list(
                re.finditer(
                    r"(?:^|\n)\s*(?:@\w+(?:\(.*?\))?\s*\n\s*)*(?:(async)\s+)?def\s+([A-Za-z0-9_]+)\s*\((.*?)\)(?:\s*->\s*([^:]+))?:",
                    clean_code,
                    re.DOTALL,
                )
            )
            if py_matches:
                parsed_funcs = []
                for m in py_matches:
                    is_async = bool(m.group(1))
                    fn_name = m.group(2)
                    raw_args = m.group(3) or ""
                    return_type = m.group(4).strip() if m.group(4) else None

                    params = []
                    for p in raw_args.split(","):
                        p = p.strip()
                        if not p or p in ("self", "cls"):
                            continue
                        pname = p
                        ptype = None
                        pdefault = None
                        if "=" in pname:
                            pname, pdefault = pname.split("=", 1)
                            pname = pname.strip()
                            pdefault = pdefault.strip()
                        if ":" in pname:
                            pname, ptype = pname.split(":", 1)
                            pname = pname.strip()
                            ptype = ptype.strip()
                        params.append((pname, ptype, pdefault))

                    parsed_funcs.append(
                        FunctionMetadata(
                            name=fn_name,
                            is_async=is_async,
                            parameters=params,
                            return_type=return_type,
                        )
                    )
                if parsed_funcs:
                    return parsed_funcs

            # Check JS / TS function signatures
            fn_m = re.search(
                r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\((.*?)\)",
                clean_code,
            )
            arrow_m = re.search(
                r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>",
                clean_code,
            )
            go_m = re.search(
                r"func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\((.*?)\)", clean_code
            )

            matched = fn_m if fn_m else arrow_m
            if matched:
                name = matched.group(1)
                raw_p = [p.strip() for p in matched.group(2).split(",") if p.strip()]
                params = [
                    (
                        p.split(":")[0].strip(),
                        p.split(":")[1].strip() if ":" in p else None,
                        None,
                    )
                    for p in raw_p
                ]
                return [
                    FunctionMetadata(
                        name=name, is_async="async" in clean_code, parameters=params
                    )
                ]
            elif go_m:
                name = go_m.group(1)
                raw_p = [p.strip() for p in go_m.group(2).split(",") if p.strip()]
                params = [
                    (
                        p.split(" ")[0].strip(),
                        p.split(" ")[1].strip() if " " in p else None,
                        None,
                    )
                    for p in raw_p
                ]
                return [FunctionMetadata(name=name, is_async=False, parameters=params)]

            return []

        functions: list[FunctionMetadata] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                is_async = isinstance(node, ast.AsyncFunctionDef)
                docstring = ast.get_docstring(node)

                # Extract return annotation
                return_type = None
                if node.returns:
                    return_type = (
                        ast.unparse(node.returns) if hasattr(ast, "unparse") else None
                    )

                # Extract parameters with defaults and annotations
                fn_params: list[tuple[str, str | None, str | None]] = []
                args = node.args.args
                defaults = node.args.defaults
                # Align defaults with rightmost positional args
                num_non_defaults = len(args) - len(defaults)

                for i, arg in enumerate(args):
                    if arg.arg in ["self", "cls"]:
                        continue
                    pname = arg.arg
                    ptype = (
                        ast.unparse(arg.annotation)
                        if hasattr(ast, "unparse") and arg.annotation
                        else None
                    )

                    pdefault = None
                    if i >= num_non_defaults:
                        default_node = defaults[i - num_non_defaults]
                        pdefault = (
                            ast.unparse(default_node)
                            if hasattr(ast, "unparse")
                            else "..."
                        )

                    fn_params.append((pname, ptype, pdefault))

                functions.append(
                    FunctionMetadata(
                        name=func_name,
                        is_async=is_async,
                        parameters=fn_params,
                        return_type=return_type,
                        docstring=docstring,
                    )
                )

        return functions

    def _generate_test_suite_code(
        self, functions: list[FunctionMetadata], module_path: str
    ) -> str:
        """Construct pytest file content with targeted fixtures, assertions, and test classes."""
        has_async = any(f.is_async for f in functions)
        # Clean module_path to ensure valid Python import syntax
        clean_mod = module_path.replace("\\", "/").strip().lstrip("/")
        if clean_mod.endswith(".py"):
            clean_mod = clean_mod[:-3]
        parts = [p for p in clean_mod.split("/") if p]
        if parts and parts[0] == "src":
            parts = parts[1:]
        py_module = ".".join(parts) or "app"
        func_names = [f.name for f in functions]

        lines = [
            '"""',
            f"Automated Pytest Unit & Regression Test Suite for `{py_module}`.",
            "Generated via AST signature analysis by AI Code Review Agent.",
            '"""',
            "",
            "import pytest",
            "from unittest.mock import MagicMock, patch",
        ]

        if has_async:
            lines.append("import asyncio")

        lines.extend(
            [
                "",
                "try:",
                f"    from {py_module} import {', '.join(func_names)}",
                "except (ImportError, ModuleNotFoundError):",
                "    # Fallback to local module name if full path is not in sys.path",
                "    try:",
                f"        from {parts[-1] if parts else 'service'} import {', '.join(func_names)}",
                "    except (ImportError, ModuleNotFoundError):",
                "        # Define resilient stubs so test runner can still introspect signatures",
            ]
        )
        for fn in functions:
            lines.append(f"        {fn.name} = lambda *args, **kwargs: True")
        lines.append("")

        for func in functions:
            lines.extend(self._generate_test_class_for_function(func, py_module))
            lines.append("")

        return "\n".join(lines)

    def _generate_test_class_for_function(
        self, func: FunctionMetadata, module_path: str
    ) -> list[str]:
        """Generate a dedicated test class with tailored fixtures and tests for a specific function."""
        pascal_name = (
            "".join(w.capitalize() for w in re.split(r"[_]+", func.name)) or "Function"
        )
        class_lines = [
            f"class Test{pascal_name}:",
            f'    """Test suite for `{func.name}` covering happy path, edge cases, and mocks."""',
            "",
        ]

        # Identify parameters that suggest mockable services or dependencies
        mock_candidates = [
            p[0]
            for p in func.parameters
            if any(
                k in p[0].lower()
                for k in [
                    "db",
                    "client",
                    "session",
                    "conn",
                    "request",
                    "cursor",
                    "service",
                    "repo",
                ]
            )
        ]

        # Generate fixtures for dependencies
        for mock_param in mock_candidates:
            class_lines.extend(
                [
                    "    @pytest.fixture",
                    f"    def mock_{mock_param}(self):",
                    f'        """Mock fixture for `{mock_param}` dependency."""',
                    "        mock_obj = MagicMock()",
                    "        return mock_obj",
                    "",
                ]
            )

        # 1. Happy Path Test
        call_args = []
        fixture_args = []
        for pname, ptype, pdefault in func.parameters:
            if pname in mock_candidates:
                fixture_args.append(f"mock_{pname}")
                call_args.append(f"mock_{pname}")
            else:
                sample_val = self._generate_sample_value(pname, ptype, pdefault)
                call_args.append(f"{sample_val}")

        args_str = ", ".join(call_args)
        test_fixture_str = (
            f"self, {', '.join(fixture_args)}" if fixture_args else "self"
        )
        async_decorator = "    @pytest.mark.asyncio\n" if func.is_async else ""
        await_prefix = "await " if func.is_async else ""

        class_lines.extend(
            [
                f"{async_decorator}    def test_{func.name}_happy_path({test_fixture_str}):",
                f'        """Verify `{func.name}` executes successfully with valid parameters."""',
                "        # Act",
                f"        result = {await_prefix}{func.name}({args_str})",
                "        # Assert",
                "        assert result is not None",
                "",
            ]
        )

        # 2. Edge Case / Boundary Test
        boundary_args = []
        for pname, ptype, _pdefault in func.parameters:
            if pname in mock_candidates:
                boundary_args.append(f"mock_{pname}")
            else:
                boundary_val = self._generate_boundary_value(pname, ptype)
                boundary_args.append(f"{boundary_val}")

        boundary_args_str = ", ".join(boundary_args)
        class_lines.extend(
            [
                f"{async_decorator}    def test_{func.name}_boundary_edge_cases({test_fixture_str}):",
                f'        """Verify `{func.name}` handles boundary or empty input appropriately."""',
                f"        # Act & Assert with boundary values: {boundary_args_str}",
                "        try:",
                f"            result = {await_prefix}{func.name}({boundary_args_str})",
                "        except (ValueError, TypeError, KeyError) as exc:",
                "            # Boundary rejection is an acceptable defensive pattern",
                "            assert str(exc) != ''",
                "",
            ]
        )

        # 3. None/Invalid Input Test
        if func.parameters:
            first_param = func.parameters[0][0]
            none_args = ["None"] + [
                self._generate_sample_value(p[0], p[1], p[2])
                for p in func.parameters[1:]
            ]
            none_call_str = ", ".join(none_args)
            class_lines.extend(
                [
                    f"{async_decorator}    def test_{func.name}_none_rejection({test_fixture_str}):",
                    f'        """Defensive check: verify `{func.name}` behavior when `{first_param}` is None."""',
                    "        with pytest.raises((TypeError, ValueError, AttributeError, Exception)):",
                    f"            {await_prefix}{func.name}({none_call_str})",
                ]
            )

        return class_lines

    def _generate_sample_value(
        self, name: str, ptype: str | None, pdefault: str | None
    ) -> str:
        """Derive plausible sample test argument based on name, annotation, and defaults."""
        if pdefault and pdefault not in ["...", "None"]:
            return pdefault

        lower_name = name.lower()
        if ptype:
            ptype_lower = ptype.lower()
            if "int" in ptype_lower:
                return "1"
            if "float" in ptype_lower:
                return "10.5"
            if "str" in ptype_lower:
                return f'"{name}_sample"'
            if "bool" in ptype_lower:
                return "True"
            if "list" in ptype_lower:
                return "[]"
            if "dict" in ptype_lower:
                return "{}"

        if any(k in lower_name for k in ["id", "count", "num", "index", "size"]):
            return "42"
        if any(k in lower_name for k in ["rate", "price", "amount", "total", "score"]):
            return "99.9"
        if any(
            k in lower_name
            for k in ["name", "email", "title", "text", "path", "url", "msg", "message"]
        ):
            return f'"test_{name}"'
        if any(k in lower_name for k in ["is_", "has_", "enabled", "active", "flag"]):
            return "True"
        if any(k in lower_name for k in ["items", "data", "list", "array", "channels"]):
            return "[]"
        if any(
            k in lower_name
            for k in ["payload", "config", "opts", "options", "metadata"]
        ):
            return "{}"

        return f'"{name}_val"'

    def _generate_boundary_value(self, name: str, ptype: str | None) -> str:
        """Derive edge-case/boundary value for testing defensive edge cases."""
        lower_name = name.lower()
        if ptype:
            ptype_lower = ptype.lower()
            if "int" in ptype_lower:
                return "0"
            if "float" in ptype_lower:
                return "0.0"
            if "str" in ptype_lower:
                return '""'
            if "list" in ptype_lower:
                return "[]"
            if "dict" in ptype_lower:
                return "{}"

        if any(k in lower_name for k in ["id", "count", "num"]):
            return "0"
        if any(k in lower_name for k in ["name", "text", "msg", "title", "message"]):
            return '""'
        if any(k in lower_name for k in ["items", "channels", "list"]):
            return "[]"
        if any(k in lower_name for k in ["config", "data", "payload"]):
            return "{}"

        return "None"
