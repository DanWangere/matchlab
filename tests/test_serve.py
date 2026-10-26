"""The hub: open for readers, optionally keyed for writers, with a health endpoint for the platform."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from matchlab.serve import HUB, Handler
from matchlab.sinks import HttpSink


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_health_and_open_posts(server):
    with urllib.request.urlopen(server + "/healthz", timeout=5) as r:
        body = json.loads(r.read())
    assert body["ok"] is True and "viewers" in body
    HttpSink(server + "/cues").send([{"cue_id": "x", "layer": "tag"}])
    assert any(c["cue_id"] == "x" for c in HUB.cues)


def test_write_key_guards_posts(server, monkeypatch):
    monkeypatch.setenv("MATCHLAB_HUB_KEY", "s3cret")
    req = urllib.request.Request(server + "/cues", data=b"[]", headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 401
    HttpSink(server + "/cues").send([])  # the sink reads the key from the environment
    with urllib.request.urlopen(server + "/state", timeout=5) as r:  # readers need no key
        assert r.status == 200
