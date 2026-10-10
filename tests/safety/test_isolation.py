"""Nothing outside the paper-only lock can point the platform at a live account.

A scan of every module under src/hftlab, in the spirit of a broker-isolation test. It fails when a module:

  * names a live gateway port, as a number, inside text, or as simple arithmetic on constants;
  * holds a "live" trading-mode literal or a name that is exactly `live`;
  * touches the process environment in any way but reading a constant key that cannot enable live trading;
  * imports a network library or the broker's client library outside hftlab.venues.ibkr, by any static or
    dynamic route (one module of the host layer may import five names of the standard library's HTTP client:
    see `POST` below);
  * imports by a computed name, or runs code from text, which no scan can follow;
  * rebinds or edits the venue registry.

The lock module is the one exemption for the first and third rules; it is covered by tests/safety/test_lock.py.

Limits, stated so nobody mistakes this for proof: a port built from a variable, a key assembled at run time
through a helper in another module, or a network call made by shelling out are not visible to a syntax scan.
The run-time lock and code review are the other two layers (ADR 0001).
"""
from __future__ import annotations

import ast
import operator
import re

import pytest

from hftlab.safety import VENUES, UnknownVenueError, Venue, venue
from tests.support import SRC, called_name, imports, runs_text, sources

LOCK = "src/hftlab/safety/lock.py"
REGISTRY = "src/hftlab/safety/venues.py"
# Only the broker adapter opens sockets. The venue port, its guard, the fake broker, the recorder, the replay
# and the shadow venue are plain computation: the recorder gets its quotes through the port and needs no
# network library of its own.
NETWORK_OK = ("src/hftlab/venues/ibkr/",)
BROKER_OK = ("src/hftlab/venues/ibkr/",)
BROKER_LIBRARIES = frozenset({"ib_async", "ib_insync", "ibapi", "ibind"})
# One module outside the adapter may open a connection: the host layer's HTTPS POST, which pages the owner and
# sends the status snapshot (ADR 0017). It may import these five names of the standard library's HTTP client,
# with `from`, and nothing else that opens a socket: no socket module, no event loop, never the broker's
# library. It has no other exemption, and it is one file, not a package.
POST = "src/hftlab/ops/post.py"
POST_MAY_IMPORT = frozenset({
    "urllib.request.Request", "urllib.request.build_opener", "urllib.request.HTTPRedirectHandler",
    "urllib.error.HTTPError", "urllib.error.URLError",
})  # fmt: skip

LIVE_PORTS = frozenset({4001, 7496})
# Not after a digit or a decimal point: 1.4001 and 0.7496 are prices, and prices appear in this code base.
PORT_IN_TEXT = re.compile(r"(?<![0-9.])(?:4001|7496)(?![0-9])")
ENV_NAMES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})
ENV_IN_TEXT = re.compile(r"\b(?:environb?|getenvb?|putenv|unsetenv)\b")
LIVE_KEY = re.compile(r"(?i)(?:^|_)(?:mode|live)(?:_|$)")
FOLDABLE = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.FloorDiv: operator.floordiv,
            ast.LShift: operator.lshift, ast.BitOr: operator.or_, ast.BitXor: operator.xor}  # fmt: skip

# Standard-library and third-party modules that open sockets. A dotted entry covers that module and below.
NETWORK = frozenset({
    "socket", "socketserver", "ssl", "select", "selectors", "asyncio", "http", "ftplib", "smtplib", "poplib",
    "imaplib", "nntplib", "telnetlib", "xmlrpc", "urllib.request", "urllib.error", "urllib.response",
    "urllib.robotparser", "multiprocessing.connection", "multiprocessing.managers",
    "urllib3", "requests", "httpx", "httpcore", "aiohttp", "websockets", "websocket", "grpc", "zmq", "paramiko",
    "pycurl", "tornado", "twisted", "trio", "anyio", "boto3", "botocore", "google", "redis", "kafka", "nats", "pika",
})  # fmt: skip
# Packages with both harmless and network submodules: `import pkg` binds the package and so reaches every
# submodule already loaded, which a scan cannot see. Their harmless parts must be imported with `from`.
MIXED_PACKAGES = frozenset({"urllib", "multiprocessing"})
READ_METHODS = frozenset({"get", "keys", "values", "items", "__contains__", "__getitem__"})


