from __future__ import annotations

from flask import Flask, Response, send_from_directory

from .db import init_schema


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    init_schema()

    from .api import calendars, facts, query, reference, scenarios

    app.register_blueprint(reference.bp)
    app.register_blueprint(calendars.bp)
    app.register_blueprint(facts.bp)
    app.register_blueprint(scenarios.bp)
    app.register_blueprint(query.bp)

    @app.get("/")
    def index():
        return send_from_directory(app.template_folder, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
               '<rect width="16" height="16" rx="3" fill="#4f8ef7"/>'
               '<text x="8" y="12" font-size="10" font-family="sans-serif" '
               'fill="#fff" text-anchor="middle">d</text></svg>')
        return Response(svg, mimetype="image/svg+xml")

    return app
