"""Tolerant GDB/MI parser that preserves partial data on malformed output."""

from __future__ import annotations

import ast
import re
from typing import Any


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.i = 0
        self.errors: list[str] = []

    def parse_results(self, closing: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.i < len(self.source) and self.source[self.i] != closing:
            if self.source[self.i] == ",":
                self.i += 1
                continue
            start = self.i
            while self.i < len(self.source) and self.source[self.i] not in "=,{}[]":
                self.i += 1
            if self.i >= len(self.source) or self.source[self.i] != "=":
                self.errors.append(f"expected '=' after result name at offset {start}")
                self.i = len(self.source)
                break
            key = self.source[start:self.i]
            self.i += 1
            result[key] = self.parse_value()
        return result

    def parse_value(self) -> Any:
        if self.i >= len(self.source):
            self.errors.append("missing value")
            return None
        char = self.source[self.i]
        if char == '"':
            return self.parse_string()
        if char == "{":
            self.i += 1
            value = self.parse_results("}")
            if self.i < len(self.source) and self.source[self.i] == "}":
                self.i += 1
            else:
                self.errors.append("unterminated tuple")
            return value
        if char == "[":
            self.i += 1
            values: list[Any] = []
            while self.i < len(self.source) and self.source[self.i] != "]":
                if self.source[self.i] == ",":
                    self.i += 1
                    continue
                start = self.i
                while self.i < len(self.source) and self.source[self.i] not in "=,{}[]":
                    self.i += 1
                is_result = self.i < len(self.source) and self.source[self.i] == "="
                self.i = start
                before = self.i
                if is_result:
                    key_start = self.i
                    while self.i < len(self.source) and self.source[self.i] != "=":
                        self.i += 1
                    key = self.source[key_start:self.i]
                    if self.i < len(self.source):
                        self.i += 1
                    values.append({key: self.parse_value()})
                else:
                    values.append(self.parse_value())
                if self.i == before:
                    self.errors.append(f"parser made no progress at offset {self.i}")
                    break
                if self.i < len(self.source) and self.source[self.i] == ",":
                    self.i += 1
            if self.i < len(self.source) and self.source[self.i] == "]":
                self.i += 1
            else:
                self.errors.append("unterminated list")
            return values
        start = self.i
        while self.i < len(self.source) and self.source[self.i] not in ",}]":
            self.i += 1
        if self.i == start:
            self.errors.append(f"empty value at offset {self.i}")
            return None
        return self.source[start:self.i]

    def parse_string(self) -> str:
        start = self.i
        self.i += 1
        escaped = False
        while self.i < len(self.source):
            char = self.source[self.i]
            self.i += 1
            if char == '"' and not escaped:
                literal = self.source[start:self.i]
                if re.search(r"\\(?![\\\"'abfnrtv0-7xuU])", literal):
                    self.errors.append("malformed escape in quoted string")
                    return literal[1:-1]
                try:
                    value = ast.literal_eval(literal)
                    return value if isinstance(value, str) else str(value)
                except (SyntaxError, ValueError):
                    self.errors.append("malformed quoted string")
                    return literal[1:-1]
            if char == "\\" and not escaped:
                escaped = True
            else:
                escaped = False
        self.errors.append("unterminated MI string")
        return self.source[start + 1:self.i]


def _add_status(result: dict[str, Any], parser: _Parser, original: str) -> dict[str, Any]:
    if parser.errors:
        result["raw"] = original
        result["parse_error"] = "; ".join(parser.errors)
    return result


def parse_mi_line(line: str) -> dict[str, Any] | None:
    """Parse one MI line. Never raises; incomplete lines include partial data and raw text."""
    if not line or line == "(gdb)":
        return None
    try:
        prefix = line[0]
        if prefix in "~@&":
            parser = _Parser(line[1:])
            text = parser.parse_value() if parser.source.startswith('"') else parser.source
            result = {"record": "stream", "stream": {"~": "console", "@": "target", "&": "log"}[prefix],
                      "text": text}
            if parser.i < len(parser.source):
                parser.errors.append(f"unparsed stream data at offset {parser.i}")
            return _add_status(result, parser, line)
        if prefix not in "^*+=" and not prefix.isdigit():
            return {"record": "raw", "text": line, "raw": line, "parse_error": "unrecognized MI record"}
        match = re.match(r"^(\d*)([\^*+=])([^,]*)(.*)$", line)
        if not match:
            return {"record": "raw", "text": line, "raw": line, "parse_error": "unrecognized MI record"}
        token, marker, record_class, rest = match.groups()
        parser = _Parser(rest[1:] if rest.startswith(",") else "")
        results = parser.parse_results()
        if parser.i < len(parser.source):
            parser.errors.append(f"unparsed data at offset {parser.i}")
        result = {"record": {"^": "result", "*": "exec", "+": "status", "=": "notify"}[marker],
                  "token": int(token) if token else None, "class": record_class, "results": results}
        return _add_status(result, parser, line)
    except Exception as exc:  # Malformed debugger output must never crash a tool call.
        return {"record": "raw", "text": line, "raw": line,
                "parse_error": f"parser failure: {type(exc).__name__}: {exc}"}


def parse_mi_output(lines: list[str]) -> list[dict[str, Any]]:
    """Parse a collection of MI lines defensively, retaining every non-prompt line."""
    parsed_records: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = parse_mi_line(line)
        except Exception as exc:  # Defensive boundary for unexpected parser bugs.
            parsed = {"record": "raw", "text": line, "raw": line,
                      "parse_error": f"parser failure: {type(exc).__name__}: {exc}"}
        if parsed is not None:
            parsed_records.append(parsed)
    return parsed_records