def is_network(name: str) -> bool:
    parts = name.split(".")
    return any(".".join(parts[:n]) in NETWORK for n in range(1, len(parts) + 1))


def folded(node: ast.AST) -> int | None:
    """The integer a constant expression works out to, for `4000 + 1` and the like."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = folded(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.BinOp) and type(node.op) in FOLDABLE:
        left, right = folded(node.left), folded(node.right)
        if left is not None and right is not None and not (isinstance(node.op, ast.FloorDiv) and right == 0):
            return int(FOLDABLE[type(node.op)](left, right))
    if isinstance(node, ast.Call) and called_name(node) == "int" and len(node.args) == 1:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.strip().isdigit():
            return int(arg.value)
    return None


def _env_key(node: ast.AST) -> ast.expr | None:
    """The key expression when `node` is one of the three permitted shapes of an environment read."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        holder = node.value
        return node.slice if isinstance(holder, ast.Attribute) and holder.attr == "environ" else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        target = node.func
        if target.attr == "get" and isinstance(target.value, ast.Attribute) and target.value.attr == "environ":
            return node.args[0]
        if target.attr == "getenv":
            return node.args[0]
    return None


def port_violations(rel: str, tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, int | float) and not isinstance(value, bool) and value in LIVE_PORTS:
                out.append(f"{rel}: names live port {int(value)}")
            elif isinstance(value, str) and PORT_IN_TEXT.search(value):
                out.append(f"{rel}: names a live port inside text")
            elif isinstance(value, bytes) and PORT_IN_TEXT.search(value.decode("latin-1")):
                out.append(f"{rel}: names a live port inside bytes")
        elif isinstance(node, ast.BinOp | ast.UnaryOp | ast.Call) and folded(node) in LIVE_PORTS:
            out.append(f"{rel}: computes live port {folded(node)}")
    return out


def mode_violations(rel: str, tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
            text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
            if text.strip().casefold() == "live":
                out.append(f"{rel}: holds a 'live' mode literal")
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.keyword):
            names = [node.arg or ""]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = [node.name]
        elif isinstance(node, ast.alias):
            names = [node.name.rsplit(".", 1)[-1], node.asname or ""]
        out += [f"{rel}: uses the name {name!r}" for name in names if name.casefold() == "live"]
    return out


