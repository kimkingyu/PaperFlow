"""Unit tests for journal importers across different sources and schemas."""
import json
import os
import sqlite3
import tempfile
import pytest

from paperflow.engine.journals.importers import parse_file
from paperflow.engine.journals.models import JournalError


def test_empty_file_raises_schema_changed():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as tf:
        tf.write("")
        path = tf.name
    try:
        with pytest.raises(JournalError) as exc_info:
            parse_file(source_id="showjcr", file_path=path)
        assert exc_info.value.code == "SCHEMA_CHANGED"
    finally:
        os.remove(path)


def test_html_error_page_raises_schema_changed():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as tf:
        tf.write("<!DOCTYPE html><html><head><title>404 Not Found</title></head><body><h1>404 Not Found</h1></body></html>")
        path = tf.name
    try:
        with pytest.raises(JournalError) as exc_info:
            parse_file(source_id="showjcr", file_path=path)
        assert exc_info.value.code == "SCHEMA_CHANGED"
    finally:
        os.remove(path)


def test_html_with_css_error_class_not_misidentified():
    # An HTML file that contains the word 'error' in a CSS class or span, but has real table data
    html_content = """
    <html>
      <head><style>.error-banner { color: red; }</style></head>
      <body>
        <div class="error-banner">Notice</div>
        <table>
          <tr><th>刊名</th><th>周期</th><th>经验外链</th></tr>
          <tr><td>Demo Journal In HTML</td><td>1个月</td><td>https://example.com/rev</td></tr>
        </table>
      </body>
    </html>
    """
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".html", encoding="utf-8") as tf:
        tf.write(html_content)
        path = tf.name
    try:
        batch = parse_file(source_id="fault_experiences", file_path=path)
        assert len(batch.records) == 1
        assert batch.records[0].title == "Demo Journal In HTML"
    finally:
        os.remove(path)


def test_missing_critical_headers_raises_schema_changed():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as tf:
        tf.write("Col1,Col2,Col3\nVal1,Val2,Val3\n")
        path = tf.name
    try:
        with pytest.raises(JournalError) as exc_info:
            parse_file(source_id="showjcr", file_path=path)
        assert exc_info.value.code == "SCHEMA_CHANGED"
    finally:
        os.remove(path)


def test_all_rejected_records_raises_schema_changed():
    # CSV has title column, but every row has empty title -> 100% rejected -> raises SCHEMA_CHANGED
    content = "刊名,ISSN,大类名称,大类分区\n,1234-5678,工程技术,1区\n,2345-6789,医学,2区\n"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="FQBJCR2025.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        with pytest.raises(JournalError) as exc_info:
            parse_file(source_id="showjcr", file_path=path)
        assert exc_info.value.code == "SCHEMA_CHANGED"
    finally:
        os.remove(path)


