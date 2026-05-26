from app.services.projects.batch_import import parse_csv, parse_json


def test_csv_basic():
    csv = "prompt,ref1,width,height,seed\n\"foo bar\",,1024,1024,random\n\"baz\",refs/a.png,512,512,42\n"
    b = parse_csv(csv)
    assert len(b.rows) == 2
    assert b.rows[0].prompt == "foo bar"
    assert b.rows[0].seed == "random"
    assert b.rows[1].seed == 42
    assert b.rows[1].ref1 == "refs/a.png"


def test_csv_warns_on_bad_size():
    csv = "prompt,width,height\n\"x\",1023,1024\n"
    b = parse_csv(csv)
    assert any("divisible by 16" in w for w in b.rows[0]._warnings)


def test_csv_warns_on_empty_prompt():
    csv = "prompt,width,height\n,1024,1024\n"
    b = parse_csv(csv)
    assert any("empty prompt" in w for w in b.rows[0]._warnings)


def test_json_full_shape():
    js = '{"project_name":"P","defaults":{"width":768,"height":768},"items":[{"prompt":"x"},{"prompt":"y","ref1":"a.png"}]}'
    b = parse_json(js)
    assert b.project_name == "P"
    assert len(b.rows) == 2
    assert b.rows[0].width == 768
    assert b.rows[1].ref1 == "a.png"


def test_json_list_shape():
    js = '[{"prompt":"x"},{"prompt":"y"}]'
    b = parse_json(js)
    assert len(b.rows) == 2
