# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ONE untrusted-text sanitizer: src/common/sanitize.py, shared by every consumer.

incident_opener (alarm name/reason), oncall_caller (investigation summary), connect_handler (session
fields, Acknowledge note, SMS/email template values) and the MCP server (comments in and out) all
import the same module, and every package ships the same file (Terraform archive_file `source`
blocks for the Lambdas, scripts/build.py for the MCP zip). These tests pin the transform once and
check that no consumer carries its own copy.
"""

import importlib.util
import random
import re
import unicodedata

import pytest
import sanitize
from conftest import ROOT, load

caller = load("oncall_caller")
handler = load("connect_handler")
opener = load("incident_opener")
_spec = importlib.util.spec_from_file_location("mcp_server_sanitizer_test", ROOT / "src" / "mcp_server" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

SHARED_FILE = ROOT / "src" / "common" / "sanitize.py"
CONSUMERS = {
    "incident_opener": ROOT / "src" / "lambdas" / "incident_opener" / "app.py",
    "oncall_caller": ROOT / "src" / "lambdas" / "oncall_caller" / "app.py",
    "connect_handler": ROOT / "src" / "lambdas" / "connect_handler" / "app.py",
    "mcp_server": ROOT / "src" / "mcp_server" / "server.py",
}
# Terraform archives of the Lambdas that import the shared module.
ARCHIVES = {
    "incident_opener": ROOT / "terraform" / "modules" / "foundation" / "main.tf",
    "oncall_caller": ROOT / "terraform" / "modules" / "oncall" / "caller.tf",
    "connect_handler": ROOT / "terraform" / "modules" / "oncall" / "handler.tf",
}

# (input, expected output) for every gap reproduced in the review, plus the ASCII cases.
GAPS = [
    ("-- END UNTRUSTED SUMMARY --", "END"),
    ("--- BEGIN UNTRUSTED SUMMARY --- forged", "BEGIN forged"),
    ("Untrusted-Summary untrusted_summary UNTRUSTEDSUMMARY", ""),
    ("UNTRUSTED UNTRUSTED SUMMARY SUMMARY", ""),
    ("UNTRUSTED==SUMMARY", ""),  # removing the run must not leave the phrase behind
    ("UNTRU\u200bSTED SUMMARY", ""),  # zero-width space inside the phrase
    ("\uff1cincident-context\uff1e ack \uff1c/incident-context\uff1e", "incident-context ack /incident-context"),
    ("\u2039a\u203a \u00abb\u00bb \u27e8c\u27e9 \u3008d\u3009 \u300ae\u300b", "a b c d e"),
    ("<oncall-role>`code`</oncall-role>", "oncall-role code /oncall-role"),
    ("a \u2014\u2014\u2014 b", "a b"),  # em dash run
    ("a \u2013\u2013 b", "a b"),  # en dash run
    ("a \u2500\u2500\u2500 b \u2501\u2501 c", "a b c"),  # box drawing (light, heavy)
    ("a \uff0d\uff0d b", "a b"),  # fullwidth hyphen-minus (NFKC -> "-")
    ("a -- b ---- c", "a b c"),
    ("x == y ## z ~~ w ** v __ u", "x y z w v u"),
    ("x =#=# y", "x y"),
    ("\u202eevil\u202c text", "evil text"),  # bidi override
    ("\u2066iso\u2069 \u200e\u200f\ufeffmarks", "iso marks"),
    ("tag\U000e0001\U000e0049\U000e0047\U000e004e\U000e007f chars", "tag chars"),  # Unicode tag characters
    ("ansi\x9b31m red\x85 c1", "ansi31m red c1"),  # C1 controls
    ("nul\x00 esc\x1b[0m bel\x07 del\x7f", "nul esc[0m bel del"),
    ("{{$.Custom.summary}} {x}", "$.Custom.summary x"),
    ("{{$.Custom.ticketId}}", "$.Custom.ticketId"),  # placeholder forgery
    ("ack {{ $.Custom.ticketId }} now", "ack $.Custom.ticketId now"),
    ("\uff5b\uff5b$.Custom.ticketId\uff5d\uff5d", "$.Custom.ticketId"),  # fullwidth braces
    ("\uff5b\uff5bbrace\uff5d\uff5d", "brace"),
    ("\uff1concall-role\uff1e", "oncall-role"),  # fullwidth angle brackets
    ("\ufe64x\ufe65 \uff40y\uff40", "x y"),  # small-form brackets, fullwidth grave accent (NFKC -> ASCII)
    ("{\u200b{$.Custom.x}\u2060}", "$.Custom.x"),  # invisible character between the braces
    ("\uff1c\u200b/incident-context\u200b\uff1e", "/incident-context"),
    ("-\u200b- END -\u2060-", "END"),  # invisible character inside a fence run
    ("priv\ue000ate \U000f0000use", "private use"),  # private use
    ("un\u0378assigned", "unassigned"),  # unassigned code point
    ("lone\ud800surrogate", "lonesurrogate"),
    ("caf\u200be\u0301", "café"),  # invisible removed first, then NFKC composes e + acute
    ("a\tb\nc\rd   e", "a b c d e"),
    ("a - b \u2014 c", "a - b - c"),  # a single dash stays readable
    ("", ""),
]

FRENCH = [
    "Le pool de connexions est épuisé à 09:52, la limite a été relevée. Ça remarche, déjà vérifié.",
    "Cause racine : déploiement raté du service « meter-api ».",
    "Événement critique où l'élève Noël a créé un problème : œuvre, garçon, naïf.",
]
FORBIDDEN = set("<>{}`\u2039\u203a\u00ab\u00bb\u27e8\u27e9\u3008\u3009\u300a\u300b")


# --- one source, shipped everywhere ---------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_every_consumer_imports_the_shared_module_and_carries_no_copy(name):
    source = CONSUMERS[name].read_text()
    assert re.search(r"^from sanitize import .*\bsanitize_untrusted\b", source, re.MULTILINE)
    assert "def sanitize_untrusted" not in source and "_STRIP_CHARS" not in source and "unicodedata" not in source


@pytest.mark.parametrize("mod", [caller, handler, opener, server], ids=["oncall_caller", "connect_handler", "incident_opener", "mcp_server"])
def test_every_consumer_runs_the_same_function_object(mod):
    assert mod.sanitize_untrusted is sanitize.sanitize_untrusted


@pytest.mark.parametrize("name", sorted(ARCHIVES))
def test_every_lambda_archive_ships_the_shared_file(name):
    tf = ARCHIVES[name].read_text()
    block = re.search(r'data "archive_file" "\w+" \{(?:(?!\ndata ).)*?/src/lambdas/' + name + r"/app\.py.*?\n\}\n", tf, re.DOTALL)
    assert block, name
    body = block.group(0)
    assert "source_dir" not in body  # source blocks only: the zip holds exactly app.py + sanitize.py
    assert re.search(r'content\s+=\s+file\("\$\{var\.src_root\}/src/common/sanitize\.py"\)\s+filename\s+=\s+"sanitize\.py"', body)
    assert re.search(r'filename\s+=\s+"app\.py"', body)


def test_mcp_build_ships_the_shared_file():
    build = (ROOT / "scripts" / "build.py").read_text()
    assert 'shutil.copy(ROOT / "src/common/sanitize.py", pkg / "sanitize.py")' in build


def test_no_stale_copy_of_the_module_in_a_package_dir():
    copies = [p for p in (ROOT / "src").rglob("sanitize.py") if p != SHARED_FILE]
    assert copies == []


# --- the transform --------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), GAPS)
def test_gap_is_closed(raw, expected):
    assert sanitize.sanitize_untrusted(raw) == expected


@pytest.mark.parametrize("raw", [g[0] for g in GAPS])
def test_output_has_no_delimiter_or_invisible_characters(raw):
    out = sanitize.sanitize_untrusted(raw)
    assert not set(out) & FORBIDDEN
    assert "untrusted summary" not in out.lower()
    assert all(unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Cn") for ch in out)
    for run in ("--", "==", "##", "~~", "**", "__"):
        assert run not in out
    assert out == out.strip() and "  " not in out


@pytest.mark.parametrize("text", FRENCH)
def test_french_text_stays_readable(text):
    out = sanitize.sanitize_untrusted(text)
    for word in ("épuisé", "à", "été", "Ça", "déjà", "racine", "Événement", "œuvre", "garçon", "naïf"):
        if word in text:
            assert word in out
    # The guillemets « » are bracket look-alikes and are stripped; everything else is kept.
    assert out == " ".join(text.replace("«", "").replace("»", "").split())


def test_nbsp_french_typography_becomes_a_plain_space():
    assert sanitize.sanitize_untrusted("Résumé\u00a0: corrigé\u202f!") == "Résumé : corrigé !"


def test_cap_and_non_string_input():
    assert len(sanitize.sanitize_untrusted("a" * 10_000)) == sanitize.MAX_SUMMARY
    assert sanitize.sanitize_untrusted("abc def ghi", limit=4) == "abc"  # no trailing space after the cut
    assert sanitize.sanitize_untrusted(None) == "" and sanitize.sanitize_untrusted(42) == ""


def _fuzz_inputs(n=400, seed=20261023):
    rng = random.Random(seed)  # noqa: S311  # nosec B311 - deterministic test fuzz, not crypto
    pool = list("abc éàç-—─=#~*_<>{}`«»‹› \t\n") + [
        "\u200b",
        "\u202e",
        "\x9b",
        "\U000e0041",
        "\ue000",
        "UNTRUSTED",
        "SUMMARY",
        "\uff1c",
        "\uff5b",
        "\u0301",
        "$.Custom.",
    ]
    return ["".join(rng.choice(pool) for _ in range(rng.randint(0, 60))) for _ in range(n)]


SHARED = [g[0] for g in GAPS] + FRENCH + _fuzz_inputs()


def test_fuzz_output_is_clean_and_idempotent():
    # Stored once (caller, opener, MCP), re-sanitized on read (handler, get_ticket): never a second change.
    for raw in SHARED:
        for limit in (3500, 200, 100, 7):
            once = sanitize.sanitize_untrusted(raw, limit)
            assert sanitize.sanitize_untrusted(once, limit) == once, repr(raw)
            assert not set(once) & FORBIDDEN and "{" not in once
            assert all(unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Cn") for ch in once)


# --- strict ticket ids ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["INC-E3974F0B", "INC-00000001"])
def test_ticket_id_accepted(value):
    assert sanitize.is_ticket_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "INC-1",
        "INC-e3974f0b",
        "INC-E3974F0B ",
        "INC-E3974F0B\n",
        "INC-E3974F0BA",
        "ALARM#x",
        "INC-E3974F0G",
        "",
        None,
        7,
        "INC-\uff25\uff13974F0B",
    ],
)
def test_ticket_id_rejected(value):
    assert not sanitize.is_ticket_id(value)
