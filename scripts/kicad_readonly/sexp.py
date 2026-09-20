"""Minimal KiCad S-expression parser (read-only, no external deps)."""

from __future__ import annotations

import re
from typing import Any, List, Union

Token = Union[str, float, int]
Sexp = Union[Token, List[Any]]


def _tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == ";":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "(":
            tokens.append("(")
            i += 1
            continue
        if c == ")":
            tokens.append(")")
            i += 1
            continue
        if c == '"':
            i += 1
            start = i
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            tokens.append(text[start:i])
            i += 1
            continue
        m = re.match(r"[^\s()\";]+", text[i:])
        if not m:
            raise ValueError(f"Unexpected character at {i}: {repr(text[i:i+20])}")
        raw = m.group(0)
        if re.fullmatch(r"-?\d+\.?\d*", raw):
            tokens.append(float(raw) if "." in raw else int(raw))
        else:
            tokens.append(raw)
        i += len(raw)
    return tokens


def parse(text: str) -> Sexp:
    tokens = _tokenize(text)
    pos = 0

    def read() -> Sexp:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("Unexpected end of input")
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            node: List[Any] = []
            while pos < len(tokens) and tokens[pos] != ")":
                node.append(read())
            if pos >= len(tokens):
                raise ValueError("Unclosed list")
            pos += 1
            return node
        pos += 1
        return tok

    result = read()
    return result
