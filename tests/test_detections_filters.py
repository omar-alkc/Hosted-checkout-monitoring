from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.services.detections_service import _detections_stmt


def _compiled_sql(**kwargs) -> str:
    stmt = _detections_stmt(None, **kwargs)  # type: ignore[arg-type]
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_detections_stmt_card_id_filter_includes_ilike():
    sql = _compiled_sql(card_id="5213723450")
    assert "CardId" in sql
    assert "ILIKE" in sql
    assert "5213723450" in sql


def test_detections_stmt_card_id_empty_omits_filter():
    compiled_with = _compiled_sql(card_id="4111")
    compiled_without = _compiled_sql()
    assert "CardId" in compiled_with
    assert "CardId" not in compiled_without


def test_detections_stmt_strips_like_metachars_from_card_id():
    sql = _compiled_sql(card_id="5213%_")
    assert "5213" in sql
    assert "5213%_" not in sql
