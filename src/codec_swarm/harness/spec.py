"""The approved spec and the ticket's own acceptance criteria, in any language.

Scenarios come from Gherkin in whatever language the feature declares (`# language: es` → Escenario, Esquema del
escenario…), parsed with the official Gherkin dialects. Acceptance criteria come from the ticket's description
under a heading like "Acceptance criteria" or "Criterios de aceptación". The Specifier tags each scenario with the
criteria it covers (@AC-1); a criterion no scenario covers is judged on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

AC_TAG = re.compile(r"^@AC-(\d+)$", re.I)
LANGUAGE = re.compile(r"^\s*#\s*language\s*:\s*([\w-]+)", re.M)


class SpecScenario(BaseModel, frozen=True):
    name: str
    text: str  # the scenario as written, tags included
    tags: tuple[str, ...] = ()
    file: str = ""

    @property
    def criteria(self) -> tuple[int, ...]:
        """The ticket's acceptance criteria (1-based) this scenario says it covers."""
        return tuple(int(m.group(1)) for t in self.tags if (m := AC_TAG.match(t)))


def parse_feature(text: str, file: str = "") -> list[SpecScenario]:
    """Every scenario and scenario outline in a feature file, in any Gherkin language."""
    try:
        from gherkin.parser import Parser
        from gherkin.token_scanner import TokenScanner

        document = Parser().parse(TokenScanner(text))
    except Exception:  # not valid Gherkin: fall back to splitting on the dialect's keywords
        return _split(text, file)
    lines = text.splitlines()
    found: list[tuple[int, dict]] = []

    def walk(children: list[dict]) -> None:
        for child in children:
            if "scenario" in child:
                scenario = child["scenario"]
                first = min([t["location"]["line"] for t in scenario.get("tags", [])] + [scenario["location"]["line"]])
                found.append((first, scenario))
            elif "rule" in child:
                walk(child["rule"].get("children", []))

    walk((document.get("feature") or {}).get("children", []))
    starts = [first for first, _ in found] + [len(lines) + 1]
    out = []
    for i, (first, scenario) in enumerate(found):
        block = "\n".join(lines[first - 1 : starts[i + 1] - 1]).strip()
        out.append(SpecScenario(name=scenario["name"].strip(), text=block, tags=tuple(t["name"] for t in scenario.get("tags", [])), file=file))
    return out


def _split(text: str, file: str) -> list[SpecScenario]:
    from gherkin.dialect import Dialect

    language = (LANGUAGE.search(text) or [None, "en"])[1]
    dialect = Dialect.for_name(language) or Dialect.for_name("en")
    keywords = sorted({k.strip() for k in dialect.scenario_outline_keywords + dialect.scenario_keywords if k.strip() != "*"}, key=len, reverse=True)
    blocks = re.split(rf"(?m)^(?=[ \t]*(?:{'|'.join(map(re.escape, keywords))})[ \t]*:)", text)
    out = []
    for block in (b.strip() for b in blocks[1:]):
        if block:
            out.append(SpecScenario(name=block.splitlines()[0].split(":", 1)[1].strip(), text=block, file=file))
    return out


def spec_scenarios(files: list[Path]) -> list[SpecScenario]:
    out: list[SpecScenario] = []
    for path in sorted(files):
        out += parse_feature(path.read_text(), path.name)
    return out


# --- the ticket's acceptance criteria ---------------------------------------------------------

AC_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)?(acceptance criteria|criterios de aceptaci[oó]n|crit[eé]rios de aceita[cç][aã]o|"
    r"crit[eè]res d['’]acceptation|akzeptanzkriterien|definition of done|definici[oó]n de (?:hecho|terminado)|AC)\s*(?:\*\*)?\s*:?\s*$",
    re.I,
)
BULLET = re.compile(r"^\s*(?:[-*•–]|\d+[.)]|\[[ xX]?\])\s+")
# A short line without a final period that starts with a capital: the next section's heading ("Notas técnicas").
NEXT_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?[A-ZÁÉÍÓÚÑ][^.:;!?]{0,48}:?\s*$")
# Items that a plain-text export ran together: "…the fields they share nullable.Soft delete…voided by the mergeClear the…"
RUN_TOGETHER = re.compile(r"(?<=[a-záéíóúñ0-9.)])(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}\s)")


def ticket_criteria(description: str) -> list[str]:
    """The acceptance criteria a ticket lists under its heading, one per item; empty if it has none."""
    lines = description.splitlines()
    start = next((i for i, line in enumerate(lines) if AC_HEADING.match(line)), None)
    if start is None:
        return []
    items: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            if items:
                continue
            continue
        is_item = bool(BULLET.match(line))
        if not is_item and items and NEXT_HEADING.match(line) and len(line.strip()) < 50:
            break
        text = BULLET.sub("", line).strip()
        if text:
            items.append(text)
    split: list[str] = []
    for item in items:
        parts = RUN_TOGETHER.split(item) if len(item) > 160 else [item]
        for n, part in enumerate(p.strip() for p in parts if p.strip()):
            if n > 0 and NEXT_HEADING.match(part) and len(part) < 50:
                return split  # the next section's heading, run into the last item
            split.append(part)
    return split
