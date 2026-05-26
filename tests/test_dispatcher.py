from app.services.comfyui.dispatcher import Dispatcher


def test_configure_adds_and_removes():
    d = Dispatcher()
    d.configure(["http://a", "http://b"])
    urls = {w["url"] for w in d.summary()}
    assert urls == {"http://a", "http://b"}
    d.configure(["http://b", "http://c"])
    urls = {w["url"] for w in d.summary()}
    assert urls == {"http://b", "http://c"}


def test_select_skips_unhealthy():
    d = Dispatcher()
    d.configure(["http://a", "http://b"])
    for w in d._workers.values():  # type: ignore[attr-defined]
        w.healthy = w.url == "http://b"
    chosen = d.select_worker()
    assert chosen is not None
    assert chosen.url == "http://b"


def test_reserve_increments_in_flight():
    d = Dispatcher()
    d.configure(["http://a"])
    d.max_parallel_per_worker = 2
    w = d.select_worker(reserve=True)
    assert w.in_flight == 1
    d.release(w.url)
    assert d._workers[w.url].in_flight == 0  # type: ignore[attr-defined]


def test_returns_none_when_all_full():
    d = Dispatcher()
    d.configure(["http://a"])
    d.max_parallel_per_worker = 1
    d.select_worker(reserve=True)
    assert d.select_worker() is None
