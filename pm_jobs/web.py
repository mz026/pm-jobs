"""The page you actually read.

Server-rendered HTML and form posts — no JavaScript, no build step. Marking a
job read happens by clicking through to it: the title links to /open, which
records the read and redirects to the board. That way the state matches what
you actually did rather than what you remembered to click.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, url_for

from .reviewstore import VIEWS, ReviewStore
from .store import DEFAULT_DB_PATH, connect


def create_app(db_path: Path | str = DEFAULT_DB_PATH) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)

    def store() -> ReviewStore:
        # A fresh connection per request: SQLite connections are not safe to
        # share across threads, and Flask's dev server is threaded.
        return ReviewStore(connect(app.config["DB_PATH"]))

    def back_to(default: str = "unread") -> str:
        view = request.form.get("from", default)
        return url_for("view", view=view if view in VIEWS else default)

    @app.route("/")
    def index():
        return redirect(url_for("view", view="unread"))

    @app.route("/<view>")
    def view(view: str):
        if view not in VIEWS:
            abort(404)
        s = store()
        try:
            return render_template("list.html", view=view, jobs=s.list_jobs(view),
                                   counts=s.counts())
        finally:
            s.conn.close()

    @app.route("/job/<board>/<board_job_id>/open")
    def open_job(board: str, board_job_id: str):
        s = store()
        try:
            target = s.conn.execute(
                """SELECT job_url FROM raw_jobs
                   WHERE board = ? AND board_job_id = ? ORDER BY id DESC LIMIT 1""",
                (board, board_job_id),
            ).fetchone()
            if target is None or not target["job_url"]:
                abort(404)
            s.mark_read(board, board_job_id)
            return redirect(target["job_url"], code=302)
        finally:
            s.conn.close()

    @app.route("/job/<board>/<board_job_id>/favorite", methods=["POST"])
    def favorite(board: str, board_job_id: str):
        s = store()
        try:
            s.toggle_favorite(board, board_job_id)
        finally:
            s.conn.close()
        return redirect(back_to())

    @app.route("/job/<board>/<board_job_id>/unread", methods=["POST"])
    def unread(board: str, board_job_id: str):
        s = store()
        try:
            s.mark_read(board, board_job_id, read=False)
        finally:
            s.conn.close()
        return redirect(back_to("read"))

    return app