def test_showjcr_fqbjcr_combined_issn_and_multi_minors():
    content = (
        "刊名,ISSN/EISSN,Web of Science,Open Access,大类名称,大类分区,Top,小类1名称,小类1分区,小类2名称,小类2分区\n"
        "Demo Journal of Science,1234-5678/2345-6789,SCIE; ESCI,Yes,工程技术,1区,Top,自动化与控制系统,1区,计算机：人工智能,2区\n"
        "Another Medical Review,0000-1111,,No,医学,2区,否,肿瘤学,2区,,\n"
        ",9999-9999,,No,医学,2区,否,肿瘤学,2区,,\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="FQBJCR2025.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        batch = parse_file(source_id="showjcr", file_path=path)
        assert len(batch.records) == 2
        assert batch.rejected_count == 1
        assert batch.coverage["complete"] is False

        # Check safety: rejected list does not echo raw values or secrets
        rej = batch.rejected[0]
        assert "raw" not in rej
        assert rej["error_code"] == "FIELD_EMPTY"
        assert rej["field"] == "title"

        r1 = batch.records[0]
        assert r1.title == "Demo Journal of Science"
        assert r1.issns == ["1234-5678", "2345-6789"]
        assert r1.oa_mode == "full"
        assert "SCIE" in r1.indexing and "ESCI" in r1.indexing
        assert r1.provenance.authority == "community"
        assert r1.provenance.data_year == 2025

        maj_rank = [rk for rk in r1.rankings if rk.category_type == "major"][0]
        assert maj_rank.category == "工程技术"
        assert maj_rank.quartile == 1
        assert maj_rank.top is True

        minors = [rk for rk in r1.rankings if rk.category_type == "minor"]
        assert len(minors) == 2
        assert minors[0].category == "自动化与控制系统" and minors[0].quartile == 1
        assert minors[1].category == "计算机：人工智能" and minors[1].quartile == 2
    finally:
        os.remove(path)


def test_showjcr_jcr_multi_categories_and_comparator():
    content = (
        "刊名,ISSN,Category_1,IF Quartile(2024)_1,Category_2,IF Quartile(2024)_2,影响因子\n"
        "Advanced Computing,9999-8888,Computer Science,Q1,Information Systems,Q2,<0.1\n"
        "Bio Engineering,8888-7777,Biotechnology,Q1,,,>25.0\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="JCR2024.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        batch = parse_file(source_id="showjcr", file_path=path)
        assert len(batch.records) == 2
        assert batch.coverage["complete"] is True

        r1 = batch.records[0]
        assert len(r1.rankings) == 2
        assert r1.rankings[0].category == "Computer Science" and r1.rankings[0].quartile == 1
        assert r1.rankings[1].category == "Information Systems" and r1.rankings[1].quartile == 2
        assert r1.metrics[0].name == "impact_factor"
        assert r1.metrics[0].comparator == "lt"
        assert r1.metrics[0].value == 0.1

        r2 = batch.records[1]
        assert len(r2.rankings) == 1
        assert r2.metrics[0].comparator == "gt"
        assert r2.metrics[0].value == 25.0
    finally:
        os.remove(path)


def test_showjcr_xr2026_not_misrouted_to_warning():
    # Header contains '预警标记', but filename is XR2026.csv -> must route to XR, not warning!
    content = (
        "刊名,ISSN/EISSN,大类中文名,大类英文名,大类新锐分区,Top,大类2中文名,大类2新锐分区,小类1中文名,小类1新锐分区,预警标记\n"
        "XR Exemplar Journal,3333-4444/4444-5555,计算机科学,Computer Science,1区,Top,工程技术,2区,软件工程,1区,Under Review\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="XR2026.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        batch = parse_file(source_id="showjcr", file_path=path)
        assert batch.coverage["subsystem"] == "xr"
        assert batch.coverage["year"] == 2026
        assert len(batch.records) == 1

        r = batch.records[0]
        assert r.title == "XR Exemplar Journal"
        assert r.issns == ["3333-4444", "4444-5555"]
        assert len(r.rankings) == 3
        assert r.rankings[0].category == "计算机科学" and r.rankings[0].quartile == 1 and r.rankings[0].top is True
        assert r.rankings[1].category == "工程技术" and r.rankings[1].quartile == 2
        assert r.rankings[2].category == "软件工程" and r.rankings[2].quartile == 1

        # Only xr_review system under_review risk
        assert len(r.risks) == 1
        assert r.risks[0].system == "xr_review"
        assert r.risks[0].value == "under_review"
        assert r.risks[0].reason == "Under Review"
    finally:
        os.remove(path)


def test_showjcr_warning_list_coverage_strict():
    content = (
        "刊名,ISSN,等级,原因\n"
        "Warning Specimen Review,1111-2222,高,论文工厂;引用异常\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="GJQKYJMD2024.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        batch = parse_file(source_id="showjcr", file_path=path)
        assert len(batch.records) == 1
        assert batch.coverage["system"] == "cas_warning"
        assert batch.coverage["year"] == 2024
        assert batch.coverage["complete"] is True

        r = batch.records[0]
        assert len(r.risks) == 1
        assert r.risks[0].system == "cas_warning"
        assert r.risks[0].level == "高"
    finally:
        os.remove(path)


def test_showjcr_ccf_and_ccft_real_headers():
    # CCF2026: CCF推荐类别（国际学术刊物/会议）is kind, CCF推荐类型 is grade (A/B/C)
    ccf_content = (
        "刊名,简称,CCF推荐类别（国际学术刊物/会议）,CCF推荐类型,方向名称\n"
        "ACM Trans on Computer Systems,TOCS,国际学术刊物,A,计算机系统与高性能计算\n"
        "IEEE INFOCOM,INFOCOM,国际学术会议,A,计算机网络\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="CCF2026.csv", encoding="utf-8") as tf:
        tf.write(ccf_content)
        ccf_path = tf.name
    try:
        batch = parse_file(source_id="showjcr", file_path=ccf_path)
        assert len(batch.records) == 2
        r1, r2 = batch.records[0], batch.records[1]
        assert r1.kind == "journal"
        assert r1.rankings[0].system == "ccf"
        assert r1.rankings[0].grade == "A"
        assert r2.kind == "conference"
        assert r2.rankings[0].grade == "A"
    finally:
        os.remove(ccf_path)

    # CCFT2025: T分区 is grade (T1/T2/T3)
    ccft_content = (
        "刊名,CCF推荐类别,T分区,领域\n"
        "Chinese Journal of Electronics,科技期刊,T1,电子信息\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="CCFT2025.csv", encoding="utf-8") as tf:
        tf.write(ccft_content)
        ccft_path = tf.name
    try:
        batch_t = parse_file(source_id="showjcr", file_path=ccft_path)
        assert len(batch_t.records) == 1
        assert batch_t.records[0].rankings[0].system == "ccft"
        assert batch_t.records[0].rankings[0].grade == "T1"
        assert batch_t.coverage["complete"] is True
    finally:
        os.remove(ccft_path)


def test_showjcr_sqlite_jcr_xr_and_row_quota():
    with tempfile.NamedTemporaryFile("wb", delete=False, suffix="jcr.db") as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE JCR2025 (刊名 TEXT, ISSN TEXT, Category TEXT, Quartile TEXT, 影响因子 TEXT);")
        c.execute("INSERT INTO JCR2025 VALUES ('JCR DB Journal', '1111-2222', 'Chemistry', 'Q1', '6.8');")
        c.execute("CREATE TABLE XR2026 (刊名 TEXT, ISSN TEXT, 大类中文名 TEXT, 大类新锐分区 TEXT);")
        c.execute("INSERT INTO XR2026 VALUES ('XR DB Journal', '3333-4444', '材料科学', '1区');")
        c.execute("CREATE TABLE untrusted_table (foo TEXT);")
        conn.commit()
        conn.close()

        batch = parse_file(source_id="showjcr", file_path=db_path)
        assert len(batch.records) == 2
        tables = {d["table"] for d in batch.coverage["datasets"]}
        assert tables == {"JCR2025", "XR2026"}
        assert all(d["complete"] is True for d in batch.coverage["datasets"])

        # Verify JCR rankings & metrics parsed from DB
        jcr_rec = [r for r in batch.records if r.title == "JCR DB Journal"][0]
        assert jcr_rec.rankings[0].system == "jcr"
        assert jcr_rec.rankings[0].quartile == 1
        assert jcr_rec.metrics[0].value == 6.8

        # Verify XR rankings parsed from DB
        xr_rec = [r for r in batch.records if r.title == "XR DB Journal"][0]
        assert xr_rec.rankings[0].system == "xr"
        assert xr_rec.rankings[0].quartile == 1
    finally:
        os.remove(db_path)


def test_xr_journal_query_2d_array_and_cn_mapping():
    # Real format: const CN = {...}; const D = [[catId, title, tier, isTop], ...]; followed by extra scripts
    html_content = """
    <html>
      <head><title>XR Query</title></head>
      <body>
        <script>
          const CN = {"1": "工程技术", "2": "计算机科学"};
          const D = [
            [1, "Journal of Engineering Wonders", "1区", 1],
            [2, "Computing Frontiers", "2区", 0]
          ];
          const OTHER = [1, 2, 3];
        </script>
        <script>
          console.log("Subsequent script block");
        </script>
      </body>
    </html>
    """
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".html", encoding="utf-8") as tf:
        tf.write(html_content)
        path = tf.name
    try:
        batch = parse_file(source_id="xr_journal_query", file_path=path, data_year=2026)
        assert len(batch.records) == 2
        r1, r2 = batch.records[0], batch.records[1]
        assert r1.title == "Journal of Engineering Wonders"
        assert r1.issns == []  # No ISSN in 2D array
        assert r1.rankings[0].system == "xr"
        assert r1.rankings[0].category == "工程技术"
        assert r1.rankings[0].quartile == 1
        assert r1.rankings[0].top is True

        assert r2.title == "Computing Frontiers"
        assert r2.rankings[0].category == "计算机科学"
        assert r2.rankings[0].quartile == 2
        assert r2.rankings[0].top is False
    finally:
        os.remove(path)


def test_scipythonspider_cycles_and_no_first_decision_claim():
    content = (
        "刊名,ISSN,初审周期,年发文量,投稿难度,录用比例\n"
        "Spider Journal A,1234-0001,2-4周,500,较难,约20%\n"
        "Spider Journal B,1234-0002,4-9,1200,容易,\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="spider_2017.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        batch = parse_file(source_id="scipythonspider", file_path=path)
        assert len(batch.records) == 2
        r1 = batch.records[0]
        m_speed = [m for m in r1.metrics if m.name == "review_speed"][0]
        assert m_speed.lower == 14.0
        assert m_speed.upper == 28.0
        assert m_speed.unit == "days"
        assert m_speed.stage == "unknown"

        r2 = batch.records[1]
        m2_speed = [m for m in r2.metrics if m.name == "review_speed"][0]
        assert m2_speed.lower == 4.0
        assert m2_speed.upper == 9.0
        assert m2_speed.unit == ""
    finally:
        os.remove(path)


def test_row_year_conflict_with_explicit_year_raises():
    content = (
        "刊名,ISSN,年份,大类名称,大类分区\n"
        "Test Year Mismatch,1234-5678,2023,工程技术,1区\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, suffix="FQBJCR2025.csv", encoding="utf-8") as tf:
        tf.write(content)
        path = tf.name
    try:
        with pytest.raises(JournalError) as exc_info:
            parse_file(source_id="showjcr", file_path=path, data_year=2025)
        assert exc_info.value.code == "SCHEMA_CHANGED"
    finally:
        os.remove(path)


def test_rejected_does_not_leak_secrets():
    bad_json = json.dumps([
        {"title": "Valid One", "issns": ["1234-5678"]},
        {"invalid_key_secret": "SUPER_SECRET_TOKEN", "bad": True}  # Missing title
    ])
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tf:
        tf.write(bad_json)
        path = tf.name
    try:
        batch = parse_file(source_id="local", file_path=path, kind="records")
        assert len(batch.records) == 1
        assert batch.rejected_count == 1
        rej_repr = json.dumps(batch.rejected)
        assert "SUPER_SECRET_TOKEN" not in rej_repr
        assert "raw" not in batch.rejected[0]
    finally:
        os.remove(path)
