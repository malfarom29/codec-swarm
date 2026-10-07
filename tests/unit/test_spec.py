"""Specs in any Gherkin language, the ticket's acceptance criteria, and how the judge relates them."""

import anyio

from codec_swarm.domain import Mission
from codec_swarm.harness.spec import parse_feature, ticket_criteria
from codec_swarm.plugins.jev.judge import JevJudge, LaneUnderJudgement
from codec_swarm.plugins.jira import adf_text

SPANISH = """# language: es
Característica: Plantillas reutilizables

  Antecedentes:
    Dado la tienda "Norte"

  @AC-1 @AC-2
  Escenario: Unificar plantillas duplicadas
    Cuando corre la migración
    Entonces queda una plantilla

  Regla: Soft delete
    @AC-6
    Esquema del escenario: Borrar con <campo>
      Cuando borro
      Entonces <campo> tiene fecha
      Ejemplos:
        | campo      |
        | deleted_at |
"""


def test_spanish_scenarios_and_outlines_are_found_with_their_criteria():
    scenarios = parse_feature(SPANISH, "fixes.feature")
    assert [s.name for s in scenarios] == ["Unificar plantillas duplicadas", "Borrar con <campo>"]
    assert scenarios[0].criteria == (1, 2) and scenarios[1].criteria == (6,)
    assert scenarios[0].text.startswith("@AC-1 @AC-2") and "Entonces queda una plantilla" in scenarios[0].text


def test_english_without_a_language_line_still_parses():
    assert [s.name for s in parse_feature("Feature: F\n\n  Scenario: Refund\n    Then it is refunded\n")] == ["Refund"]


def test_criteria_come_from_their_heading_in_any_language():
    english = "Context\nSome text.\n\nAcceptance Criteria:\n- Refunds can be partial\n- The key is unique\n\nNotes\nNothing."
    spanish = "Objetivo\nAlgo.\nCriterios de aceptación\n1. Unificar plantillas\n2. Archivar facturas anuladas\nNotas técnicas\nAlgo más."
    assert ticket_criteria(english) == ["Refunds can be partial", "The key is unique"]
    assert ticket_criteria(spanish) == ["Unificar plantillas", "Archivar facturas anuladas"]
    assert ticket_criteria("A ticket with no criteria heading.") == []


def test_run_together_criteria_are_split():
    text = ("Acceptance criteria\nMerge invoice_templates into one record per template and make the fields they share nullable.Soft delete "
            "the invoices that were voided by the mergeClear the template_id on every invoice.Add logo and footer fields to the invoice.Technical notes\nSomething.")
    assert ticket_criteria(text) == [
        "Merge invoice_templates into one record per template and make the fields they share nullable.",
        "Soft delete the invoices that were voided by the merge",
        "Clear the template_id on every invoice.",
        "Add logo and footer fields to the invoice.",
    ]


def test_jira_checklists_keep_one_criterion_per_line():
    doc = {"type": "doc", "content": [
        {"type": "heading", "content": [{"type": "text", "text": "Acceptance criteria"}]},
        {"type": "taskList", "content": [
            {"type": "taskItem", "content": [{"type": "text", "text": "Merge duplicate templates"}]},
            {"type": "taskItem", "content": [{"type": "text", "text": "Soft delete voided invoices"}]},
        ]},
        {"type": "heading", "content": [{"type": "text", "text": "Technical notes"}]},
    ]}
    assert ticket_criteria(adf_text(doc)) == ["Merge duplicate templates", "Soft delete voided invoices"]


class Ask:
    """Jev for the judge: every scenario 0.97, each uncovered criterion from a table."""

    def __init__(self, criteria):
        self.criteria = criteria
        self.questions = {}

    async def __call__(self, state, questions):
        from typesafe_sdk import SystemOneResponse

        self.questions = questions
        answers = {}
        for name, q in questions.items():
            p = 0.97 if name.startswith("scenario_") or name == "done" else self.criteria.get(q.instructions.get("criterion"), 0.9)
            answers[name] = {"type": "noul", "noul": p}
        return SystemOneResponse.model_validate({"model": "jev", "usage": {"input_tokens": 1, "output_tokens": 1}, "answers": answers})


def test_the_judge_scores_ticket_criteria_no_scenario_covers(tmp_path):
    spec = tmp_path / ".swarm" / "spec"
    spec.mkdir(parents=True)
    (spec / "fixes.feature").write_text(SPANISH)
    description = "Acceptance criteria\n- Merge duplicate templates\n- Reassign the invoices\n- Add logo and footer fields\n"
    ask = Ask({"Add logo and footer fields": 0.40})
    judge = JevJudge(ask, lambda m: LaneUnderJudgement(worktree=tmp_path, base="develop", checks=()))
    verdict = anyio.run(judge.evaluate, Mission(ticket="T", repo="api", description=description), [])
    assert [s.name for s in verdict.scenarios] == ["Unificar plantillas duplicadas", "Borrar con <campo>"]
    by_index = {c.index: c for c in verdict.criteria}
    assert by_index[1].covered_by == ("Unificar plantillas duplicadas",) and by_index[2].covered_by  # tagged @AC-1 @AC-2
    assert by_index[3].covered_by == () and by_index[3].probability == 0.40  # no scenario covers it: judged on its own
    assert "criterion_3" in ask.questions and "criterion_1" not in ask.questions
    assert verdict.score == 0.40  # the weakest of scenarios and uncovered criteria
