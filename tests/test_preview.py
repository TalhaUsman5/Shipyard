"""preview.py: auto-launching a completed project's own server, and
engine.py's wiring of it into a genuinely completed run.

No test here ever actually spawns npm/Node — start_preview's own
subprocess.Popen call is monkeypatched out in the engine-wiring tests, and
the pure-logic tests below rely on a project that legitimately has no
"start" script (every fixture-made project in this suite — see
conftest.py's make_repo and engine.py's scaffolded package.json, which
only ever gets a "test" script), so start_preview's own no-op path is
exercised for real.
"""
import json
import os

import engine
import preview


def test_start_preview_is_a_noop_for_a_project_with_no_package_json(isolated_dirs, make_repo):
    project_root = make_repo("proj_preview_no_pkg")

    result = preview.start_preview(project_root, "proj_preview_no_pkg")

    assert result == {"status": "unavailable", "reason": "no start script in package.json"}
    assert "proj_preview_no_pkg" not in preview._running


def test_start_preview_is_a_noop_for_a_project_with_no_start_script(isolated_dirs, make_repo):
    project_root = make_repo("proj_preview_no_start")
    with open(os.path.join(project_root, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "proj_preview_no_start", "scripts": {"test": "node --test"}}, f)

    result = preview.start_preview(project_root, "proj_preview_no_start")

    assert result["status"] == "unavailable"


def test_stop_preview_is_safe_when_nothing_is_running():
    preview.stop_preview("some-project-with-no-running-preview")


def test_a_completed_run_triggers_a_preview_for_a_project_with_a_start_script(
    fake_agents, calls, make_repo, monkeypatch
):
    project_root = make_repo("proj_preview_completed")
    with open(os.path.join(project_root, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "proj_preview_completed", "scripts": {"start": "node server.js"}}, f)

    captured = {}

    def fake_start_preview(root, name, on_update=None):
        captured["project_root"] = root
        captured["project_name"] = name
        info = {"status": "starting", "port": 54321, "url": "http://127.0.0.1:54321/"}
        if on_update:
            on_update(info)
        return info

    monkeypatch.setattr(engine.preview, "start_preview", fake_start_preview)

    session = engine.run_pipeline("add greet helper", "proj_preview_completed")

    assert session.data["status"] == "completed"
    assert captured["project_name"] == "proj_preview_completed"
    assert os.path.normcase(captured["project_root"]) == os.path.normcase(project_root)
    assert session.data["preview"] == {"status": "starting", "port": 54321, "url": "http://127.0.0.1:54321/"}


def test_a_completed_run_against_a_project_with_no_start_script_records_unavailable(
    fake_agents, calls, make_repo
):
    make_repo("proj_preview_none")

    session = engine.run_pipeline("add greet helper", "proj_preview_none")

    assert session.data["status"] == "completed"
    assert session.data["preview"]["status"] == "unavailable"


def test_preview_update_callback_persists_onto_the_session(fake_agents, calls, make_repo, monkeypatch):
    """Regression test: engine.py must treat on_update as the sole source
    of truth for session.data["preview"], never separately applying
    start_preview's return value too — that would race a fast on_update
    (e.g. a "live" update arriving before the caller finishes handling the
    earlier "starting" return) and could clobber a newer status with a
    stale one. This fake calls on_update for both the initial and the
    later status, exactly like the real preview.start_preview, with the
    later one genuinely arriving from another thread."""
    project_root = make_repo("proj_preview_update")
    with open(os.path.join(project_root, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "proj_preview_update", "scripts": {"start": "node server.js"}}, f)

    import threading

    fired = threading.Event()

    def fake_start_preview(root, name, on_update=None):
        starting = {"status": "starting", "port": 54321, "url": "http://127.0.0.1:54321/"}
        if on_update:
            on_update(starting)

        def _later():
            on_update({"status": "live", "port": 54321, "url": "http://127.0.0.1:54321/"})
            fired.set()

        threading.Thread(target=_later, daemon=True).start()
        return starting

    monkeypatch.setattr(engine.preview, "start_preview", fake_start_preview)

    session = engine.run_pipeline("add greet helper", "proj_preview_update")

    assert fired.wait(timeout=5), "on_update never fired"
    assert session.data["preview"]["status"] == "live"
    preview_events = [e for e in session.data["events"] if e["type"] == "PREVIEW_UPDATED"]
    assert len(preview_events) == 2
    assert preview_events[-1]["data"]["status"] == "live"
