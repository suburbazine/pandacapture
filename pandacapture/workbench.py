"""The dashboard's Captures & Claude page, server side: reference logs uploaded for the session, capture
bundles, and Claude's analysis as a background job (prepare, show what would be sent and what it costs,
then run only once confirmed). See ai.py for the rules the analysis keeps: your own API key, Opus or Fable."""

import io
import json
import tempfile
import threading
from pathlib import Path


class References:
    """Reference logs (loggers' CSVs, obd CSVs) uploaded from the page, kept until the dashboard stops."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pandacapture-refs-"))
        self.files = {}          # id -> (path, original name)
        self.lock = threading.Lock()

    def add(self, data: bytes, filename: str) -> str:
        with self.lock:
            rid = f"r{len(self.files) + 1}"
            name = Path(filename or "reference.csv").name
            path = self.dir / f"{rid}-{name}"
            path.write_bytes(data)
            self.files[rid] = (path, name)
            return rid

    def get(self, rid):
        """(path, name), or (None, None) for none / the capture's own OBD answers."""
        if not rid or rid == "obd":
            return None, None
        with self.lock:
            if rid not in self.files:
                raise ValueError("That reference log isn't loaded any more: choose it again.")
            return self.files[rid]

    def listing(self):
        with self.lock:
            return [{"id": rid, "name": name} for rid, (_, name) in self.files.items()]

    def close(self):
        for path, _ in self.files.values():
            path.unlink(missing_ok=True)
        try:
            self.dir.rmdir()
        except OSError:
            pass


def bundle_bytes(capture, address_map, reference=None, ref_name=None) -> bytes:
    from .bundle import write_bundle
    buf = io.BytesIO()
    write_bundle(capture, address_map, buf, reference, ref_name=ref_name)
    return buf.getvalue()


def key_status() -> dict:
    """Where the API key would come from, and whether it's usable. Never the key itself."""
    import os
    from .ai import key_problem, stored_key
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    env = bool(key)
    if not env:
        key = (stored_key() or "").strip()
    problem = key_problem(key) if key else ""
    return {"env": env, "store": bool(key) and not env, "ready": bool(key) and not problem, "problem": problem or ""}


class ClaudeJob:
    """One analysis at a time: idle -> preparing -> confirm -> running -> done / error / canceled."""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.lock = threading.Lock()
        self.state = {"state": "idle"}
        self._session = None
        self._cancel = threading.Event()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _log(self, text):
        with self.lock:
            self.state.setdefault("log", []).append(text)

    def prepare(self, capture, reference, ref_name, address_map, model_name, effort, max_cost, client_factory=None):
        from .ai import MODELS, PRICES, AnalyzeError, Session, api_key
        from .analysis import Analysis
        if model_name not in MODELS:
            raise ValueError("Choose Opus or Fable.")
        with self.lock:
            if self.state.get("state") in ("preparing", "running"):
                raise ValueError("An analysis is already under way.")
            self.state = {"state": "preparing", "capture": Path(capture).name, "reference": ref_name or "OBD answers",
                          "model": MODELS[model_name], "log": []}
            self._cancel.clear()

        def work():
            try:
                key, source = api_key()
                if client_factory is None:
                    import anthropic
                    client = anthropic.Anthropic(api_key=key)   # always the explicit key
                else:
                    client = client_factory(key)
                analysis = Analysis(capture, address_map, reference, log=self._log)
                session = Session(client, MODELS[model_name], analysis, log=self._log, max_cost=max_cost,
                                  effort=effort, canceled=self._cancel.is_set)
                params = {k: v for k, v in session.request_params(session.first_message()).items()
                          if k not in ("max_tokens", "cache_control")}
                tokens = client.messages.count_tokens(**params).input_tokens
                self._session = session
                self._set(state="confirm", key_source=source, ids=len(analysis.stats),
                          withheld=[f"0x{i:03X}" for i in analysis.withheld], references=list(analysis.columns),
                          markers=len(analysis.events), duration=round(analysis.duration, 1),
                          first_tokens=tokens, first_cost=round(tokens * PRICES[session.model][0] / 1e6, 4),
                          max_cost=max_cost, effort=effort)
            except AnalyzeError as e:
                self._set(state="error", error=str(e))
            except Exception as e:  # noqa: BLE001 - shown on the page
                self._set(state="error", error=_api_error(e))

        threading.Thread(target=work, daemon=True, name="pandacapture-claude").start()

    def start(self):
        with self.lock:
            if self.state.get("state") != "confirm" or self._session is None:
                raise ValueError("Prepare an analysis first.")
            self.state["state"] = "running"
            session = self._session

        def work():
            try:
                session.run()
                self._finish(session, "canceled" if session.stopped == "Canceled." else "done")
            except Exception as e:  # noqa: BLE001 - shown on the page
                self._finish(session, "error", _api_error(e))

        threading.Thread(target=work, daemon=True, name="pandacapture-claude").start()

    def _finish(self, session, state, error=""):
        from .ai import report, write_results
        saved = {"saved": "", "built_map": ""}
        if session.proposals or session.summary:
            saved = write_results(session, self.out_dir, {"capture": self.state.get("capture"),
                                                          "reference": self.state.get("reference"),
                                                          "map": session.analysis.map.name})
        built = None
        if saved["built_map"]:
            b = session.built_map
            base = {s.key for s in session.analysis.map.signals}
            built = {"name": b["name"], "signals": len(b["signals"]),
                     "added": [s["key"] for s in b["signals"] if s.get("key") not in base], "path": saved["built_map"]}
        self._set(state=state, error=error, proposals=session.proposals, rejected=len(session.rejected),
                  summary=session.summary, stopped=session.stopped, cost=round(session.cost(), 4),
                  usage=session.usage, saved=saved["saved"], built_map=built, report=report(session))
        self._session = None

    def use_built_map(self, maps_dir) -> Path:
        """Copies the map Claude built into your maps folder (never over another map) and returns its path."""
        from .ai import map_file_name
        from .signals import builtin_maps, load_map
        with self.lock:
            built = (self.state.get("built_map") or {}).get("path", "")
        if not built or not Path(built).exists():
            raise ValueError("This analysis didn't build a map.")
        load_map(built)                                   # still passes the map rules
        maps_dir = Path(maps_dir)
        maps_dir.mkdir(parents=True, exist_ok=True)
        name = json.loads(Path(built).read_text(encoding="utf-8"))["name"]
        stem = map_file_name(name)[:-5]
        dest, n = maps_dir / f"{stem}.json", 2
        while dest.exists() or dest.stem in builtin_maps():     # never shadows a map you or PandaCapture have
            dest, n = maps_dir / f"{stem}-{n}.json", n + 1
        dest.write_bytes(Path(built).read_bytes())
        return dest

    def cancel(self):
        with self.lock:
            st = self.state.get("state")
            if st == "confirm":
                self.state = {"state": "idle"}
                self._session = None
                return
        self._cancel.set()


def _api_error(e) -> str:
    try:
        import anthropic
    except ImportError:
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "The API key was refused. Save a new one with: pandacapture apikey set"
    if isinstance(e, anthropic.PermissionDeniedError):
        return f"The key isn't allowed to use this model: {getattr(e, 'message', e)}"
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the API. Try again in a minute."
    if isinstance(e, anthropic.APIStatusError):
        return f"The API returned {e.status_code}: {getattr(e, 'message', e)}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the API. Check the internet connection."
    return str(e)
