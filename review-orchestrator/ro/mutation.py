"""Mutation testing restricted to lines the candidate added (Python, AST based).

Mutants are spliced into the original bytes (ast offsets are UTF-8 byte offsets) so
the rest of the file keeps its exact formatting. Each mutant must still compile.
Surviving mutants are evidence for the reviewer, not a verdict: the reviewer decides
whether a survivor is equivalent, irrelevant, or a real test gap.
"""

import ast
import copy
import difflib
import hashlib
import re

CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
            ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
            ast.In: ast.NotIn, ast.NotIn: ast.In}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}
PRIORITY = ["delete-statement", "condition-false", "condition-true", "negate-condition",
            "swap-compare", "swap-boolop", "remove-not", "return-none", "flip-constant",
            "swap-arith"]
SIMPLE = (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Raise, ast.Return,
          ast.Delete, ast.Assert)


def added_lines(diff_bytes):
    """{path: set(new-side line numbers added)} for .py files in a unified diff."""
    out, cur, n = {}, None, 0
    for line in diff_bytes.decode(errors="replace").splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            cur = path[2:] if path.startswith("b/") else None
            if cur and not cur.endswith(".py"):
                cur = None
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            out.setdefault(cur, set()).add(n)
            n += 1
        elif line.startswith(" "):
            n += 1
    return out


class _Source:
    def __init__(self, data):
        self.data = data
        self.starts = [0]
        for i, b in enumerate(data):
            if b == 0x0A:
                self.starts.append(i + 1)

    def span(self, node):
        a = self.starts[node.lineno - 1] + node.col_offset
        b = self.starts[node.end_lineno - 1] + node.end_col_offset
        return a, b

    def text(self, node):
        a, b = self.span(node)
        return self.data[a:b].decode(errors="replace")

    def splice(self, node, replacement):
        a, b = self.span(node)
        return self.data[:a] + replacement.encode() + self.data[b:]


def _candidates(tree, lines, src):
    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or node.lineno not in lines:
            continue
        if isinstance(node, SIMPLE):
            yield "delete-statement", node, "pass"
        if isinstance(node, (ast.If, ast.While)) or (isinstance(node, ast.IfExp)):
            t = node.test
            seg = src.text(t)
            yield "negate-condition", t, f"(not ({seg}))"
            yield "condition-false", t, "False"
            if isinstance(node, ast.If):
                yield "condition-true", t, "True"
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in CMP_SWAP:
                    new = copy.deepcopy(node)
                    new.ops[i] = CMP_SWAP[type(op)]()
                    yield "swap-compare", node, f"({ast.unparse(new)})"
        if isinstance(node, ast.BoolOp):
            new = copy.deepcopy(node)
            new.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            yield "swap-boolop", node, f"({ast.unparse(new)})"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield "remove-not", node, f"({src.text(node.operand)})"
        if isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            yield "return-none", node.value, "None"
        if isinstance(node, ast.Constant):
            if node.value is True or node.value is False:
                yield "flip-constant", node, str(not node.value)
            elif type(node.value) is int:
                yield "flip-constant", node, str(node.value + 1)
        if isinstance(node, ast.BinOp) and type(node.op) in BIN_SWAP:
            new = copy.deepcopy(node)
            new.op = BIN_SWAP[type(node.op)]()
            yield "swap-arith", node, f"({ast.unparse(new)})"


def generate(files, limit=30):
    """files: {path: (fixed_bytes, added_line_set)} -> list of mutant dicts."""
    out, seen = [], set()
    for path, (data, lines) in sorted(files.items()):
        try:
            tree = ast.parse(data)
        except SyntaxError:
            continue
        src = _Source(data)
        for op, node, repl in _candidates(tree, lines, src):
            new = src.splice(node, repl)
            digest = hashlib.sha256(path.encode() + new).hexdigest()
            if new == data or digest in seen:
                continue
            try:
                compile(new, path, "exec")
            except SyntaxError:
                continue
            seen.add(digest)
            diff = "".join(difflib.unified_diff(
                data.decode(errors="replace").splitlines(True), new.decode(errors="replace").splitlines(True),
                "a/" + path, "b/" + path))
            out.append(dict(file=path, line=node.lineno, operator=op,
                            original=src.text(node)[:200], replacement=repl[:200],
                            source=new, diff=diff))
    out.sort(key=lambda m: (PRIORITY.index(m["operator"]), m["file"], m["line"]))
    # Spread the budget across operators before taking a second mutant of one kind.
    picked, by_op = [], {}
    for m in out:
        by_op.setdefault(m["operator"], []).append(m)
    while len(picked) < limit and any(by_op.values()):
        for op in PRIORITY:
            if by_op.get(op) and len(picked) < limit:
                picked.append(by_op[op].pop(0))
    for i, m in enumerate(picked, 1):
        m["id"] = f"m{i:02d}"
    return picked, len(out)
