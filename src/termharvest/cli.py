"""Command-line entry point: argument parsing and dispatch only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import AUTHORIZED_USE_NOTICE, __version__
from .config import load_screens_dir, load_session
from .errors import HarvestError
from .logging_util import build_logger
from .mockhost import MockTerminalServer
from .pipeline import run_pipeline
from .store import Store
from .validate_config import load_validation_config

EPILOG = f"合规声明：{AUTHORIZED_USE_NOTICE}"

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="termharvest", description="终端拾屏台：离线、只读、可复现的字符终端屏幕采集管线。", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve-mock", help="start the loopback-only mock terminal", description="启动仅绑定 loopback 的确定性 mock 终端。", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    serve.add_argument("--scenario", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--max-connections", type=int, default=8)
    serve.add_argument("--frame-timeout", type=float, default=5.0)
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--log-level", default="INFO")
    serve.add_argument("--log-file")

    run = sub.add_parser("run", help="collect, extract, validate, store and report", description="执行一次只读采集管线。", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    run.add_argument("--session", required=True)
    run.add_argument("--screens", required=True)
    run.add_argument("--validators", required=True)
    run.add_argument("--db", default="data/termharvest.db")
    run.add_argument("--out", default="data/report")
    run.add_argument("--mode", choices=["live", "replay"], default="live")
    run.add_argument("--cassette")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--evidence-dir")
    run.add_argument("--record-cassette")
    run.add_argument("--redact-package", help="write a redacted package (no sensitive values) for verifiers")
    run.add_argument("--redact-scope", choices=["span", "row"], default="span", help="redact only the sensitive value spans or their whole rows")
    run.add_argument("--log-level", default="INFO")
    run.add_argument("--log-file")

    verify = sub.add_parser(
        "verify-redacted",
        help="verify and replay a redacted package offline",
        description="离线核验脱敏拾屏包：校验脱敏帧摘要、契约摘要、投影指纹、帧序与 trailer，并重放非敏感抽取与批量校验。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify.add_argument("--package", required=True)
    verify.add_argument("--screens", required=True)
    verify.add_argument("--session", required=True)
    verify.add_argument("--validators")
    verify.add_argument("--log-level", default="INFO")
    verify.add_argument("--log-file")
    return parser

def _serve(args: argparse.Namespace) -> int:
    logger = build_logger("termharvest.mock", level=args.log_level, log_file=args.log_file)
    server = MockTerminalServer(args.scenario, host=args.host, port=args.port, max_connections=args.max_connections, frame_timeout=args.frame_timeout, once=args.once, logger=logger)
    port = server.start()
    print(f"MOCK_READY host={server.address[0]} port={port} " f"scenario={server.scenario.scenario_id}", flush=True)
    logger.info("event=mock_listening port=%s once=%s", port, args.once)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0

def _run(args: argparse.Namespace) -> int:
    logger = build_logger("termharvest", level=args.log_level, log_file=args.log_file)
    session = load_session(args.session)
    screens = load_screens_dir(args.screens)
    validation_config = load_validation_config(args.validators)
    store = Store(args.db)
    try:
        result = run_pipeline(
            session=session,
            screens=screens,
            validation_config=validation_config,
            store=store,
            output_dir=args.out,
            mode=args.mode,
            cassette_path=args.cassette,
            evidence_dir=args.evidence_dir,
            host_override=args.host,
            port_override=args.port,
            record_cassette=args.record_cassette,
            redact_package=args.redact_package,
            redact_scope=args.redact_scope,
            logger=logger,
        )
    finally:
        store.close()
    summary = result["summary"]
    print("RUN_RESULT " + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["status"] == "success" else 1

def _verify_redacted(args: argparse.Namespace) -> int:
    from .redaction import verify_redacted_package

    session = load_session(args.session)
    screens = load_screens_dir(args.screens)
    validation_config = load_validation_config(args.validators) if args.validators else None
    summary = verify_redacted_package(args.package, screens=screens, session=session, validation_config=validation_config)
    print("VERIFY_RESULT " + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0

def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve-mock":
            return _serve(args)
        if args.command == "run":
            return _run(args)
        if args.command == "verify-redacted":
            return _verify_redacted(args)
        return 2
    except HarvestError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        if exc.context:
            print("CONTEXT " + json.dumps(exc.context, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
