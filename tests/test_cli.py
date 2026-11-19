from logseek.cli import main

TEXT = (
    "2026-11-19 10:00:00.000 INFO  auth     login ok user=1\n"
    "2026-11-19 10:00:01.000 ERROR payment  card declined user=2\n"
    "2026-11-19 10:00:03.000 ERROR orders   db connection refused host=db-2\n"
    "2026-11-19 11:00:00.000 INFO  payment  charge ok user=3\n"
)


def make(tmp_path):
    path = tmp_path / "t.log"
    path.write_bytes(TEXT.encode())
    return str(path)


def test_prints_matching_lines_and_summary(tmp_path, capsys):
    code = main([make(tmp_path), "--from", "10:00", "--to", "10:30", "--level", "error", "--color", "never"])
    out, err = capsys.readouterr()
    assert code == 0
    assert out.splitlines() == TEXT.splitlines()[1:3]
    assert "2 matches / 3 entries in window" in err


def test_count(tmp_path, capsys):
    assert main([make(tmp_path), "--service", "payment", "--count"]) == 0
    assert capsys.readouterr().out.strip() == "2"


def test_color_marks_errors(tmp_path, capsys):
    main([make(tmp_path), "--level", "ERROR", "--color", "always"])
    assert "\033[31m" in capsys.readouterr().out


def test_histogram(tmp_path, capsys):
    assert main([make(tmp_path), "--histogram"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 24 and out[10].startswith("10:00") and "#" in out[10]


def test_bad_time_is_a_clean_error(tmp_path, capsys):
    assert main([make(tmp_path), "--from", "2pm"]) == 2
    assert "time must look like" in capsys.readouterr().err


def test_missing_file_is_a_clean_error(tmp_path, capsys):
    assert main([str(tmp_path / "nope.log")]) == 2
    assert "logseek:" in capsys.readouterr().err