def env_violations(rel: str, tree: ast.AST) -> list[str]:
    out = []
    approved: set[int] = set()
    for node in ast.walk(tree):
        key = _env_key(node)
        if key is None:
            continue
        holder = node.value if isinstance(node, ast.Subscript) else node.func  # type: ignore[attr-defined]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if LIVE_KEY.search(key.value):
                out.append(f"{rel}: reads the environment variable {key.value!r}, which belongs to the lock")
            approved.add(id(holder))
            approved.add(id(getattr(holder, "value", None)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ENV_NAMES and id(node) not in approved:
            out.append(f"{rel}: touches the environment through .{node.attr} in a way the scan cannot clear")
        elif isinstance(node, ast.Name) and node.id in ENV_NAMES:
            out.append(f"{rel}: uses the bare name {node.id}")
        elif isinstance(node, ast.alias) and (node.name in ENV_NAMES or node.asname in ENV_NAMES):
            out.append(f"{rel}: imports {node.name} by name")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and ENV_IN_TEXT.search(node.value):
            out.append(f"{rel}: names an environment accessor inside text")
    return out


def network_violations(rel: str, tree: ast.AST) -> list[str]:
    out = []
    for how, name in imports(tree, rel):
        if how == "computed":
            out.append(f"{rel}: imports a module by a computed name")
        elif name.split(".")[0] in BROKER_LIBRARIES:
            if not rel.startswith(BROKER_OK):
                out.append(f"{rel}: imports the broker library {name} ({how})")
        elif rel.startswith(NETWORK_OK) or (rel == POST and how == "from" and name in POST_MAY_IMPORT):
            continue
        elif is_network(name):
            out.append(f"{rel}: imports the network library {name} ({how})")
        elif how in ("import", "dynamic") and name.split(".")[0] in MIXED_PACKAGES:
            out.append(f"{rel}: binds the package {name.split('.')[0]}, which has network submodules; use `from`")
    out += [f"{rel}: runs code from text" for node in ast.walk(tree) if runs_text(node)]
    return out


def registry_violations(rel: str, tree: ast.AST) -> list[str]:
    def is_registry(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id == "VENUES") or (
            isinstance(node, ast.Attribute) and node.attr == "VENUES"
        )

    out = []
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        flat = [t for target in targets for t in ast.walk(target)]
        if any(is_registry(t) for t in flat):
            out.append(f"{rel}: rebinds or edits the venue registry")
        if isinstance(node, ast.Call) and called_name(node) in ("setattr", "delattr") and len(node.args) >= 2:
            name = node.args[1]
            if not isinstance(name, ast.Constant) or name.value == "VENUES":
                out.append(f"{rel}: could rebind the venue registry through {called_name(node)}")
        method = node.func if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
        if method is not None and is_registry(method.value) and method.attr not in READ_METHODS:
            out.append(f"{rel}: calls .{method.attr} on the venue registry")
    return out


def violations(rel: str, tree: ast.AST) -> list[str]:
    """Everything wrong with one module. The lock may name ports and read its variables; nothing else may."""
    out = mode_violations(rel, tree) + network_violations(rel, tree)
    if rel != LOCK:
        out += port_violations(rel, tree) + env_violations(rel, tree)
    if rel != REGISTRY:
        out += registry_violations(rel, tree)
    return out


# ---- the real tree -------------------------------------------------------------------------------------------------


def test_no_module_can_reach_a_live_account_or_the_network_outside_its_place():
    found = [v for rel, tree in sources() for v in violations(rel, tree)]
    assert found == []


def test_the_scan_reads_the_whole_package():
    scanned = {rel for rel, _ in sources()}
    assert {LOCK, REGISTRY, "src/hftlab/core/clock.py", "src/hftlab/registry/records.py"} <= scanned
    assert len(scanned) == len(list(SRC.rglob("*.py")))


def test_the_lock_is_exempt_for_a_reason():
    tree = dict(sources())[LOCK]
    assert port_violations(LOCK, tree) and env_violations(LOCK, tree), "the scan no longer sees what the lock holds"
    assert mode_violations(LOCK, tree) == [] and network_violations(LOCK, tree) == []


def test_the_network_packages_exist_as_the_anchor_for_the_rule():
    for package in ("venues", "record"):
        assert (SRC / package / "__init__.py").is_file()
    assert "src/hftlab/venues/ibkr/x.py".startswith(NETWORK_OK)
    assert not "src/hftlab/venues/x.py".startswith(NETWORK_OK) and not "src/hftlab/record/x.py".startswith(NETWORK_OK)


# ---- every way round it --------------------------------------------------------------------------------------------

SNEAKY = "src/hftlab/strategy/sneaky.py"

BYPASSES = {
    "live port as a number": "PORT = 4001",
    "the other live port": "connect(host, 7496)",
    "live port as a float": "PORT = 4001.0",
    "live port in a default argument": "def connect(port=7496): ...",
    "live port as text": "PORT = int('4001')",
    "live port inside a url": "URL = 'tcp://127.0.0.1:7496'",
    "live port inside an f-string": "URL = f'{host}:4001'",
    "live port in bytes": "PORT = b'4001'",
    "live port by addition": "PORT = 4000 + 1",
    "live port by subtraction from the paper port": "PORT = 4002 - 1",
    "live port by multiplication": "PORT = 2 * 3748",
    "live port by shifting": "PORT = 937 << 3",
    "live port by nested arithmetic": "PORT = (4000 + 2) - 1",
    "live mode literal": "MODE = 'live'",
    "live mode literal in upper case": "if mode == 'LIVE': ...",
    "live mode literal with spaces": "mode = ' Live '",
    "live mode in bytes": "mode = b'live'",
    "live mode as a keyword argument value": "start(mode='live')",
    "live as an enum member": "class Mode:\n    PAPER = 1\n    LIVE = 2",
    "live as an attribute": "if mode is Mode.LIVE: ...",
    "live as a parameter": "def start(live=False): ...",
    "live as a keyword": "start(live=True)",
    "live as a function": "def live(): ...",
    "live as an import alias": "from brokers import production as live",
    "reads MODE": "import os\nmode = os.environ['MODE']",
    "reads MODE with get": "import os\nmode = os.environ.get('MODE', 'paper')",
    "reads MODE with getenv": "import os\nmode = os.getenv('MODE')",
    "reads the live flag": "import os\nflag = os.environ.get('LIVE_TRADING_ENABLED')",
    "reads a differently named mode variable": "import os\nmode = os.environ.get('TRADING_MODE')",
    "reads a lower-case mode variable": "import os\nmode = os.getenv('hft_mode')",
    "reads a live username variable": "import os\nname = os.environ['IBKR_LIVE_USERNAME']",
    "writes MODE": "import os\nos.environ['MODE'] = 'paper'",
    "deletes the live flag": "import os\ndel os.environ['LIVE_TRADING_ENABLED']",
    "reads a computed key": "import os\nvalue = os.environ[key]",
    "reads a key built by concatenation": "import os\nvalue = os.environ['MO' + 'DE']",
    "reads a key built by an f-string": "import os\nvalue = os.getenv(f'{prefix}MODE')",
    "takes the whole environment": "import os\nenv = os.environ",
    "copies the environment": "import os\nenv = os.environ.copy()",
    "makes a dict of the environment": "import os\nenv = dict(os.environ)",
    "unpacks the environment": "import os\nenv = {**os.environ}",
    "iterates the environment": "import os\nfor k, v in os.environ.items(): ...",
    "pops from the environment": "import os\nos.environ.pop('LIVE_TRADING_ENABLED', None)",
    "sets a default in the environment": "import os\nos.environ.setdefault('MODE', 'paper')",
    "updates the environment": "import os\nos.environ.update(MODE='paper')",
    "imports environ by name": "from os import environ\nmode = environ['MODE']",
    "imports getenv by name": "from os import getenv\nmode = getenv('MODE')",
    "imports environ under another name": "from os import environ as settings\nmode = settings['MODE']",
    "aliases os": "import os as system\nmode = system.environ['MODE']",
    "reaches environ through getattr": "import os\nenv = getattr(os, 'environ')",
    "reaches environ through the module dict": "import os\nenv = os.__dict__['environ']",
    "reaches environ through vars": "import os\nenv = vars(os)['environ']",
    "reads the bytes environment": "import os\nmode = os.environb[b'MODE']",
    "uses putenv": "import os\nos.putenv('MODE', 'paper')",
    "reads the process environment file": "text = open('/proc/self/environ').read()",
    "reaches environ through sys.modules": "import sys\nmode = sys.modules['os'].environ['MODE']",
    "imports socket": "import socket",
    "imports socket under another name": "import socket as s",
    "imports from socket": "from socket import create_connection",
    "imports ssl": "import ssl",
    "imports http.client": "import http.client",
    "imports from http": "from http import client",
    "imports urllib.request": "import urllib.request",
    "imports request from urllib": "from urllib import request",
    "imports from urllib.request": "from urllib.request import urlopen",
    "binds urllib": "import urllib",
    "binds urllib through its harmless submodule": "import urllib.parse",
    "imports asyncio": "import asyncio",
    "imports from asyncio": "from asyncio import open_connection",
    "imports requests": "import requests",
    "imports httpx": "import httpx",
    "imports websockets": "from websockets import connect",
    "imports the broker api": "from ibapi.client import EClient",
    "imports an async broker library": "import ib_async",
    "imports multiprocessing.connection": "from multiprocessing.connection import Client",
    "binds multiprocessing": "import multiprocessing",
    "imports socket dynamically": "import importlib\ns = importlib.import_module('socket')",
    "imports socket with dunder import": "s = __import__('socket')",
    "imports a network submodule dynamically": "import importlib\nr = importlib.import_module('urllib.request')",
    "imports by a computed name": "import importlib\nm = importlib.import_module(name)",
    "dunder import of a computed name": "m = __import__(prefix + 'ocket')",
    "imports through an aliased import_module": "from importlib import import_module\nm = import_module('ssl')",
    "takes socket from sys.modules": "import sys\ns = sys.modules['socket']",
    "evaluates text": "eval(\"__import__('socket')\")",
    "executes text": "exec(code)",
    "compiles text": "compile(source, 'x', 'exec')",
    "compiles text through builtins": "import builtins\ncode = builtins.compile(source, 'x', 'exec')",
    "evaluates text through builtins": "import builtins\nbuiltins.eval(text)",
    "live port after a colon in text": "ADDRESS = 'gateway:4001'",
    "rebinds the registry": "from hftlab.safety import venues\nvenues.VENUES = {}",
    "rebinds the registry by name": "VENUES = {'ibkr': None}",
    "adds a venue": "from hftlab.safety.venues import VENUES\nVENUES['ibkr'] = None",
    "annotated rebinding": "VENUES: dict = {}",
    "augmented rebinding": "from hftlab.safety.venues import VENUES\nVENUES |= {'ibkr': None}",
    "walrus rebinding": "if (VENUES := {}): ...",
    "deletes a venue": "from hftlab.safety.venues import VENUES\ndel VENUES['sim']",
    "setattr on the registry module": "import hftlab.safety.venues as v\nsetattr(v, 'VENUES', {})",
    "setattr with a computed name": "import hftlab.safety.venues as v\nsetattr(v, name, {})",
    "updates the registry": "from hftlab.safety.venues import VENUES\nVENUES.update(ibkr=None)",
}


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_the_scan_catches_each_way_round_it(name):
    assert violations(SNEAKY, ast.parse(BYPASSES[name])), name


HONEST = {
    "the paper ports": "PORTS = (4002, 7497)",
    "numbers that only resemble a live port": "A, B, C = 14001, 40010, 74960\nTEXT = 'order 140017, id 74961'",
    "prices that end in the digits of a live port": "NOTE = 'GBP.USD 1.4001, AUD.USD 0.7496'",
    "a method called compile": "import re\nWORD = re.compile('[a-z]+')",
    "words that only contain live": "alive = True\nliveness = 1\ndelivery = 'delivered'\nnote = 'the live docs'",
    "a field of the lock's facts": "facts = ConnectionFacts(port=4002, managed_accounts=a, live_username=None)",
    "a harmless environment read": "import os\nstate = os.environ.get('HFT_STATE_DIR', 'var')",
    "a harmless subscript read": "import os\nhome = os.environ['HOME']",
    "a harmless getenv": "import os\nzone = os.getenv('TZ', 'UTC')",
    "variables that only resemble mode or live": "import os\na = os.getenv('MODEL_DIR')\nb = os.getenv('ALIVE_CHECK')",
    "the harmless part of urllib": "from urllib.parse import urlsplit\nfrom urllib import parse",
    "the harmless part of multiprocessing": "from multiprocessing import Process",
    "ordinary imports": "import json, re, decimal, hashlib, subprocess, pathlib\nfrom dataclasses import dataclass",
    "a constant dynamic import of our own": "import importlib\nm = importlib.import_module('hftlab.core.money')",
    "reading the registry": "from hftlab.safety.venues import VENUES, venue\nv = VENUES['sim']\nnames = sorted(VENUES)",
    "the registry's read methods": "from hftlab.safety.venues import VENUES\nx = VENUES.get('sim')\ny = VENUES.items()",
    "setattr on something else": "setattr(self, 'ready', True)",
}


@pytest.mark.parametrize("name", sorted(HONEST))
def test_the_scan_passes_ordinary_code(name):
    assert violations(SNEAKY, ast.parse(HONEST[name])) == [], name


NETWORK_SOURCE = "import socket\nimport asyncio\nfrom ib_async import IB\nimport urllib.request"


def test_the_adapter_may_use_the_network_and_the_broker_library():
    assert violations("src/hftlab/venues/ibkr/client.py", ast.parse(NETWORK_SOURCE)) == []


@pytest.mark.parametrize(
    "module",
    ["risk/gate.py", "venues/__init__.py", "venues/port.py", "venues/guard.py", "venues/fake/broker.py",
     "venues/sim/venue.py", "record/recorder.py", "replay/reader.py", "shadow/runner.py"],
)  # fmt: skip
def test_everything_else_including_the_recorder_and_the_shadow_venue_may_use_neither(module):
    assert len(violations(f"src/hftlab/{module}", ast.parse(NETWORK_SOURCE))) == 4


HTTP_CLIENT = (
    "from urllib.request import HTTPRedirectHandler, Request, build_opener\n"
    "from urllib.error import HTTPError, URLError"
)


def test_the_host_layers_post_may_import_the_http_client_by_name_and_nothing_else():
    assert violations(POST, ast.parse(HTTP_CLIENT)) == []
    for src in (
        "import socket", "import ssl", "import asyncio", "import http.client", "import urllib.request",
        "from urllib import request", "from urllib.request import urlopen", "import requests", "import httpx",
        "from ib_async import IB", "import importlib\nr = importlib.import_module('urllib.request')",
        "s = __import__('socket')",
    ):  # fmt: skip
        assert violations(POST, ast.parse(src)), src


def test_that_exemption_is_one_file_and_it_is_used_exactly():
    for rel in ("src/hftlab/ops/alerts.py", "src/hftlab/ops/status.py", "src/hftlab/publish/send.py",
                "src/hftlab/ops/post/other.py", "src/hftlab/record/post.py"):  # fmt: skip
        assert len(violations(rel, ast.parse(HTTP_CLIENT))) == 5, rel
    reached = {name for _, name in imports(dict(sources())[POST], POST) if is_network(name)}
    assert reached == POST_MAY_IMPORT, "the list allows what the module imports, no more and no less"


def test_the_post_module_gets_no_other_exemption():
    for name in ("live port as a number", "live mode literal", "reads MODE", "takes the whole environment",
                 "imports by a computed name", "executes text", "rebinds the registry by name"):  # fmt: skip
        assert violations(POST, ast.parse(BYPASSES[name])), name


@pytest.mark.parametrize("library", sorted(BROKER_LIBRARIES))
def test_every_broker_library_is_confined_to_the_adapter(library):
    for src in (f"import {library}", f"from {library} import x", f"__import__('{library}')"):
        assert violations("src/hftlab/venues/fake/broker.py", ast.parse(src)), src
        assert violations("src/hftlab/record/recorder.py", ast.parse(src)), src
        assert violations("src/hftlab/venues/ibkr/adapter.py", ast.parse(src)) == [], src


@pytest.mark.parametrize("rel", ["src/hftlab/venues/ibkr/connection.py", "src/hftlab/venues/ibkr/feed.py"])
def test_the_network_packages_get_no_other_exemption(rel):
    for name in ("live port as a number", "live mode literal", "reads MODE", "takes the whole environment",
                 "imports by a computed name", "executes text", "rebinds the registry by name"):  # fmt: skip
        assert violations(rel, ast.parse(BYPASSES[name])), name


def test_the_lock_itself_may_not_use_the_network_or_a_live_mode_literal():
    assert violations(LOCK, ast.parse("import socket"))
    assert violations(LOCK, ast.parse("MODE = 'live'"))
    assert violations(LOCK, ast.parse("LIVE_PORTS = {4001, 7496}\nimport os\nmode = os.environ.get('MODE')")) == []


def test_a_relative_import_is_resolved_before_it_is_judged():
    tree = ast.parse("from . import connection\nfrom ..safety import lock")
    assert network_violations("src/hftlab/risk/gate.py", tree) == []
    assert imports(tree, "src/hftlab/risk/gate.py") == [
        ("from", "hftlab.risk.connection"),
        ("from", "hftlab.safety.lock"),
    ]
    assert imports(ast.parse("from . import x"), "src/hftlab/risk/__init__.py") == [("from", "hftlab.risk.x")]


# ---- the venue registry --------------------------------------------------------------------------------------------


def test_the_registry_holds_exactly_the_paper_broker_and_the_simulator():
    assert set(VENUES) == {"ibkr_paper", "sim"}
    assert all(isinstance(v, Venue) and v.name == name for name, v in VENUES.items())


def test_only_the_simulator_is_exempt_from_the_paper_lock():
    assert VENUES["ibkr_paper"].needs_paper_lock is True
    assert [name for name, v in VENUES.items() if not v.needs_paper_lock] == ["sim"]


@pytest.mark.parametrize("name", ["ibkr_live", "ibkr", "live", "IBKR_PAPER", "ibkr_paper ", "", None, 5])
def test_an_unregistered_venue_has_no_fallback(name):
    with pytest.raises(UnknownVenueError):
        venue(name)
    assert venue("ibkr_paper") is VENUES["ibkr_paper"] and venue("sim").name == "sim"


def test_the_registry_cannot_be_edited_at_run_time():
    with pytest.raises(TypeError):
        VENUES["ibkr_live"] = Venue("ibkr_live", "no", False)  # type: ignore[index]
    with pytest.raises(TypeError):
        del VENUES["sim"]  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        VENUES["sim"].needs_paper_lock = True  # type: ignore[misc]
    assert set(VENUES) == {"ibkr_paper", "sim"}
